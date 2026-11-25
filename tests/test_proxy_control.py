"""The egress proxy's control socket, through `serve_control` and `ProxyControlClient`.

The client is synchronous, as the runner's `ProxyControl` is, so these tests call it from a
thread while the server runs on the event loop. The socket answers root alone by default; these
tests run as another user, so they serve it for their own user id, which the kernel reports for
each connection on Linux (SO_PEERCRED) and on macOS (LOCAL_PEERCRED) alike.
"""

import asyncio
import contextlib
import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import footfall.proxy.control
from footfall.proxy.control import (
    MAX_REQUEST,
    ProxyControlClient,
    ProxyRefused,
    serve_control,
)
from footfall.proxy.policy import Policy
from footfall.proxy.server import ProxyServer
from footfall.runner.service import ProxyControl

RUN = "r_abcdefghijklmnopqrstuvwxyz"
OTHER = "r_" + "q" * 26
APP = "10.89.0.10"
BROWSERS = "10.89.0.11"


@dataclass
class Live:
    policy: Policy
    proxy: ProxyServer
    client: ProxyControlClient
    socket: Path
    port: int  # the proxy port each listener opens


@contextlib.contextmanager
def short_directory() -> Iterator[Path]:
    # pytest's tmp_path is too deep for a Unix socket path (about 100 bytes at most on macOS).
    with tempfile.TemporaryDirectory(prefix="ffp", dir="/tmp") as directory:
        yield Path(directory)


async def wait_for_socket(path: Path, task: asyncio.Task[None]) -> None:
    for _ in range(500):
        if path.exists() or task.done():
            break
        await asyncio.sleep(0.01)
    if task.done():
        task.result()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    policy = Policy()
    port = free_port()
    proxy = ProxyServer(policy, port=port)
    with short_directory() as directory:
        path = directory / "p.sock"
        task = asyncio.create_task(serve_control(proxy, path, allowed_uid=os.getuid()))
        try:
            await wait_for_socket(path, task)
            yield Live(policy, proxy, ProxyControlClient(path), path, port)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await proxy.close()


async def connects(port: int) -> bool:
    """Whether something accepts a connection on 127.0.0.1 at `port`."""
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def call[T](function: Callable[..., T], *args: Any) -> T:
    return await asyncio.to_thread(function, *args)


