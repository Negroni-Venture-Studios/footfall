"""The egress proxy's control socket, through `serve_control` and `ProxyControlClient`.

The client is synchronous, as the runner's `ProxyControl` is, so these tests call it from a
thread while the server runs on the event loop.
"""

import asyncio
import contextlib
import json
import socket
import stat
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from footfall.proxy.control import (
    MAX_REQUEST,
    ProxyControlClient,
    ProxyRefused,
    serve_control,
)
from footfall.proxy.policy import Policy
from footfall.runner.service import ProxyControl

RUN = "r_abcdefghijklmnopqrstuvwxyz"
OTHER = "r_" + "q" * 26
APP = "10.89.0.10"
BROWSERS = "10.89.0.11"


@dataclass
class Live:
    policy: Policy
    client: ProxyControlClient
    socket: Path


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


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    policy = Policy()
    with short_directory() as directory:
        path = directory / "p.sock"
        task = asyncio.create_task(serve_control(policy, path))
        try:
            await wait_for_socket(path, task)
            yield Live(policy, ProxyControlClient(path), path)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


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


async def test_allow_replaces_the_earlier_list(live: Live) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    await call(live.client.allow, APP, OTHER, ["fonts.gstatic.com"])
    assert not live.policy.decide(APP, "api.example.com", 443).allowed
    assert live.policy.decide(APP, "fonts.gstatic.com", 443).run_id == OTHER


async def test_revoke_run_round_trip(live: Live) -> None:
    await call(live.client.allow, APP, RUN, ["api.example.com"])
    await call(live.client.allow, BROWSERS, RUN, ["fonts.gstatic.com"])
    await call(live.client.allow, "10.90.0.10", OTHER, ["api.example.com"])
    await call(live.client.revoke_run, RUN)
    assert live.policy.decide(APP, "api.example.com", 443).run_id is None
    assert live.policy.decide(BROWSERS, "fonts.gstatic.com", 443).run_id is None
    assert live.policy.decide("10.90.0.10", "api.example.com", 443).allowed


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


async def test_serve_replaces_a_stale_socket_but_not_a_file() -> None:
    policy = Policy()
    with short_directory() as directory:
        path = directory / "p.sock"
        path.write_text("not a socket\n")
        with pytest.raises(FileExistsError):
            await serve_control(policy, path)
        assert path.read_text() == "not a socket\n"

        path.unlink()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()  # leaves the socket file behind, as a crashed proxy would
        task = asyncio.create_task(serve_control(policy, path))
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
