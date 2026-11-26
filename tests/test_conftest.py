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


# Neither address the guard allows: TEST-NET-1, and one on the loopback network that is not
# 127.0.0.1, which never leaves this machine even unguarded.
@pytest.mark.parametrize("elsewhere", [ELSEWHERE, ("127.0.0.2", 9)])
def test_a_datagram_off_this_machine_is_refused(elsewhere: tuple[str, int]) -> None:
    blocked = "network blocked in tests: " + elsewhere[0].replace(".", r"\.")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match=blocked):
            sock.sendto(b"guard-marker", elsewhere)
        with pytest.raises(OSError, match=blocked):
            sock.sendto(b"guard-marker", 0, elsewhere)
        with pytest.raises(OSError, match=blocked):
            sock.sendmsg([b"guard-marker"], [], 0, elsewhere)


def test_a_datagram_to_loopback_is_still_sent() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender,
    ):
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        sender.sendto(b"first-marker", receiver.getsockname())
        sender.sendto(b"second-marker", 0, receiver.getsockname())
        sender.sendmsg([b"third-marker"], [], 0, receiver.getsockname())
        received = [receiver.recv(64) for _ in range(3)]
    assert received == [b"first-marker", b"second-marker", b"third-marker"]


# A name no resolver may answer, and one only this machine's hosts file may, if any does.
@pytest.mark.parametrize("name", ["footfall-guard-check.invalid", "broadcasthost"])
def test_a_name_lookup_is_refused(name: str) -> None:
    for spelt in (name, name.encode()):
        with pytest.raises(socket.gaierror, match=f"name lookups blocked in tests: {name}"):
            socket.getaddrinfo(spelt, 443)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", None])
def test_localhost_and_written_out_addresses_are_still_looked_up(host: str | None) -> None:
    assert socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_loopback_is_still_reachable(method: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            result = getattr(client, method)(server.getsockname())
            assert result in (None, 0)
