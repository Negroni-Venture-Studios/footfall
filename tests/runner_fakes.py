"""Fakes for the runner's tests: a Docker engine and an egress proxy that record their calls.

The engine keeps just enough state to catch the runner's mistakes: names must be unique, a
container needs its network, and a network with anything still attached cannot be removed, as
with the real daemon. Steps are scripted by role, so a test can make the seed step fail, time out
or write files into its workspace.
"""

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from footfall.proxy.control import ProxyRefused
from footfall.runner.engine import EngineError
from footfall.runner.service import RunnerService

PROXY = "footfall-proxy"
ORCHESTRATOR = "footfall-orchestrator"
OUTSIDE_IPS = {PROXY: "10.89.0.2", ORCHESTRATOR: "10.89.0.3"}
IMAGES = {"python3.13": "footfall-python3.13:dev", "node22": "footfall-node22:dev"}
BROWSERS_IMAGE = "footfall-browsers:dev"


class FakeEngine:
    """An in-memory Docker Engine that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.configs: dict[str, dict[str, Any]] = {}  # every container ever created, by name
        self.live: dict[str, str] = {}  # name -> id, for containers not yet removed
        self.ips: dict[str, str] = {}
        self.networks: dict[str, dict[str, str]] = {}
        self.attached: set[tuple[str, str]] = set()  # (network, container) made by connect
        self.exit_codes: dict[str, int] = {}  # by role
        self.output: dict[str, str] = {}  # log text, by role
        self.slow: set[str] = set()  # roles whose wait times out
        self.during_wait: dict[str, Callable[[Path], object]] = {}  # by role, given the workspace
        self.failures: dict[tuple[str, str | None], BaseException] = {}
        self._count = 9
        self._lock = threading.RLock()

    def fail(self, method: str, error: BaseException, role: str | None = None) -> None:
        """Make `method` raise `error`, for every container or only those of one role."""
        self.failures[(method, role)] = error

    def _maybe_fail(self, method: str, role: str | None = None) -> None:
        error = self.failures.get((method, role)) or self.failures.get((method, None))
        if error is not None:
            raise error

    def _name(self, ref: str) -> str:
        for name, container_id in self.live.items():
            if ref in (name, container_id):
                return name
        return ref

    def _role(self, name: str) -> str | None:
        config = self.configs.get(name)
        return None if config is None else str(config["Labels"]["footfall.role"])

    def workspace_of(self, name: str) -> Path:
        bind: str = self.configs[name]["HostConfig"]["Binds"][0]
        return Path(bind.rsplit(":", 2)[0])

    def create(self, name: str, config: dict[str, Any]) -> str:
        with self._lock:
            self.calls.append(("create", name))
            self._maybe_fail("create", config["Labels"]["footfall.role"])
            if name in self.live:
                raise EngineError(f"Conflict. The container name {name} is already in use")
            if config["HostConfig"]["NetworkMode"] not in self.networks:
                raise EngineError("network not found")
            self._count += 1
            container_id = f"id{self._count}"
            self.configs[name] = config
            self.live[name] = container_id
            self.ips[name] = f"10.89.0.{self._count}"
            return container_id

    def start(self, container: str) -> None:
        with self._lock:
            name = self._name(container)
            self.calls.append(("start", name))
            self._maybe_fail("start", self._role(name))
            if name not in self.live:
                raise EngineError(f"No such container: {name}")

    def wait(self, container: str, timeout: float) -> int:
        name = self._name(container)
        role = self._role(name)
        self.calls.append(("wait", name, timeout))
        self._maybe_fail("wait", role)
        if role in self.slow:
            raise TimeoutError(f"container {name} still running after {timeout}s")
        action = self.during_wait.get(role or "")
        if action is not None:
            action(self.workspace_of(name))
        return self.exit_codes.get(role or "", 0)

    def logs(self, container: str, tail: int = 200) -> str:
        with self._lock:
            name = self._name(container)
            self.calls.append(("logs", name, tail))
            self._maybe_fail("logs", self._role(name))
            if name not in self.live:
                raise EngineError(f"No such container: {name}")
            return self.output.get(self._role(name) or "", "")

    def address(self, container: str, network: str) -> str:
        with self._lock:
            name = self._name(container)
            self.calls.append(("address", name, network))
            self._maybe_fail("address", self._role(name))
            if name in self.live and self.configs[name]["HostConfig"]["NetworkMode"] == network:
                return self.ips[name]
            if (network, name) in self.attached:
                return OUTSIDE_IPS[name]
            raise EngineError(f"container {name} has no address on network {network}")

    def remove(self, container: str) -> None:
        with self._lock:
            name = self._name(container)
            self.calls.append(("remove", name))
            self._maybe_fail("remove", self._role(name))
            self.live.pop(name, None)

    def create_network(self, config: Mapping[str, Any]) -> None:
        name = str(config["Name"])
        with self._lock:
            self.calls.append(("create_network", name, dict(config)))
            self._maybe_fail("create_network")
            if name in self.networks:
                raise EngineError(f"network with name {name} already exists")
            self.networks[name] = dict(config["Labels"])

    def connect(self, network: str, container: str) -> None:
        with self._lock:
            self.calls.append(("connect", network, container))
            self._maybe_fail("connect")
            if network not in self.networks:
                raise EngineError(f"network {network} not found")
            self.attached.add((network, container))

    def disconnect(self, network: str, container: str) -> None:
        with self._lock:
            self.calls.append(("disconnect", network, container))
            self._maybe_fail("disconnect")
            self.attached.discard((network, container))

    def remove_network(self, name: str) -> None:
        with self._lock:
            self.calls.append(("remove_network", name))
            self._maybe_fail("remove_network")
            in_use = any(network == name for network, _ in self.attached) or any(
                self.configs[c]["HostConfig"]["NetworkMode"] == name for c in self.live
            )
            if in_use:
                raise EngineError(f"error while removing network: {name} has active endpoints")
            self.networks.pop(name, None)

    def containers(self, label: str) -> list[str]:
        with self._lock:
            self.calls.append(("containers", label))
            self._maybe_fail("containers")
            key, _, value = label.partition("=")
            return [
                container_id
                for name, container_id in self.live.items()
                if self.configs[name]["Labels"].get(key) == value
            ]

    def labelled(self, key: str) -> list[tuple[str, str]]:
        with self._lock:
            self.calls.append(("labelled", key))
            self._maybe_fail("labelled")
            return [
                (container_id, self.configs[name]["Labels"][key])
                for name, container_id in self.live.items()
                if key in self.configs[name]["Labels"]
            ]

    def labelled_networks(self, key: str) -> list[tuple[str, str]]:
        with self._lock:
            self.calls.append(("labelled_networks", key))
            self._maybe_fail("labelled_networks")
            return [(name, labels[key]) for name, labels in self.networks.items() if key in labels]

    def names(self, method: str) -> list[str]:
        """The first argument of every call to `method`, in order."""
        return [call[1] for call in self.calls if call[0] == method]


class FakeClock:
    """A monotonic clock that moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProxy:
    """The egress proxy's control interface, holding allow lists by address."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.allowed: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.failures: dict[str, BaseException] = {}

    def allow(self, ip: str, run_id: str, hosts: Any) -> None:
        self.calls.append(("allow", ip, run_id, tuple(hosts)))
        if "allow" in self.failures:
            raise self.failures["allow"]
        owner = self.allowed.get(ip, (run_id, ()))[0]
        if owner != run_id:
            raise ProxyRefused(f"{ip} already has an allow list for another run")
        self.allowed[ip] = (run_id, tuple(hosts))

    def revoke(self, ip: str) -> None:
        self.calls.append(("revoke", ip))
        if "revoke" in self.failures:
            raise self.failures["revoke"]
        self.allowed.pop(ip, None)

    def revoke_run(self, run_id: str) -> None:
        self.calls.append(("revoke_run", run_id))
        if "revoke_run" in self.failures:
            raise self.failures["revoke_run"]
        for ip in [ip for ip, (owner, _) in self.allowed.items() if owner == run_id]:
            del self.allowed[ip]

    def reached(self, run_id: str) -> list[str]:
        return []

    def listen(self, ip: str, run_id: str) -> None:
        self.calls.append(("listen", ip, run_id))
        if "listen" in self.failures:
            raise self.failures["listen"]


@dataclass
class Harness:
    """A runner with fake Docker and proxy, and real folders in a temporary directory."""

    engine: FakeEngine
    proxy: FakeProxy
    service: RunnerService
    workspaces: Path
    runs: Path
    chowned: list[Path] = field(default_factory=list)
    clock: FakeClock = field(default_factory=FakeClock)

    def workspace(self, name: str = "app", files: Mapping[str, str] | None = None) -> Path:
        """Make a workspace folder holding `files` (relative path -> text)."""
        folder = self.workspaces / name
        folder.mkdir()
        for relative, text in (files or {"serve.py": "print('hello')\n"}).items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return folder


def make_harness(tmp_path: Path, **settings: Any) -> Harness:
    engine = FakeEngine()
    proxy = FakeProxy()
    workspaces = (tmp_path / "workspaces").resolve()
    workspaces.mkdir(parents=True)
    runs = (tmp_path / "runs").resolve()
    chowned: list[Path] = []
    record = settings.pop("chown", chowned.append)
    clock = FakeClock()
    settings.setdefault("clock", clock)
    settings.setdefault("runs_owner", os.getuid())
    service = RunnerService(
        engine,
        proxy,
        workspaces=workspaces,
        runs=runs,
        images=IMAGES,
        browsers_image=BROWSERS_IMAGE,
        proxy_container=PROXY,
        orchestrator_container=ORCHESTRATOR,
        chown=recording(record),
        **settings,
    )
    return Harness(engine, proxy, service, workspaces, runs, chowned, clock)


def recording(record: Callable[[Path], object]) -> Callable[[int, str, Path], None]:
    """A chown for the runner that only passes the path of each entry to `record`."""

    def chown(folder: int, name: str, path: Path) -> None:
        record(path)

    return chown
