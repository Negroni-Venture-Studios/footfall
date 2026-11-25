"""The fixed settings every Footfall container starts with.

`container_config` is the single place that decides how a container is sandboxed, and its output
is the body of Docker Engine's `POST /containers/create`. gVisor (`runsc`) puts a user-space
kernel between the code under test and the host kernel; dropping every capability, forbidding
privilege gain and refusing privileged mode stop that code acquiring powers it was not given.
A read-only root with small tmpfs scratch areas keeps it from altering its image, and the memory,
CPU and process limits stop a runaway app from starving the host. The internal network and the
egress proxy confine what it can reach, and the environment allow-list keeps secrets out.

The function is pure: it reads neither the environment nor the filesystem.
"""

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import urlsplit

Role = Literal["install", "seed", "app", "browsers"]

APP_UID: Final = 10010

_GIB: Final = 1024**3
_MIB: Final = 1024**2


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource ceilings for one role."""

    memory_bytes: int
    nano_cpus: int
    pids: int


LIMITS: Final[Mapping[Role, Limits]] = MappingProxyType(
    {
        "install": Limits(memory_bytes=_GIB, nano_cpus=1_000_000_000, pids=256),
        "seed": Limits(memory_bytes=512 * _MIB, nano_cpus=1_000_000_000, pids=256),
        "app": Limits(memory_bytes=512 * _MIB, nano_cpus=1_000_000_000, pids=256),
        "browsers": Limits(memory_bytes=2 * _GIB, nano_cpus=2_000_000_000, pids=1024),
    }
)

# The sandbox in one place: every container gets exactly these, whatever its role. The
# table cannot be changed, and `container_config` copies all of it, so a setting added here
# reaches every container. Docker's default seccomp profile applies because nothing here
# turns it off.
_HARDENING: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "CapDrop": ("ALL",),
        "SecurityOpt": ("no-new-privileges:true",),
        "ReadonlyRootfs": True,
        "Privileged": False,
        # Docker's init is the container's first process: it reaps orphaned processes, such
        # as a browser's, and passes signals on.
        "Init": True,
        # The container's own IPC namespace, which no other container can join.
        "IpcMode": "private",
        # A crash writes no core file, which could hold what the process held.
        "Ulimits": (MappingProxyType({"Name": "core", "Soft": 0, "Hard": 0}),),
        "LogConfig": MappingProxyType(
            {"Type": "json-file", "Config": MappingProxyType({"max-size": "5m", "max-file": "2"})}
        ),
    }
)

# Docker adds noexec to a tmpfs whatever it is given; the options say so rather than rely on it.
_TMPFS_OPTIONS: Final = "rw,noexec,nosuid,nodev,size=268435456"
# Chromium's renderers share memory through /dev/shm, whose Docker default of 64 MiB is too
# small for a dozen pages. Docker's own setting sizes the mount it already makes, and the
# pages count against the container's memory limit as any other memory does.
_BROWSERS_SHM: Final = 512 * _MIB
_BROWSER_HOME: Final = "/home/pwuser"
_WORKSPACE: Final = "/workspace"

_RUNTIMES: Final = frozenset({"runsc", "runc"})
_ALLOWED_ENV: Final = frozenset({"FOOTFALL_USERNAME", "FOOTFALL_PASSWORD", "PORT"})
_RUN_ID: Final = re.compile(r"r_[a-z2-7]{26}")
_COPY: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")
_HOST_NAME: Final = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
# Leaves the host's side of a run's network without an address.
_NO_HOST_ADDRESS: Final = "com.docker.network.bridge.inhibit_ipv4"
_PROXY_VARS: Final = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def _thaw(value: Any) -> Any:
    """A fresh, mutable copy of a value from the tables above, as JSON-ready lists and dicts."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _workspace_problem(workspace: Path) -> str | None:
    """Why a workspace cannot be bound, if it cannot.

    Docker reads a bind as `source:target:options`, so a colon in the source would name
    another target or options. A `.` or `..` part, or a control character, would make the
    path mean something other than what the runner checked.
    """
    text = str(workspace)
    if not workspace.is_absolute():
        return f"workspace must be an absolute path: {workspace}"
    if ":" in text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return f"workspace must hold no colon or control character: {text!r}"
    if any(part in (".", "..") for part in text.split("/")):
        return f"workspace must hold no . or .. part: {text!r}"
    return None


def _proxy_problem(proxy_url: str) -> str | None:
    """Why a proxy address is not one Footfall makes: http://host:port, and nothing more."""
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in proxy_url):
        return f"the proxy's address holds a space or control character: {proxy_url!r}"
    try:
        parts = urlsplit(proxy_url)
        port = parts.port
    except ValueError:
        return f"the proxy's address is not a URL: {proxy_url!r}"
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_NAME.fullmatch(host):
            return f"the proxy's host is not an address or a host name: {proxy_url!r}"
    if (
        parts.scheme != "http"
        or port is None
        or port < 1
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or proxy_url.endswith(("?", "#"))
    ):
        return f"the proxy's address must be http://host:port, not {proxy_url!r}"
    return None


