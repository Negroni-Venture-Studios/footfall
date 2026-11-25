"""The suite's own network guard, in `tests/conftest.py`."""

import socket

import pytest

# TEST-NET-1 (RFC 5737): documentation only, so nothing would answer even if a packet left.
ELSEWHERE = ("192.0.2.1", 9)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_a_connection_off_this_machine_is_refused(method: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        with pytest.raises(OSError, match=r"network blocked in tests: 192\.0\.2\.1"):
            getattr(sock, method)(ELSEWHERE)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_loopback_is_still_reachable(method: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            result = getattr(client, method)(server.getsockname())
            assert result in (None, 0)
