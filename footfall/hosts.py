"""Which outside hosts an app may name, and whether an address is public.

The manifest check uses `validate_host_name`. The egress proxy uses `resolve_public`, so
a sandboxed app can reach public internet addresses only: never the droplet, its
metadata service or a private network.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable

__all__ = [
    "HostError",
    "Resolver",
    "is_public_address",
    "resolve_public",
    "system_resolver",
    "validate_host_name",
]


class HostError(ValueError):
    """A host name is not acceptable, or it does not lead to a public address."""


type Resolver = Callable[[str, int], Awaitable[list[str]]]

_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_MAX_NAME = 253


def _is_address_literal(name: str) -> bool:
    """Whether a resolver would take this name for an IP address.

    `ipaddress` knows only the dotted-quad and IPv6 forms; `inet_aton` also takes the
    short, octal and hexadecimal IPv4 forms (such as 127.1) that a resolver passes on.
    """
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return True
    try:
        socket.inet_aton(name)
    except OSError:
        return False
    return True


def validate_host_name(name: str) -> str:
    """Return the lower-case ASCII (IDNA) form of a host name, or raise `HostError`.

    A name needs at least one dot, labels of 1 to 63 letters, digits and hyphens that
    neither start nor end with a hyphen, at most 253 characters in all, and must not be
    an IP address in any form.
    """
    try:
        ascii_name = name.lower().encode("idna").decode("ascii")
    except UnicodeError as error:
        raise HostError(f"{name!r} is not a valid host name") from error
    labels = ascii_name.split(".")
    if (
        len(ascii_name) > _MAX_NAME
        or len(labels) < 2
        or not all(_LABEL.fullmatch(label) for label in labels)
        or _is_address_literal(ascii_name)
    ):
        raise HostError(f"{name!r} is not a valid host name")
    return ascii_name


# IPv6 ranges that carry an IPv4 address inside, which `is_global` can pass while the
# address within is private: NAT64 (global and local-use), 6to4 and Teredo.
_TRANSLATED = tuple(
    ipaddress.ip_network(network)
    for network in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)
_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")


def is_public_address(address: str) -> bool:
    """Whether an IP address is on the public internet. Unparseable text is not.

    It fails closed. An IPv6 address must lie in global unicast 2000::/3, be global, and
    be outside the translated ranges; that refuses the IPv4-compatible and IPv4-translated
    ranges, site-local and multicast addresses, which `is_global` does not catch. An
    IPv4-mapped address is judged by the IPv4 address inside it. An IPv4 address must be
    global and neither multicast nor reserved.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        return (
            ip in _GLOBAL_UNICAST
            and ip.is_global
            and not any(ip in network for network in _TRANSLATED)
        )
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve a name with the system resolver, returning every address found."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def resolve_public(host: str, port: int, resolver: Resolver = system_resolver) -> str:
    """Resolve a host and return its first address, only if every address is public.

    Raises `HostError` if the name does not resolve, or if any address it resolves to is
    not public. The caller connects to the address returned, so a name that resolves
    differently a moment later cannot slip past (DNS rebinding).
    """
    try:
        addresses = await resolver(host, port)
    except OSError as error:
        raise HostError(f"{host} does not resolve") from error
    if not addresses:
        raise HostError(f"{host} does not resolve")
    for address in addresses:
        if not is_public_address(address):
            raise HostError(f"{host} resolves to {address}, which is not a public address")
    return addresses[0].split("%", 1)[0]
