"""The gate in front of Playwright's run-server, `images/browsers/gate.js`, run with the Node
that Playwright bundles.

run-server tells anyone who asks `GET /json` its secret path, so the browser container's port
is the gate's: it passes on only a WebSocket upgrade for the run's own path, byte for byte, and
answers everything else with an empty 404.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright._impl._driver import compute_driver_executable
from playwright.async_api import Playwright, async_playwright

from footfall.browsers import BrowserConnectError, connect_run_server
from tests.app_server import AppServer

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "images" / "browsers" / "gate.js"
NODE, CLI = (str(part) for part in compute_driver_executable())
PATH = "/c2VjcmV0LXBhdGgtZm9yLXRoaXMtcnVuLW9ubHk"
NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
# Every child process a test starts carries this in its command line, so that a test can
# find any that is left behind.
MARKER = f"footfall-gate-test-{os.getpid()}"


def child(marker: str, script: str) -> list[str]:
    """A node child whose command line names `marker` first, in a comment at the head of its
    script, where no cut of a long command line can reach it."""
    return [NODE, "-e", f"/* {marker} */ {script}"]


KEEP_RUNNING = child(MARKER, "setInterval(() => {}, 1 << 30)")


def keep_running(marker: str, ready: Path) -> list[str]:
    """A child that runs until it is signalled, and writes `ready` once it is running."""
    script = (
        f"require('fs').writeFileSync({json.dumps(str(ready))}, ''); "
        "setInterval(() => {}, 1 << 30)"
    )
    return child(marker, script)


def ignores_term(marker: str, ready: Path) -> list[str]:
    """A child that ignores SIGTERM, as a slow run-server would, and writes `ready` once its
    handler is in place."""
    script = (
        "process.on('SIGTERM', () => {}); "
        f"require('fs').writeFileSync({json.dumps(str(ready))}, ''); "
        "setInterval(() => {}, 1 << 30)"
    )
    return child(marker, script)


def ready_soon(ready: Path, within: float = 5) -> bool:
    deadline = time.monotonic() + within
    while not ready.exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


PS = shutil.which("ps") or "ps"


def running(marker: str) -> list[str]:
    """The command lines of every process still running that carry `marker`.

    `-A` is POSIX for every process. procps reads `-axo` as processes with a terminal,
    which leaves out everything a CI runner starts. procps also cuts each command line at
    the screen width, 80 columns when the output is not a terminal, which on CI is less
    than the path to node; `-ww` means no limit, there and on macOS.

    The gate and its keeper carry the child's command line, marker included, in their own,
    so lines naming gate.js are left out: only the child itself counts.
    """
    listed = subprocess.run([PS, "-A", "-ww", "-o", "command="], capture_output=True, check=True)
    lines = listed.stdout.decode().splitlines()
    return [line for line in lines if marker in line and "gate.js" not in line]


def appears(marker: str, within: float) -> bool:
    """Whether a process carrying `marker` is running within `within` seconds."""
    deadline = time.monotonic() + within
    while not running(marker):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def gone(marker: str, within: float) -> bool:
    deadline = time.monotonic() + within
    while running(marker):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def upgrade(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\nHost: 10.89.0.12:3000\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"  # gitleaks:allow
        "Sec-WebSocket-Version: 13\r\nx-playwright-launch-options: {}\r\n\r\n"
    ).encode()


@dataclass
class Upstream:
    """Stands in for run-server: records every byte it is sent and answers with its own."""

    port: int
    received: bytearray = field(default_factory=bytearray)
    connections: int = 0
    reply: bytes = b"HTTP/1.1 101 Switching Protocols\r\n\r\n\x81\x05hello"


async def start_upstream(port: int = 0) -> tuple[Upstream, asyncio.Server]:
    record = Upstream(port)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        record.connections += 1
        writer.write(record.reply)
        await writer.drain()
        while chunk := await reader.read(65536):
            record.received += chunk
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    record.port = server.sockets[0].getsockname()[1]
    return record, server


class Gate:
    def __init__(self, upstream: int, child: list[str]) -> None:
        self.port = free_port()
        self.process = subprocess.Popen(
            [
                NODE,
                str(GATE),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--upstream",
                str(upstream),
                "--path",
                PATH,
                "--",
                *child,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.process.stdout is not None
        line = self.process.stdout.readline().decode()
        assert line.startswith("gate listening"), line + self.process.stderr.read().decode()  # type: ignore[union-attr]
        assert PATH not in line

    def stop(self) -> None:
        """Stop the gate: terminate it, then kill it if it has not gone in a few seconds,
        and always close its pipes."""
        try:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        finally:
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is not None:
                    pipe.close()


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    record, server = await start_upstream()
    try:
        yield record
    finally:
        server.close()


@pytest.fixture
def gate(upstream: Upstream) -> Iterator[Gate]:
    running = Gate(upstream.port, KEEP_RUNNING)
    try:
        yield running
    finally:
        running.stop()


async def exchange(port: int, data: bytes, wait: float = 3.0) -> bytes:
    """Send `data`, then read until the gate closes the connection or `wait` passes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    answer = b""
    with contextlib.suppress(TimeoutError, ConnectionError):
        async with asyncio.timeout(wait):
            while chunk := await reader.read(65536):
                answer += chunk
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()
    return answer


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"GET /json HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
        f"GET {PATH} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),  # the right path, but no upgrade
        f"POST {PATH} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n\r\n".encode(),
        upgrade("/json"),
        upgrade("/"),
        upgrade(PATH[:-1]),  # a prefix of the token
        upgrade(PATH + "x"),
        upgrade(PATH[:-1] + ("A" if PATH[-1] != "A" else "B")),  # same length, one byte off
        upgrade(PATH + "?launch-options={}"),
        upgrade(PATH.upper()),
    ],
)
async def test_the_gate_refuses_all_but_the_runs_own_upgrade(
    gate: Gate, upstream: Upstream, request_bytes: bytes
) -> None:
    assert await exchange(gate.port, request_bytes) == NOT_FOUND
    assert upstream.connections == 0
    assert upstream.received == b""


