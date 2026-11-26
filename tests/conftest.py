"""Shared fixtures."""

import ipaddress
import os
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from playwright.async_api import Browser, async_playwright

from tests.app_server import AppServer
from tests.fake_api import Api

LOOPBACK = {"127.0.0.1", "::1"}
# The names a test may look up, which the system answers from its hosts file.
LOOPBACK_NAMES = {"", "localhost"}
# Host names the browser tests use for "somewhere else". The browser resolves them to the
# second server, and every other name to nothing, so a browser test reaches no network
# even when the code under test lets a request through.
OTHER_HOSTS = ("evil.test", "assets.test")


def _check(sock: socket.socket, address: Any) -> None:
    """Raise unless the address is a Unix socket or loopback."""
    if sock.family == socket.AF_UNIX:
        return
    host = address[0] if isinstance(address, tuple) else address
    if host not in LOOPBACK:
        raise OSError(f"network blocked in tests: {host}")


def _check_name(host: Any) -> None:
    """Raise unless looking up `host` needs no resolver: no name at all, localhost, or an
    address written out, which is checked again when it is connected or sent to."""
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host or "")
    if name.rstrip(".").lower() in LOOPBACK_NAMES:
        return
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        raise socket.gaierror(socket.EAI_NONAME, f"name lookups blocked in tests: {name}") from None


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the internet from the test process: only loopback and Unix sockets
    may be connected or sent to, and only localhost and written-out addresses looked up.

    Four ways a socket reaches another are guarded: `connect`, and `connect_ex`, which
    returns an error number instead of raising, and `sendto` and `sendmsg`, which send a
    datagram without connecting. A refused `connect_ex` raises all the same, so that no
    caller mistakes the refusal for an ordinary failure to connect. Of the ways to look a
    name up, `getaddrinfo` is guarded, which asyncio and `socket.create_connection` use; a
    refused lookup raises `socket.gaierror`, as a name that does not resolve would. The
    older `gethostbyname` and its kin are not guarded, and nor are the programs the tests
    start, such as git and the browsers; the browsers are given name rules of their own.
    """
    real_connect: Callable[[socket.socket, Any], None] = socket.socket.connect
    real_connect_ex: Callable[[socket.socket, Any], int] = socket.socket.connect_ex
    real_sendto: Callable[..., int] = socket.socket.sendto
    real_sendmsg: Callable[..., int] = socket.socket.sendmsg
    real_getaddrinfo: Callable[..., Any] = socket.getaddrinfo

    def guarded(self: socket.socket, address: Any) -> None:
        _check(self, address)
        return real_connect(self, address)

    def guarded_ex(self: socket.socket, address: Any) -> int:
        _check(self, address)
        return real_connect_ex(self, address)

    def guarded_sendto(self: socket.socket, data: Any, *rest: Any) -> int:
        if rest:  # sendto(data, address) or sendto(data, flags, address)
            _check(self, rest[-1])
        return real_sendto(self, data, *rest)

    def guarded_sendmsg(self: socket.socket, *arguments: Any, **options: Any) -> int:
        address = options.get("address", arguments[3] if len(arguments) > 3 else None)
        if address is not None:
            _check(self, address)
        return real_sendmsg(self, *arguments, **options)

    def guarded_getaddrinfo(host: Any, *arguments: Any, **options: Any) -> Any:
        _check_name(host)
        return real_getaddrinfo(host, *arguments, **options)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", guarded_sendmsg)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture
def app_server() -> Iterator[AppServer]:
    """The app under test, on 127.0.0.1."""
    with AppServer() as server:
        yield server


@pytest.fixture
def other_server() -> Iterator[AppServer]:
    """Every other host: the browser's names in OTHER_HOSTS all lead here."""
    with AppServer() as server:
        yield server


@pytest.fixture
async def loopback_browser(other_server: AppServer) -> AsyncIterator[Browser]:
    """A local Chromium whose name lookups never leave this machine.

    The names in OTHER_HOSTS go to `other_server`, 127.0.0.1 and localhost stay as they
    are, and every other name fails to resolve.
    """
    rules = [f"MAP {host} 127.0.0.1:{other_server.port}" for host in OTHER_HOSTS]
    rules += ["MAP * ~NOTFOUND", "EXCLUDE 127.0.0.1", "EXCLUDE localhost"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            args=[f"--host-resolver-rules={', '.join(rules)}"]
        )
        try:
            yield browser
        finally:
            await browser.close()


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    """A stand-in for the Messages API; see `tests/fake_api.py`."""
    fake = Api()
    try:
        yield fake
    finally:
        await fake.close()


class MaskOutput:
    """Stands in for a GitHub Actions job's output: a pipe that `footfall.actions` writes its
    mask commands to, as it writes them to the pipe a job gives a step."""

    def __init__(self) -> None:
        self.reader, self.writer = os.pipe()
        os.set_blocking(self.reader, False)

    def read(self) -> str:
        data = b""
        while True:
            try:
                chunk = os.read(self.reader, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            data += chunk
        return data.decode()

    def close(self) -> None:
        os.close(self.reader)
        os.close(self.writer)


@pytest.fixture
def mask_output(monkeypatch: pytest.MonkeyPatch) -> Iterator[MaskOutput]:
    """Mask commands go to a pipe of the test's own, as in a job, and GITHUB_ACTIONS is set."""
    output = MaskOutput()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr("footfall.actions._STREAMS", (output.writer,))
    try:
        yield output
    finally:
        output.close()


@pytest.fixture
def no_umask() -> Iterator[None]:
    """A umask of 0 for the test, so that a file's mode is the one Footfall asked for."""
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)
