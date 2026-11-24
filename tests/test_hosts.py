"""Host-name rules and the public-address check."""

import pytest

from footfall.hosts import (
    HostError,
    is_public_address,
    resolve_public,
    system_resolver,
    validate_host_name,
)


@pytest.mark.parametrize("name", ["tile.openstreetmap.org", "Example.COM", "bücher.example"])
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


async def test_resolve_public_refuses_localhost() -> None:
    with pytest.raises(HostError, match="not a public address"):
        await resolve_public("localhost", 80)
