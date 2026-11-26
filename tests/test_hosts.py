"""Host-name rules and the public-address check."""

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from footfall.hosts import (
    HostError,
    is_public_address,
    pooled_resolver,
    resolve_public,
    system_resolver,
    validate_host_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "tile.openstreetmap.org",
        "Example.COM",
        "bücher.example",
        # Numbers are fine anywhere but the last label, and a label merely starting 0x is no number.
        "123.example.com",
        "0x7f.example.com",
        "example.0xg1",
        "example.x123",
    ],
)
def test_valid_names(name: str) -> None:
    assert validate_host_name(name) == name.lower().encode("idna").decode()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "localhost",
        "example",
        "*.example.com",
        "10.0.0.1",
        "[::1]",
        "169.254.169.254",
        "exa mple.com",
        "example.com:443",
        "https://example.com",
        "a" * 254,
        "-x.example.com",
        "x-.example.com",
        "a..example.com",
        "127.1",
        "0x7f.0x1",
        "2130706433.",
        ("a" * 63 + ".") * 4 + "com",
        # Forms a resolver reads as an IPv4 address: a final label that is a number.
        "0x7f.1",
        "017.0.0.1",
        "10.0.0.0x1",
        "a.0x",
        "example.123",
        "example.0xff",
        "example.0XFF",
    ],
)
def test_invalid_names(name: str) -> None:
    with pytest.raises(HostError):
        validate_host_name(name)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fe80::1%eth0", False),
        ("64:ff9b::a00:1", False),
        ("64:ff9b:1::1", False),
        ("2002:a00:1::", False),
        ("2001::1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:93.184.215.14", True),
        ("2606:4700::1111", True),
        ("::10.0.0.1", False),
        ("::127.0.0.1", False),
        ("::a9fe:a9fe", False),
        ("::ffff:0:10.0.0.1", False),
        ("fec0::1", False),
        ("224.0.0.1", False),
        ("239.1.1.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("ff02::1", False),
        ("ff0e::1", False),
        ("4000::1", False),
        # Special-purpose ranges that `is_global` passes but that are no public host.
        ("192.88.99.1", False),  # the deprecated 6to4 relay anycast
        ("192.88.99.255", False),
        ("::ffff:192.88.99.1", False),
        ("192.0.0.9", False),  # Port Control Protocol anycast
        ("192.0.0.8", False),
        ("192.0.0.10", False),  # TURN anycast
        ("2001:1::1", False),  # Port Control Protocol anycast
        ("2001:1::2", False),  # TURN anycast
        ("::ffff:192.0.0.10", False),
        ("2001:20::1", False),  # ORCHIDv2
        ("2001:2f:ffff:ffff::1", False),
        # AS112 (RFC 7534, RFC 7535) and AMT (RFC 7450) anycast: the first and last address.
        ("192.31.196.0", False),  # AS112-v4
        ("192.31.196.255", False),
        ("192.52.193.0", False),  # AMT
        ("192.52.193.255", False),
        ("192.175.48.0", False),  # Direct Delegation AS112 Service
        ("192.175.48.255", False),
        ("::ffff:192.175.48.1", False),
        ("2001:3::", False),  # AMT
        ("2001:3:ffff:ffff:ffff:ffff:ffff:ffff", False),
        ("2001:4:112::", False),  # AS112-v6
        ("2001:4:112:ffff:ffff:ffff:ffff:ffff", False),
        ("2620:4f:8000::", False),  # Direct Delegation AS112 Service
        ("2620:4f:8000:ffff:ffff:ffff:ffff:ffff", False),
        # Their neighbours stay public.
        ("192.88.98.255", True),
        ("192.88.100.1", True),
        ("2001:30::1", True),
        ("192.31.195.255", True),
        ("192.31.197.0", True),
        ("192.52.192.255", True),
        ("192.52.194.0", True),
        ("192.175.47.255", True),
        ("192.175.49.0", True),
        ("2620:4f:7fff:ffff:ffff:ffff:ffff:ffff", True),
        ("2620:4f:8001::", True),
        # 2001:3::/32 and 2001:4:112::/48 lie in the IETF's 2001::/23, whose other addresses
        # are not global, so the nearest public neighbour is past it.
        ("2001:200::1", True),
    ],
)
def test_public_addresses(address: str, public: bool) -> None:
    assert is_public_address(address) is public


def test_unparseable_address_is_not_public() -> None:
    assert is_public_address("not-an-address") is False


async def test_resolve_public_refuses_any_private_answer() -> None:
    async def mixed(host: str, port: int) -> list[str]:
        return ["93.184.215.14", "10.0.0.5"]

    with pytest.raises(HostError, match="not a public address"):
        await resolve_public("example.com", 443, resolver=mixed)


async def test_resolve_public_returns_the_first_address() -> None:
    async def answers(host: str, port: int) -> list[str]:
        assert (host, port) == ("example.com", 443)
        return ["93.184.215.14", "2606:4700::1111"]

    assert await resolve_public("example.com", 443, resolver=answers) == "93.184.215.14"


async def test_resolve_public_refuses_an_empty_answer() -> None:
    async def nothing(host: str, port: int) -> list[str]:
        return []

    with pytest.raises(HostError, match="does not resolve"):
        await resolve_public("example.com", 443, resolver=nothing)


async def test_resolve_public_refuses_a_failed_lookup() -> None:
    async def failing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    with pytest.raises(HostError, match="does not resolve"):
        await resolve_public("example.com", 443, resolver=failing)


async def test_system_resolver_resolves_localhost_locally() -> None:
    addresses = await system_resolver("localhost", 80)
    assert addresses
    assert all(not is_public_address(address) for address in addresses)


async def test_pooled_resolver_looks_up_on_its_own_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[str] = []

    def lookup(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        threads.append(threading.current_thread().name)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with ThreadPoolExecutor(1, thread_name_prefix="own-pool") as pool:
        assert await pooled_resolver(pool)("example.com", 443) == ["93.184.215.14"]
    assert len(threads) == 1
    assert threads[0].startswith("own-pool")


async def test_resolve_public_refuses_localhost() -> None:
    with pytest.raises(HostError, match="not a public address"):
        await resolve_public("localhost", 80)


@pytest.mark.parametrize("name", ["127.1", "0x7f.0x1", "017.0.0.1", "example.123"])
def test_address_forms_are_refused_without_asking_libc(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is Footfall's own, so it holds whatever the C library accepts."""

    def accepts_nothing(text: str) -> bytes:
        raise OSError("illegal IP address string passed to inet_aton")

    monkeypatch.setattr("socket.inet_aton", accepts_nothing)
    with pytest.raises(HostError):
        validate_host_name(name)
