"""The runner's Unix-socket protocol, through `serve` and `RunnerClient`."""

import asyncio
import contextlib
import json
import resource
import socket
import stat
import tempfile
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from footfall.runner.client import RunnerClient
from footfall.runner.server import MAX_REQUEST, serve
from footfall.runner.service import DISK_REASON, RunnerError
from tests.conftest import MaskOutput
from tests.runner_fakes import Harness, make_harness

RUN = "r_abcdefghijklmnopqrstuvwxyz"
PASSWORD = "pw-7f3k2m9q4x8z"  # gitleaks:allow


@dataclass
class Live:
    harness: Harness
    client: RunnerClient
    socket: Path


@contextlib.asynccontextmanager
async def running(tmp_path: Path, **settings: Any) -> AsyncIterator[Live]:
    # pytest's tmp_path is too deep for a Unix socket path (about 100 bytes at most on macOS),
    # so the socket lives in a short temporary directory of its own.
    harness = make_harness(tmp_path, **settings)
    with tempfile.TemporaryDirectory(prefix="ff") as directory:
        path = Path(directory) / "r.sock"
        task = asyncio.create_task(serve(harness.service, path))
        try:
            for _ in range(500):
                if path.exists() or task.done():
                    break
                await asyncio.sleep(0.01)
            if task.done():
                task.result()
            yield Live(harness, RunnerClient(path), path)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.fixture
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    async with running(tmp_path) as value:
        yield value