async def test_the_gate_refuses_an_endless_head(gate: Gate, upstream: Upstream) -> None:
    answer = await exchange(gate.port, b"GET " + b"a" * 70_000)
    assert answer in (NOT_FOUND, b"")
    assert upstream.connections == 0


async def test_the_right_path_is_forwarded_byte_for_byte(gate: Gate, upstream: Upstream) -> None:
    sent = upgrade(PATH) + b"\x81\x85early frame bytes"
    answer = await exchange(gate.port, sent, wait=1.0)
    assert answer == upstream.reply
    # Byte for byte but the Host header: run-server listens on loopback and, against DNS
    # rebinding, refuses with 403 a request whose Host is not loopback, which the
    # orchestrator's, naming the container's address, never is (found in the isolation
    # workflow).
    expected = sent.replace(b"Host: 10.89.0.12:3000", f"Host: 127.0.0.1:{upstream.port}".encode())
    assert bytes(upstream.received) == expected
    assert upstream.connections == 1


async def test_every_host_header_is_replaced_by_one_for_loopback(
    gate: Gate, upstream: Upstream
) -> None:
    sent = upgrade(PATH).replace(
        b"Host: 10.89.0.12:3000\r\n", b"host:evil.test\r\nHOST: 10.0.0.1\r\n"
    )
    await exchange(gate.port, sent, wait=1.0)
    head = bytes(upstream.received).split(b"\r\n\r\n")[0].split(b"\r\n")
    hosts = [line for line in head if line.lower().startswith(b"host")]
    assert hosts == [f"Host: 127.0.0.1:{upstream.port}".encode()]
    assert head[0] == f"GET {PATH} HTTP/1.1".encode()


async def test_the_gate_waits_for_run_server_to_start() -> None:
    port = free_port()
    gate = Gate(port, KEEP_RUNNING)
    try:
        exchanged = asyncio.create_task(exchange(gate.port, upgrade(PATH), wait=3.0))
        await asyncio.sleep(0.6)
        record, server = await start_upstream(port)
        try:
            assert await exchanged == record.reply
            loopback = f"Host: 127.0.0.1:{port}".encode()
            assert bytes(record.received) == upgrade(PATH).replace(
                b"Host: 10.89.0.12:3000", loopback
            )
        finally:
            server.close()
    finally:
        gate.stop()


async def test_a_slow_head_is_closed_by_its_deadline(gate: Gate, upstream: Upstream) -> None:
    # Ten seconds for the whole head, however it trickles in: one byte every 3 s never resets it.
    reader, writer = await asyncio.open_connection("127.0.0.1", gate.port)
    started = time.monotonic()
    closed_after = None
    try:
        async with asyncio.timeout(16):
            for byte in upgrade(PATH):
                writer.write(bytes([byte]))
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
                try:
                    async with asyncio.timeout(3):
                        if await reader.read(1) == b"":
                            closed_after = time.monotonic() - started
                            break
                except TimeoutError:
                    continue
    finally:
        writer.close()
    assert closed_after is not None
    assert 9 < closed_after < 11.5
    assert upstream.connections == 0


async def test_held_heads_from_one_address_cannot_crowd_out_the_run(
    gate: Gate, upstream: Upstream
) -> None:
    held = []
    try:
        for _ in range(64):
            reader, writer = await asyncio.open_connection("127.0.0.1", gate.port)
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n")
            await writer.drain()
            held.append((reader, writer))
        answer = await exchange(gate.port, upgrade(PATH), wait=1.0)
        assert answer == upstream.reply
        assert upstream.connections == 1
        # Only a few heads from one address are kept waiting; the oldest went first.
        still_open = 0
        for reader, _ in held:
            try:
                async with asyncio.timeout(0.05):
                    if await reader.read(1) != b"":
                        still_open += 1
            except TimeoutError:
                still_open += 1
            except ConnectionError:
                pass
        assert still_open <= 8
    finally:
        for _, writer in held:
            writer.close()


