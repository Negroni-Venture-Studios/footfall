"""Shared fixtures."""

import socket
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from playwright.async_api import Browser, async_playwright

from tests.app_server import AppServer

LOOPBACK = {"127.0.0.1", "::1"}
# Host names the browser tests use for "somewhere else". The browser resolves them to the
# second server, and every other name to nothing, so a browser test reaches no network
# even when the code under test lets a request through.
OTHER_HOSTS = ("evil.test", "assets.test")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the internet: only loopback and Unix sockets may be connected to."""
    real_connect: Callable[[socket.socket, Any], None] = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else address
        if host not in LOOPBACK:
            raise OSError(f"network blocked in tests: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


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
