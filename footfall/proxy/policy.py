"""Which hosts each container may reach through the egress proxy.

The runner gives the proxy an allow list for each container's address on a run's network, and
revokes it when the container goes. An address another run still holds is never handed over:
it must be revoked first. A list names hosts exactly: no wildcards, no subdomains, and
no addresses. Only ports 80 and 443 are ever allowed. A client address with no list is refused
everything.

The policy also remembers which hosts each run reached, for the run's page. Only hosts on an
allow list are recorded, and only the last `remembered_runs` runs are kept.
"""

import ipaddress
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from footfall.hosts import HostError, validate_host_name

__all__ = [
    "ALLOWED_PORTS",
    "UNKNOWN_CLIENT",
    "Decision",
    "Policy",
    "canonical_address",
    "check_run_id",
]

ALLOWED_PORTS: Final = frozenset({80, 443})
UNKNOWN_CLIENT: Final = "unknown client"
ALLOWED: Final = "allowed"
INVALID_HOST: Final = "not a valid host name"

_RUN_ID: Final = re.compile(r"r_[a-z2-7]{26}")


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a client may reach a host and port; `run_id` is None for an unknown client."""

    allowed: bool
    reason: str
    run_id: str | None


def canonical_address(ip: str) -> str:
    """The canonical text of an IP address, with an IPv4-mapped address as plain IPv4.

    Raises ValueError for anything that is not an address.
    """
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError("not an IP address") from None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def check_run_id(run_id: str) -> str:
    """Return `run_id` if it is a valid run id; raise ValueError if it is not."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("not a valid run id")
    return run_id


class Policy:
    """Allow lists by client address, and the hosts each run reached.

    Every method is safe to call from any thread.
    """

    def __init__(self, *, remembered_runs: int = 1024) -> None:
        if remembered_runs < 1:
            raise ValueError("remembered_runs must be at least 1")
        self._remembered_runs = remembered_runs
        self._lock = threading.Lock()
        self._lists: dict[str, tuple[str, frozenset[str]]] = {}
        self._reached: dict[str, set[str]] = {}  # in the order each run first reached a host

    def allow(self, ip: str, run_id: str, hosts: Iterable[str]) -> None:
        """Give an address its allow list, replacing any list the same run gave it; it never
        merges.

        Raises ValueError, changing nothing, for a bad address, run id or host name, or for an
        address that holds another run's list.
        """
        key = canonical_address(ip)
        check_run_id(run_id)
        if isinstance(hosts, str | bytes):
            raise ValueError("hosts must be a list of host names, not a string")
        names = frozenset(validate_host_name(host) for host in hosts)
        with self._lock:
            current = self._lists.get(key)
            if current is not None and current[0] != run_id:
                raise ValueError(f"{key} already has an allow list for another run")
            self._lists[key] = (run_id, names)

    def revoke(self, ip: str) -> None:
        """Take away an address's allow list. Revoking one that has none is harmless."""
        key = canonical_address(ip)
        with self._lock:
            self._lists.pop(key, None)

    def revoke_run(self, run_id: str) -> None:
        """Take away every allow list that belongs to a run."""
        check_run_id(run_id)
        with self._lock:
            for key in [key for key, (owner, _) in self._lists.items() if owner == run_id]:
                del self._lists[key]

    def decide(self, ip: str, host: str, port: int) -> Decision:
        """Whether the client at `ip` may reach `host` on `port`.

        An unknown client is refused first, so that the caller can tell that case apart and
        wait for the runner's allow. The reason never repeats a host that is not a valid name.
        """
        try:
            key = canonical_address(ip)
        except ValueError:
            return Decision(False, UNKNOWN_CLIENT, None)
        with self._lock:
            entry = self._lists.get(key)
        if entry is None:
            return Decision(False, UNKNOWN_CLIENT, None)
        run_id, names = entry
        if isinstance(port, bool) or port not in ALLOWED_PORTS:
            return Decision(False, f"port {port} is not allowed", run_id)
        try:
            name = validate_host_name(host)
        except HostError:
            return Decision(False, INVALID_HOST, run_id)
        if name not in names:
            return Decision(False, f"{name} is not on this client's allow list", run_id)
        return Decision(True, ALLOWED, run_id)

    def record(self, run_id: str, host: str) -> None:
        """Note that a run reached a host. The caller records only allowed, checked hosts."""
        with self._lock:
            hosts = self._reached.get(run_id)
            if hosts is None:
                if len(self._reached) >= self._remembered_runs:
                    del self._reached[next(iter(self._reached))]  # forget the oldest run
                hosts = self._reached[run_id] = set()
            hosts.add(host)

    def reached(self, run_id: str) -> list[str]:
        """The hosts a run reached, each once, in order."""
        check_run_id(run_id)
        with self._lock:
            return sorted(self._reached.get(run_id, ()))