def test_a_stopped_gate_leaves_no_child_behind(tmp_path: Path) -> None:
    marker = f"{MARKER}-stopped"
    ready = tmp_path / "ready"
    gate = Gate(free_port(), keep_running(marker, ready))
    try:
        assert ready_soon(ready)
        assert appears(marker, within=5)
        started = time.monotonic()
        gate.process.terminate()
        gate.process.wait(timeout=8)
        assert time.monotonic() - started < 6
        assert gone(marker, within=2)
    finally:
        gate.stop()


def test_a_child_that_ignores_sigterm_is_killed_within_a_bound(tmp_path: Path) -> None:
    # run-server may take its time to close its browsers, longer on some machines than on
    # others; the gate waits a few seconds, then kills its child's whole group, and exits.
    marker = f"{MARKER}-stubborn"
    ready = tmp_path / "ready"
    gate = Gate(free_port(), ignores_term(marker, ready))
    try:
        assert ready_soon(ready)  # the child ignores SIGTERM from here on
        assert appears(marker, within=5)
        started = time.monotonic()
        gate.process.terminate()
        gate.process.wait(timeout=10)
        took = time.monotonic() - started
        assert 2.5 <= took < 7  # the grace period, then SIGKILL
        assert gone(marker, within=2)
    finally:
        gate.stop()


def test_a_killed_gate_takes_its_child_with_it(tmp_path: Path) -> None:
    marker = f"{MARKER}-orphan"
    ready = tmp_path / "ready"
    gate = Gate(free_port(), keep_running(marker, ready))
    try:
        assert ready_soon(ready)
        assert appears(marker, within=5)
        gate.process.kill()
        gate.process.wait(timeout=5)
        assert gone(marker, within=6)
    finally:
        gate.stop()


def test_the_keeper_listens_for_its_stop_before_it_starts_the_child() -> None:
    # A SIGTERM between starting the child and listening for the signal would kill the keeper
    # and leave the child, in a group of its own, running. The window is a few microseconds
    # in one synchronous tick, too small for a timing test to hit, so the order is read from
    # the source.
    source = GATE.read_text()
    keep = source[source.index("function keep(") : source.index("function main(")]
    spawned = keep.index("childProcess.spawn(")
    for handler in (
        'process.on("SIGTERM"',
        'process.on("SIGINT"',
        'process.stdin.on("end"',
        'process.stdin.on("error"',
    ):
        assert keep.index(handler) < spawned, handler
    # Stopped before there is a child, the keeper exits at once.
    assert re.search(r"if \(child === null\) process\.exit\(", keep)


def test_the_gate_exits_when_run_server_does() -> None:
    marker = f"{MARKER}-exits"
    gate = Gate(free_port(), child(marker, "setTimeout(() => process.exit(3), 300)"))
    try:
        assert gate.process.wait(timeout=10) == 3
    finally:
        gate.stop()
    assert gone(marker, within=2)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--host", "127.0.0.1", "--port", "3000", "--upstream", "3001", "--path", PATH],
        ["--host", "127.0.0.1", "--port", "3000", "--upstream", "3001", "--path", "/short", "--"],
        ["--port", "3000", "--upstream", "3001", "--path", PATH, "--", "true"],
    ],
)
def test_the_gate_refuses_a_bad_command_line(arguments: list[str]) -> None:
    done = subprocess.run(
        [NODE, str(GATE), *arguments], capture_output=True, timeout=10, check=False
    )
    assert done.returncode == 2
    assert PATH.encode() not in done.stderr + done.stdout


# ---------------------------------------------------------------- the real run-server


@pytest.fixture
async def playwright() -> AsyncIterator[Playwright]:
    async with async_playwright() as running:
        yield running


@pytest.fixture
def run_server_behind_gate() -> Iterator[int]:
    """Playwright's own run-server behind the gate, both as the runner starts them."""
    inner = free_port()
    command = [NODE, CLI, "run-server", "--port", str(inner), "--host", "127.0.0.1"]
    gate = Gate(inner, [*command, "--path", PATH])
    try:
        yield gate.port
    finally:
        gate.stop()
    # Neither the gate nor run-server, both of which carry the path, is left running.
    assert gone(PATH, within=5)


async def test_a_real_run_server_is_reached_through_the_gate_alone(
    run_server_behind_gate: int, playwright: Playwright, app_server: AppServer
) -> None:
    port = run_server_behind_gate
    assert await exchange(port, b"GET /json HTTP/1.1\r\nHost: x\r\n\r\n") == NOT_FOUND
    with pytest.raises(BrowserConnectError):
        await connect_run_server(playwright, "chromium", f"ws://127.0.0.1:{port}/", wait=1)
    app_server.page("/", "<!doctype html><title>Plain</title><h1>Plain</h1>")
    browser = await connect_run_server(playwright, "chromium", f"ws://127.0.0.1:{port}{PATH}")
    try:
        page = await browser.new_page()
        await page.goto(app_server.origin + "/")
        assert await page.title() == "Plain"
    finally:
        await asyncio.wait_for(browser.close(), 30)
