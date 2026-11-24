"""Shared fixtures."""

import socket
from collections.abc import Callable
from typing import Any

import pytest

LOOPBACK = {"127.0.0.1", "::1"}


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