async def exchange(path: Path, data: bytes) -> dict[str, Any]:
    """Send raw bytes and return the decoded reply."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(data)
        if not data.endswith(b"\n"):
            writer.write_eof()
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    reply: dict[str, Any] = json.loads(line)
    return reply


def test_the_client_is_a_proxy_control() -> None:
    control: ProxyControl = ProxyControlClient(Path("/run/footfall/proxy.sock"))
    assert callable(control.revoke_run)
    assert callable(control.listen)


async def test_allow_revoke_and_reached_round_trip(live: Live) -> None:
    policy, client = live.policy, live.client
    await call(client.allow, APP, RUN, ["api.example.com", "Tiles.Example.org"])
    assert policy.decide(APP, "api.example.com", 443).allowed
    assert policy.decide(APP, "tiles.example.org", 80).allowed
    assert await call(client.reached, RUN) == []

    policy.record(RUN, "tiles.example.org")
    policy.record(RUN, "api.example.com")
    assert await call(client.reached, RUN) == ["api.example.com", "tiles.example.org"]

    await call(client.revoke, APP)
    assert policy.decide(APP, "api.example.com", 443).run_id is None
    await call(client.revoke, APP)  # twice is harmless


async def test_allow_replaces_the_earlier_list_of_the_same_run(live: Live) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    await call(live.client.allow, APP, RUN, ["fonts.gstatic.com"])
    assert not live.policy.decide(APP, "api.example.com", 443).allowed
    assert live.policy.decide(APP, "fonts.gstatic.com", 443).allowed


async def test_allow_for_another_run_is_refused(live: Live) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    with pytest.raises(ProxyRefused, match="another run"):
        await call(live.client.allow, APP, OTHER, ["fonts.gstatic.com"])
    assert live.policy.decide(APP, "api.example.com", 443).run_id == RUN


async def test_revoke_run_round_trip(live: Live) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    await call(live.client.allow, BROWSERS, RUN, ["fonts.gstatic.com"])
    await call(live.client.allow, "10.90.0.10", OTHER, ["api.example.com"])
    await call(live.client.revoke_run, RUN)
    assert live.policy.decide(APP, "api.example.com", 443).run_id is None
    assert live.policy.decide(BROWSERS, "fonts.gstatic.com", 443).run_id is None
    assert live.policy.decide("10.90.0.10", "api.example.com", 443).allowed


async def test_the_proxy_listens_nowhere_until_told(live: Live) -> None:
    assert not await connects(live.port)
    await call(live.client.listen, "127.0.0.1", RUN)
    assert await connects(live.port)


async def test_listen_twice_for_the_same_run_is_harmless(live: Live) -> None:
    await call(live.client.listen, "127.0.0.1", RUN)
    await call(live.client.listen, "127.0.0.1", RUN)
    await call(live.client.listen, "::ffff:127.0.0.1", RUN)  # the same address, mapped
    assert await connects(live.port)
    await call(live.client.revoke_run, RUN)
    assert not await connects(live.port)  # one listener, and it is gone


async def test_listen_on_another_runs_address_is_refused(live: Live) -> None:
    await call(live.client.listen, "127.0.0.1", RUN)
    with pytest.raises(ProxyRefused, match="another run"):
        await call(live.client.listen, "127.0.0.1", OTHER)
    await call(live.client.revoke_run, OTHER)  # the other run holds nothing to close
    assert await connects(live.port)


async def test_revoke_run_closes_the_runs_listeners(live: Live) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    await call(live.client.listen, "127.0.0.1", RUN)
    assert await connects(live.port)
    await call(live.client.revoke_run, RUN)
    assert live.policy.decide(APP, "api.example.com", 443).run_id is None
    assert not await connects(live.port)
    # The address is free again, for whichever run is given it next.
    await call(live.client.listen, "127.0.0.1", OTHER)
    assert await connects(live.port)


@contextlib.asynccontextmanager
async def idle_connection(live: Live) -> AsyncIterator[asyncio.StreamReader]:
    """A connection from 127.0.0.1 to the proxy, open and served, that has sent nothing yet."""
    reader, writer = await asyncio.open_connection("127.0.0.1", live.port)
    try:
        async with asyncio.timeout(5):
            while live.proxy.connections < 1:
                await asyncio.sleep(0.01)
        yield reader
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def closed_by_proxy(reader: asyncio.StreamReader) -> bool:
    try:
        async with asyncio.timeout(5):
            return await reader.read() == b""
    except ConnectionError:
        return True


async def test_revoke_closes_the_clients_connections(live: Live) -> None:
    await call(live.client.listen, "127.0.0.1", RUN)
    await call(live.client.allow, "127.0.0.1", RUN, ["api.example.com"])
    async with idle_connection(live) as reader:
        await call(live.client.revoke, APP)  # another address
        assert live.proxy.connections == 1
        await call(live.client.revoke, "127.0.0.1")
        assert await closed_by_proxy(reader)


async def test_revoke_run_closes_its_clients_connections(live: Live) -> None:
    await call(live.client.listen, "127.0.0.1", RUN)
    await call(live.client.allow, "127.0.0.1", RUN, ["api.example.com"])
    async with idle_connection(live) as reader:
        await call(live.client.revoke_run, OTHER)
        assert live.proxy.connections == 1
        await call(live.client.revoke_run, RUN)
        assert await closed_by_proxy(reader)


@pytest.mark.parametrize(
    ("ip", "message"),
    [
        ("not an address", "not an IP address"),
        ("10.89.0.300", "not an IP address"),
        ("0.0.0.0", "every interface"),  # noqa: S104  # the address being refused
        ("::", "every interface"),
    ],
)
async def test_listen_refuses_what_is_not_one_address(live: Live, ip: str, message: str) -> None:
    with pytest.raises(ProxyRefused, match=message):
        await call(live.client.listen, ip, RUN)
    assert not await connects(live.port)


async def test_listen_refuses_a_bad_run_id(live: Live) -> None:
    with pytest.raises(ProxyRefused, match="run id"):
        await call(live.client.listen, "127.0.0.1", "r_short")
    assert not await connects(live.port)


async def test_listen_that_cannot_bind_is_refused_with_the_reason(live: Live) -> None:
    # 192.0.2.1 is kept for documentation (RFC 5737), so no machine running the tests has it.
    with pytest.raises(ProxyRefused, match=r"could not listen on 192\.0\.2\.1") as caught:
        await call(live.client.listen, "192.0.2.1", RUN)
    assert "assign" in str(caught.value).lower()  # the operating system's own words
    await call(live.client.listen, "127.0.0.1", RUN)  # nothing was held for the failure
    assert await connects(live.port)


@pytest.mark.parametrize(
    ("op", "args", "message"),
    [
        ("allow", ("not an address", RUN, ["api.example.com"]), "IP address"),
        ("allow", (APP, "r_short", ["api.example.com"]), "run id"),
        ("allow", (APP, RUN, ["169.254.169.254"]), "host name"),
        ("revoke", ("10.89.0.300",), "IP address"),
        ("revoke_run", ("../../etc",), "run id"),
        ("reached", ("r_short",), "run id"),
    ],
)
async def test_policy_refusals_raise_proxy_refused(
    live: Live, op: str, args: tuple[Any, ...], message: str
) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    with pytest.raises(ProxyRefused, match=message) as caught:
        await call(getattr(live.client, op), *args)
    assert isinstance(caught.value, ValueError)
    assert live.policy.decide(APP, "api.example.com", 443).allowed  # nothing changed


@contextlib.asynccontextmanager
async def serving(**options: Any) -> AsyncIterator[tuple[Policy, ProxyControlClient]]:
    """A control socket served with `options`, for tests of who may use it."""
    policy = Policy()
    proxy = ProxyServer(policy, port=free_port())
    with short_directory() as directory:
        path = directory / "p.sock"
        task = asyncio.create_task(serve_control(proxy, path, **options))
        try:
            await wait_for_socket(path, task)
            yield policy, ProxyControlClient(path)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await proxy.close()


async def test_a_peer_of_another_user_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    async with serving(allowed_uid=os.getuid() + 1) as (policy, client):
        with pytest.raises(ProxyRefused, match="answers only user"):
            await call(client.allow, APP, RUN, ["api.example.com"])
        assert policy.decide(APP, "api.example.com", 443).run_id is None
    assert f"from user {os.getuid()}" in caplog.text


@pytest.mark.skipif(os.getuid() == 0, reason="the tests run as root, the one user it answers")
async def test_the_control_socket_answers_root_only_by_default() -> None:
    async with serving() as (policy, client):
        with pytest.raises(ProxyRefused, match="answers only user 0"):
            await call(client.allow, APP, RUN, ["api.example.com"])
        assert policy.decide(APP, "api.example.com", 443).run_id is None


async def test_a_peer_the_platform_cannot_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(footfall.proxy.control, "_peer_uid", lambda sock: None)
    async with serving(allowed_uid=os.getuid()) as (policy, client):
        with pytest.raises(ProxyRefused, match="answers only user"):
            await call(client.allow, APP, RUN, ["api.example.com"])
        assert policy.decide(APP, "api.example.com", 443).run_id is None


def test_the_peers_user_id_comes_from_the_kernel() -> None:
    first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with first, second:
        assert footfall.proxy.control._peer_uid(first) == os.getuid()


def test_a_socket_the_kernel_cannot_describe_has_no_peer_user() -> None:
    closed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    closed.close()
    assert footfall.proxy.control._peer_uid(closed) is None


def test_unreachable_proxy_raises_os_error() -> None:
    with short_directory() as directory:
        client = ProxyControlClient(directory / "missing.sock")
        with pytest.raises(OSError, match=r"."):
            client.allow(APP, RUN, [])
        with pytest.raises(OSError, match=r"."):
            client.revoke_run(RUN)


async def test_socket_mode_0600(live: Live) -> None:
    mode = live.socket.stat().st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o600


async def test_oversized_request_refused(live: Live) -> None:
    big = json.dumps({"op": "revoke_run", "run_id": "r_" + "a" * MAX_REQUEST}).encode() + b"\n"
    reply = await exchange(live.socket, big)
    assert reply == {"ok": False, "error": "request is larger than 64 KiB"}
    hosts = [f"host{n}.example.com" for n in range(5000)]
    with pytest.raises(ProxyRefused, match="64 KiB"):
        await call(live.client.allow, APP, RUN, hosts)
    assert live.policy.decide(APP, "host1.example.com", 443).run_id is None


async def test_request_of_exactly_the_limit_is_read(live: Live) -> None:
    request = {"op": "reached", "run_id": RUN, "pad": ""}
    size = len(json.dumps(request).encode())
    request["pad"] = "p" * (MAX_REQUEST - size)
    data = json.dumps(request).encode()
    assert len(data) == MAX_REQUEST
    reply = await exchange(live.socket, data + b"\n")
    assert reply == {"ok": False, "error": "unexpected field: pad"}


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"not json\n", "request is not valid JSON"),
        (b"[" * 30_000 + b"\n", "request is not valid JSON"),
        (b'["reached"]\n', "request must be a JSON object"),
        (b'{"run_id": "r_abcdefghijklmnopqrstuvwxyz"}\n', "unknown op: None"),
        (b'{"op": "exec", "run_id": "r_abcdefghijklmnopqrstuvwxyz"}\n', "unknown op: 'exec'"),
        (b'{"op": "revoke"}\n', "missing field: ip"),
        (b'{"op": "revoke", "ip": 167772170}\n', "ip must be a string"),
        (b'{"op": "revoke_run", "run_id": null}\n', "run_id must be a string"),
        (b'{"op": "listen", "ip": "127.0.0.1"}\n', "missing field: run_id"),
        (
            b'{"op": "listen", "ip": 2130706433, "run_id": "r_abcdefghijklmnopqrstuvwxyz"}\n',
            "ip must be a string",
        ),
        (
            b'{"op": "listen", "ip": "127.0.0.1", "run_id": "r_abcdefghijklmnopqrstuvwxyz",'
            b' "port": 80}\n',
            "unexpected field: port",
        ),
        (
            b'{"op": "allow", "ip": "10.89.0.10", "run_id": "r_abcdefghijklmnopqrstuvwxyz",'
            b' "hosts": "api.example.com"}\n',
            "hosts must be a list of strings",
        ),
        (
            b'{"op": "allow", "ip": "10.89.0.10", "run_id": "r_abcdefghijklmnopqrstuvwxyz",'
            b' "hosts": [1]}\n',
            "hosts must be a list of strings",
        ),
        (b'{"op": "reached", "run_id": "r_abcdefghijklmnopqrstuvwxyz", "x": 1}\n', "unexpected"),
        (b'{"op": "reached", "run_id": "r_abcdefghijklmnopqrstuvwxyz"}', "request must end"),
    ],
)
async def test_malformed_requests_refused(live: Live, data: bytes, error: str) -> None:
    reply = await exchange(live.socket, data)
    assert reply["ok"] is False
    assert reply["error"].startswith(error)
    assert live.policy.decide(APP, "api.example.com", 443).run_id is None
    assert not await connects(live.port)


async def test_serve_replaces_a_stale_socket_but_not_a_file() -> None:
    proxy = ProxyServer(Policy())
    with short_directory() as directory:
        path = directory / "p.sock"
        path.write_text("not a socket\n")
        with pytest.raises(FileExistsError):
            await serve_control(proxy, path)
        assert path.read_text() == "not a socket\n"

        path.unlink()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()  # leaves the socket file behind, as a crashed proxy would
        task = asyncio.create_task(serve_control(proxy, path, allowed_uid=os.getuid()))
        try:
            client = ProxyControlClient(path)
            for _ in range(500):
                with contextlib.suppress(OSError):
                    assert await call(client.reached, RUN) == []
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the control socket did not start on a stale socket")
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not path.exists()  # removed on the way out


@contextlib.contextmanager
def fake_proxy(path: Path, reply: bytes | None) -> Iterator[None]:
    """A control socket that answers one connection with `reply`, or never answers if None."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    done = threading.Event()

    def answer() -> None:
        connection, _ = server.accept()
        with connection:
            connection.recv(65536)
            if reply is None:
                done.wait(5)
            else:
                with contextlib.suppress(OSError):  # the client stops reading a huge reply
                    connection.sendall(reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(5)
        server.close()


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"not json\n",
        b"[" * 30_000 + b"\n",
        b'["ok"]\n',
        b'{"ok": "yes"}\n',
        b'{"ok": true, "result": "api.example.com"}\n',
        b'{"ok": true, "result": [1]}\n',
        b'{"ok": true, "result": []',
        b'{"ok": true, "result": ["' + b"a" * (2 * 1024 * 1024) + b'"]}\n',
    ],
    ids=["closed", "garbage", "deep", "list", "ok-text", "text", "numbers", "no-newline", "huge"],
)
def test_bad_replies_raise_os_error(reply: bytes) -> None:
    with short_directory() as directory:
        path = directory / "p.sock"
        with fake_proxy(path, reply), pytest.raises(OSError, match=r"."):
            ProxyControlClient(path).reached(RUN)


def test_a_proxy_that_never_answers_times_out() -> None:
    with short_directory() as directory:
        path = directory / "p.sock"
        with fake_proxy(path, None), pytest.raises(OSError, match="timed out"):
            ProxyControlClient(path, timeout=0.2).revoke(APP)


def test_bad_timeout_refused() -> None:
    with pytest.raises(ValueError, match="timeout"):
        ProxyControlClient(Path("/run/footfall/proxy.sock"), timeout=0)