async def exchange(path: Path, data: bytes) -> dict[str, Any]:
    """Send raw bytes and return the decoded reply."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(data)
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        await writer.wait_closed()
    reply: dict[str, Any] = json.loads(line)
    return reply


async def test_round_trip_prepare(live: Live, mask_output: MaskOutput) -> None:
    h, client = live.harness, live.client
    workspace = h.workspace()
    runtime = await client.prepare(
        RUN,
        workspace,
        "python3.13",
        "pip install -r requirements.txt",
        "python3 seed.py",
        ["api.example.com"],
        {"FOOTFALL_USERNAME": "tester", "FOOTFALL_PASSWORD": PASSWORD},
    )
    assert runtime == "runsc"  # what the runner starts containers under
    assert h.engine.names("create") == [
        f"footfall-{RUN}-install-prepare",
        f"footfall-{RUN}-seed-prepare",
    ]
    seed = h.engine.configs[f"footfall-{RUN}-seed-prepare"]
    assert f"FOOTFALL_PASSWORD={PASSWORD}" in seed["Env"]

    address = await client.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    assert address == f"{h.engine.ips[f'footfall-{RUN}-app-anna']}:8000"
    endpoint = await client.start_browsers(RUN, ["fonts.gstatic.com"])
    browsers = h.engine.configs[f"footfall-{RUN}-browsers-browsers"]
    address = h.engine.ips[f"footfall-{RUN}-browsers-browsers"]
    assert endpoint == f"ws://{address}:3000{browsers['Cmd'][-1]}"
    # The orchestrator's output is what a GitHub Actions job reads, so it masks the path there.
    assert f"\n::add-mask::{browsers['Cmd'][-1].lstrip('/')}\n" in mask_output.read()
    assert len(browsers["Cmd"][-1]) == 33
    assert browsers["Cmd"][:2] == ["node", "/opt/footfall/gate.js"]
    h.engine.output["app"] = "listening on 8000\n"
    assert await client.logs(RUN, "anna") == "listening on 8000\n"
    assert await client.status(RUN) is None
    await client.stop(RUN)
    assert h.engine.live == {}
    assert not (h.runs / RUN).exists()


async def test_error_round_trip(live: Live) -> None:
    h, client = live.harness, live.client
    h.engine.exit_codes["install"] = 1
    h.engine.output["install"] = "npm ERR! 404 Not Found - left-pad\n"
    with pytest.raises(RunnerError) as caught:
        await client.prepare(RUN, h.workspace(), "node26", "npm ci", None, [], {})
    assert str(caught.value) == "install failed with exit code 1"
    assert caught.value.log_tail == "npm ERR! 404 Not Found - left-pad\n"

    with pytest.raises(RunnerError, match="run id") as refused:
        await client.stop("../../etc")
    assert refused.value.log_tail == ""


async def test_oversized_request_refused(live: Live) -> None:
    big = json.dumps({"op": "status", "run_id": "r_" + "a" * MAX_REQUEST}).encode() + b"\n"
    reply = await exchange(live.socket, big)
    assert reply["ok"] is False
    assert "64 KiB" in reply["error"]
    with pytest.raises(RunnerError, match="64 KiB"):
        await live.client.prepare(
            RUN, live.harness.workspace(), "python3.13", "x" * 70_000, None, [], {}
        )
    assert live.harness.engine.calls == []


async def test_request_of_exactly_the_limit_is_read(live: Live) -> None:
    request = {"op": "status", "run_id": RUN, "pad": ""}
    size = len(json.dumps(request).encode())
    request["pad"] = "p" * (MAX_REQUEST - size)
    data = json.dumps(request).encode()
    assert len(data) == MAX_REQUEST
    reply = await exchange(live.socket, data + b"\n")
    # The line is read in full and refused for its extra field, not for its size.
    assert reply == {"ok": False, "error": "unexpected field: pad", "log_tail": ""}


async def test_unknown_op_refused(live: Live) -> None:
    reply = await exchange(
        live.socket, b'{"op": "exec", "run_id": "r_abcdefghijklmnopqrstuvwxyz"}\n'
    )
    assert reply == {"ok": False, "error": "unknown op: 'exec'", "log_tail": ""}
    assert live.harness.engine.calls == []


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"not json\n", "request is not valid JSON"),
        (b"[" * 60_000 + b"\n", "request is not valid JSON"),
        (b'["status"]\n', "request must be a JSON object"),
        (b'{"run_id": "r_abcdefghijklmnopqrstuvwxyz"}\n', "unknown op: None"),
        (b'{"op": "stop"}\n', "missing field: run_id"),
        (b'{"op": "stop", "run_id": 7}\n', "run_id must be a string"),
        (
            b'{"op": "logs", "run_id": "r_abcdefghijklmnopqrstuvwxyz", "copy": null}\n',
            "copy must be a string",
        ),
        (
            b'{"op": "start_browsers", "run_id": "r_abcdefghijklmnopqrstuvwxyz", "assets": [1]}\n',
            "assets must be a list of strings",
        ),
        (b'{"op": "status", "run_id": "r_abcdefghijklmnopqrstuvwxyz"', "request must end"),
    ],
)
async def test_malformed_request_refused(live: Live, data: bytes, error: str) -> None:
    if not data.endswith(b"\n"):
        reader, writer = await asyncio.open_unix_connection(str(live.socket))
        writer.write(data)
        writer.write_eof()
        reply = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    else:
        reply = await exchange(live.socket, data)
    assert reply["ok"] is False
    assert reply["error"].startswith(error)
    assert live.harness.engine.calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"port": "8000"},
        {"port": True},
        {"port": 8000.0},
        {"egress": "api.example.com"},
        {"start": None},
    ],
)
async def test_start_app_field_types_checked(live: Live, fields: dict[str, Any]) -> None:
    request: dict[str, Any] = {
        "op": "start_app",
        "run_id": RUN,
        "copy": "anna",
        "start": "python3 serve.py",
        "port": 8000,
        "egress": [],
        **fields,
    }
    reply = await exchange(live.socket, json.dumps(request).encode() + b"\n")
    assert reply["ok"] is False
    assert reply["error"].endswith(("must be a string", "must be an integer", "of strings"))


async def test_prepare_env_must_map_strings(live: Live) -> None:
    request: dict[str, Any] = {
        "op": "prepare",
        "run_id": RUN,
        "workspace": str(live.harness.workspace()),
        "runtime": "python3.13",
        "install": None,
        "seed": None,
        "egress": [],
        "env": {"FOOTFALL_PASSWORD": 12345},
    }
    reply = await exchange(live.socket, json.dumps(request).encode() + b"\n")
    assert reply == {"ok": False, "error": "env must map strings to strings", "log_tail": ""}


async def test_socket_mode_0660(live: Live) -> None:
    mode = live.socket.stat().st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o660


async def test_serve_replaces_a_stale_socket_but_not_a_file(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    with tempfile.TemporaryDirectory(prefix="ff") as directory:
        path = Path(directory) / "r.sock"
        path.write_text("not a socket\n")
        with pytest.raises(FileExistsError):
            await serve(harness.service, path)
        assert path.read_text() == "not a socket\n"

        path.unlink()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(path))
        stale.close()  # leaves the socket file behind, as a crashed runner would
        task = asyncio.create_task(serve(harness.service, path))
        try:
            client = RunnerClient(path)
            for _ in range(500):
                with contextlib.suppress(RunnerError):
                    assert await client.status(RUN) is None
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the runner did not start on a stale socket")
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not path.exists()


async def test_serve_runs_the_disk_watch(tmp_path: Path) -> None:
    async with running(tmp_path, disk_budget=256 * 1024, disk_interval=0.01) as live:
        await live.client.prepare(RUN, live.harness.workspace(), "python3.13", None, None, [], {})
        (live.harness.runs / RUN / "prepared" / "big.bin").write_bytes(b"\x01" * 1024 * 1024)
        for _ in range(500):
            if await live.client.status(RUN) is not None:
                break
            await asyncio.sleep(0.01)
        assert await live.client.status(RUN) == DISK_REASON
        with pytest.raises(RunnerError, match=DISK_REASON):
            await live.client.start_app(RUN, "anna", "python3 serve.py", 8000, [])


async def test_unexpected_failure_is_reported_without_detail(live: Live) -> None:
    live.harness.workspace()
    live.harness.proxy.failures["allow"] = KeyError("internal detail")
    with pytest.raises(RunnerError) as caught:
        await live.client.prepare(
            RUN, live.harness.workspaces / "app", "python3.13", "pip install x", None, [], {}
        )
    assert str(caught.value) == "the runner failed unexpectedly"
    assert "internal detail" not in str(caught.value)
    assert live.harness.engine.live == {}


async def test_client_reports_an_unreachable_runner(tmp_path: Path) -> None:
    client = RunnerClient(tmp_path / "missing.sock")
    with pytest.raises(RunnerError, match="cannot reach the runner"):
        await client.status(RUN)


@pytest.mark.parametrize(
    "data",
    [b"nonsense\n", b'{"ok": "yes"}\n', b'{"ok": true, "result": 5}\n', b"[]\n", b""],
)
async def test_client_refuses_a_garbled_reply(data: bytes) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="ff") as directory:
        path = Path(directory) / "g.sock"
        server = await asyncio.start_unix_server(handle, path=str(path))
        try:
            with pytest.raises(RunnerError, match="runner"):
                await RunnerClient(path).start_app(RUN, "anna", "python3 serve.py", 8000, [])
        finally:
            server.close()
            await server.wait_closed()


async def test_stop_and_status_do_not_queue_behind_prepare(tmp_path: Path) -> None:
    # One worker in the default pool, held by a prepare whose install step never finishes.
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    release = threading.Event()
    async with running(tmp_path) as live:
        h = live.harness
        h.engine.during_wait["install"] = lambda _: release.wait(10)
        preparing = asyncio.create_task(
            live.client.prepare(RUN, h.workspace(), "python3.13", "pip install x", None, [], {})
        )
        for _ in range(500):
            if any(call[0] == "wait" for call in h.engine.calls):
                break
            await asyncio.sleep(0.01)
        try:
            async with asyncio.timeout(3):
                assert await live.client.status(RUN) is None
                await live.client.stop(RUN)
        finally:
            release.set()
        with pytest.raises(RunnerError, match="was stopped"):
            await preparing
        assert h.engine.live == {}
        assert h.engine.networks == {}


async def test_connections_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("footfall.runner.server.MAX_CONNECTIONS", 1)
    async with running(tmp_path) as live:
        reader, writer = await asyncio.open_unix_connection(str(live.socket))  # the only slot
        status = asyncio.create_task(live.client.status(RUN))
        try:
            await asyncio.sleep(0.3)
            assert not status.done()
        finally:
            writer.write_eof()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        async with asyncio.timeout(3):
            assert await status is None


async def test_serve_raises_the_open_file_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 4096))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    async with running(tmp_path) as live:
        assert await live.client.status(RUN) is None
    assert calls == [(resource.RLIMIT_NOFILE, (4096, 4096))]


async def test_serve_starts_when_the_file_limit_cannot_be_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(which: int, limits: tuple[int, int]) -> None:
        raise ValueError("not allowed to raise maximum limit")

    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", refuse)
    async with running(tmp_path) as live:
        assert await live.client.status(RUN) is None