def _validate(
    *,
    role: str,
    run_id: str,
    copy: str,
    command: Sequence[str],
    workspace: Path | None,
    network: str,
    proxy_url: str,
    env: Mapping[str, str],
    runtime: str,
) -> None:
    if role not in LIMITS:
        raise ValueError(f"unknown role: {role!r}")
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    if not _COPY.fullmatch(copy):
        raise ValueError(f"invalid copy: {copy!r}")
    if runtime not in _RUNTIMES:
        raise ValueError(f"invalid runtime: {runtime!r}")
    if (
        isinstance(command, str | bytes)
        or len(command) == 0
        or not all(isinstance(part, str) for part in command)
    ):
        raise ValueError("command must be a non-empty sequence of strings")
    problem = None if workspace is None else _workspace_problem(workspace)
    if problem:
        raise ValueError(problem)
    if network != f"footfall-{run_id}":
        raise ValueError(f"network must be the run's own, footfall-{run_id}: {network!r}")
    problem = _proxy_problem(proxy_url)
    if problem:
        raise ValueError(problem)
    for key, value in env.items():
        if key not in _ALLOWED_ENV:
            raise ValueError(f"env key not allowed: {key!r}")
        if "\x00" in value or "\n" in value:
            raise ValueError(f"env value for {key} contains NUL or a newline")


def _proxy_entry(name: str, proxy_url: str) -> str:
    """The hosts file entry `name:address` for the proxy at `proxy_url`."""
    if not _HOST_NAME.fullmatch(name):
        raise ValueError(f"invalid proxy name: {name!r}")
    try:
        address = ipaddress.ip_address(urlsplit(proxy_url).hostname or "")
    except ValueError:
        raise ValueError(f"the proxy's address must be an IP address: {proxy_url!r}") from None
    return f"{name}:{address}"


def container_config(
    *,
    role: Role,
    run_id: str,
    copy: str,
    image: str,
    command: Sequence[str],
    workspace: Path | None,
    network: str,
    proxy_url: str,
    env: Mapping[str, str],
    runtime: str = "runsc",
    proxy_name: str | None = None,
) -> dict[str, Any]:
    """Return the body of Docker's `POST /containers/create` for one container.

    With `proxy_name`, the container's hosts file names the proxy, at the address in
    `proxy_url`: Docker's own name service does not answer inside gVisor, and the browsers
    are given the proxy by its name. Raises ValueError for any input outside the shapes
    Footfall itself generates.
    """
    _validate(
        role=role,
        run_id=run_id,
        copy=copy,
        command=command,
        workspace=workspace,
        network=network,
        proxy_url=proxy_url,
        env=env,
        runtime=runtime,
    )
    limits = LIMITS[role]
    extra_hosts = None if proxy_name is None else [_proxy_entry(proxy_name, proxy_url)]

    tmpfs = {"/tmp": _TMPFS_OPTIONS}  # noqa: S108
    if role == "browsers":
        tmpfs[_BROWSER_HOME] = _TMPFS_OPTIONS

    host_config: dict[str, Any] = {
        "Runtime": runtime,
        **_thaw(_HARDENING),
        "Tmpfs": tmpfs,
        "Memory": limits.memory_bytes,
        "MemorySwap": limits.memory_bytes,
        "NanoCpus": limits.nano_cpus,
        "PidsLimit": limits.pids,
        "NetworkMode": network,
    }
    if role == "browsers":
        host_config["ShmSize"] = _BROWSERS_SHM
    if extra_hosts is not None:
        host_config["ExtraHosts"] = extra_hosts
    if workspace is not None:
        host_config["Binds"] = [f"{workspace}:{_WORKSPACE}:rw"]

    environment = [f"{name}={proxy_url}" for name in _PROXY_VARS]
    environment += ["NO_PROXY=localhost,127.0.0.1", "FOOTFALL=1"]
    environment += [f"{key}={value}" for key, value in env.items()]

    body: dict[str, Any] = {
        "Image": image,
        "Cmd": list(command),
        "Env": environment,
        "Labels": {"footfall.run": run_id, "footfall.role": role, "footfall.copy": copy},
        "HostConfig": host_config,
    }
    if role != "browsers":
        # The browsers image runs as its own pwuser, with its own home and working directory.
        body["User"] = f"{APP_UID}:{APP_UID}"
        body["WorkingDir"] = _WORKSPACE
    return body


def network_config(run_id: str) -> dict[str, Any]:
    """Return the body of Docker's `POST /networks/create` for a run's network.

    It is internal, so it has no route out, and the host's side of its bridge has no
    address, so a container on it cannot reach the host, and with it whatever the host
    serves on every address, as Docker otherwise allows. Raises ValueError for a bad run id.
    """
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return {
        "Name": f"footfall-{run_id}",
        "Internal": True,
        "Labels": {"footfall.run": run_id},
        "CheckDuplicate": True,
        "Options": {_NO_HOST_ADDRESS: "true"},
    }
