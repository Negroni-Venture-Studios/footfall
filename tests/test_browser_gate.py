"""The gate in front of Playwright's run-server, `images/browsers/gate.js`, run with the Node
that Playwright bundles.

run-server tells anyone who asks `GET /json` its secret path, so the browser container's port
is the gate's: it passes on only a WebSocket upgrade for the run's own path, byte for byte, and
answers everything else with an empty 404.
"""

import asyncio
import contextlib
import socket
import subprocess
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
KEEP_RUNNING = [NODE, "-e", "setInterval(() => {}, 1 << 30)"]


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
        if self.process.poll() is None:
            self.process.terminate()
        self.process.communicate(timeout=10)


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
    assert bytes(upstream.received) == sent
    assert upstream.connections == 1


async def test_the_gate_waits_for_run_server_to_start() -> None:
    port = free_port()
    gate = Gate(port, KEEP_RUNNING)
    try:
        exchanged = asyncio.create_task(exchange(gate.port, upgrade(PATH), wait=3.0))
        await asyncio.sleep(0.6)
        record, server = await start_upstream(port)
        try:
            assert await exchanged == record.reply
            assert bytes(record.received) == upgrade(PATH)
        finally:
            server.close()
    finally:
        gate.stop()


def test_the_gate_exits_when_run_server_does() -> None:
    gate = Gate(free_port(), [NODE, "-e", "setTimeout(() => process.exit(3), 300)"])
    assert gate.process.wait(timeout=10) == 3
    gate.stop()


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
