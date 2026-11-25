"""The isolation tests' view of the real stack: runs, probes and the proxy's log.

A test runs in the dev stack's orchestrator container, as `footfall test` does, and reaches
the runner by its socket. It never talks to Docker. A probe is `probes/probe.py` started by
the runner as an app copy, so it has exactly an app's settings, and the test reads what it
found from the copy's logs.
"""

import asyncio
import contextlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from footfall.local import new_run_id
from footfall.runner.client import RunnerClient

HERE: Final = Path(__file__).resolve().parent
PROBES: Final = HERE / "probes"
PROBE: Final = "python3 probes/probe.py"
MARK: Final = "FOOTFALL-PROBE"
READY: Final = "FOOTFALL-READY"
HELLO: Final = "FOOTFALL-HELLO"
HIT: Final = "FOOTFALL-HIT"
POLL: Final = 0.5


def setting(name: str, default: str) -> str:
    return os.environ.get(name) or default


RUNNER_SOCKET: Final = Path(setting("FOOTFALL_RUNNER_SOCKET", "/run/footfall/runner.sock"))
WORKSPACES: Final = Path(setting("FOOTFALL_WORKSPACES", "/var/lib/footfall/workspaces"))
DEMO_APP: Final = Path(setting("FOOTFALL_DEMO_APP", "/repo/demo-app"))
PROXY_LOG: Final = Path(setting("FOOTFALL_TEST_PROXY_LOG", "/var/lib/footfall/test-logs/proxy.log"))
RESULTS: Final = Path(setting("FOOTFALL_TEST_RESULTS", "/var/lib/footfall/test-results"))


def lines(text: str, tag: str) -> list[dict[str, Any]]:
    """Every `<tag> <json>` line in a log, parsed, oldest first."""
    found: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith(tag + " "):
            value = json.loads(line[len(tag) + 1 :])
            if isinstance(value, dict):
                found.append(value)
    return found


def address_of(endpoint: str) -> str:
    """The host of a `ws://host:port/<path>` endpoint, without its secret path."""
    return urlsplit(endpoint).hostname or ""


def local_address_towards(address: str) -> str:
    """This container's own address on the network that leads to `address`."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((address, 9))  # a datagram socket sends nothing when it connects
        return str(probe.getsockname()[0])


def is_private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


@dataclass
class LiveRun:
    """One prepared run, and what the test started in it."""

    run_id: str
    client: RunnerClient
    workspace: Path
    account: tuple[str, str]
    copies: dict[str, str] = field(default_factory=dict)  # copy -> "address:port"

    async def start(
        self, copy: str, start: str, *, port: int = 8000, egress: Sequence[str] = ()
    ) -> str:
        """Start an app copy and return its "address:port"."""
        address = await self.client.start_app(self.run_id, copy, start, port, egress)
        self.copies[copy] = address
        return address

    async def logs(self, copy: str) -> str:
        return await self.client.logs(self.run_id, copy)

    async def wait_for(
        self,
        copy: str,
        tag: str,
        *,
        count: int = 1,
        timeout: float = 120.0,
        where: Callable[[dict[str, Any]], bool] = lambda _: True,
    ) -> list[dict[str, Any]]:
        """Wait until the copy has printed `count` lines with `tag` that `where` accepts."""
        deadline = time.monotonic() + timeout
        while True:
            text = await self.logs(copy)
            found = [line for line in lines(text, tag) if where(line)]
            if len(found) >= count:
                return found
            if "Traceback (most recent call last)" in text:
                raise AssertionError(f"{copy} failed:\n{text[-3000:]}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{copy} printed {len(found)} of {count} {tag} lines")
            await asyncio.sleep(POLL)

    async def probe(
        self,
        copy: str,
        *arguments: str,
        egress: Sequence[str] = (),
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        """Run one check of probe.py in an app copy of its own and return its result."""
        await self.start(copy, " ".join([PROBE, *arguments]), egress=egress)
        (report,) = await self.wait_for(copy, MARK, timeout=timeout)
        result = report["result"]
        assert isinstance(result, dict)
        return result

    async def serve(
        self, copy: str, server: str, port: int, *, egress: Sequence[str] = ()
    ) -> tuple[str, dict[str, Any]]:
        """Start one of probe.py's servers and return its "address:port" once it listens."""
        address = await self.start(copy, f"{PROBE} {server} {port}", port=port, egress=egress)
        (ready,) = await self.wait_for(copy, READY, timeout=60)
        return address, ready


def make_workspace(run_id: str, *, app: Path = DEMO_APP) -> Path:
    """A run's workspace: the demo app, with the probes beside it."""
    workspace = WORKSPACES / run_id
    shutil.copytree(app, workspace, ignore=shutil.ignore_patterns("__pycache__", "*.sqlite3"))
    shutil.copytree(PROBES, workspace / "probes", ignore=shutil.ignore_patterns("__pycache__"))
    return workspace


def throwaway_account() -> tuple[str, str]:
    return "tester-" + secrets.token_hex(4), secrets.token_urlsafe(24)


async def prepare(
    client: RunnerClient,
    *,
    install: str | None = None,
    seed: str | None = "python3 seed.py",
    egress: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    run_id: str | None = None,
) -> LiveRun:
    """Prepare a run of the demo app, as `footfall test` does, with a fresh run id."""
    run_id = run_id or new_run_id()
    account = throwaway_account()
    workspace = make_workspace(run_id)
    if env is None:
        env = {"FOOTFALL_USERNAME": account[0], "FOOTFALL_PASSWORD": account[1]}
    run = LiveRun(run_id, client, workspace, account)
    try:
        await client.prepare(run_id, workspace, "python3.13", install, seed, egress, env)
    except BaseException:
        await finish(run)
        raise
    return run


async def finish(run: LiveRun) -> None:
    """Stop the run and remove its workspace; a stop that fails fails the test."""
    try:
        await run.client.stop(run.run_id)
    finally:
        shutil.rmtree(run.workspace, ignore_errors=True)


class ProxyLog:
    """The egress proxy's log, from where it stood when this was made.

    The test compose file has the proxy write its log to a file the orchestrator can read,
    so that a test can see the proxy's own decision on each request.
    """

    def __init__(self, path: Path = PROXY_LOG) -> None:
        self._path = path
        self._start = path.stat().st_size if path.exists() else 0

    def read(self) -> list[str]:
        with self._path.open("rb") as log:
            log.seek(self._start)
            return log.read().decode("utf-8", "replace").splitlines()

    def decisions(self, client: str, host: str | None = None) -> list[str]:
        """The proxy's decisions for one client address, and one host if given."""
        found = []
        for line in self.read():
            words = line.split()
            # The server's decisions alone, never the control socket's record of an allow.
            if "footfall.proxy.server:" not in words or client not in words:
                continue
            if host is not None and f"host={host}" not in words:
                continue
            found.append(line)
        return found

    async def wait_for(
        self, client: str, host: str, outcome: str, *, timeout: float = 15.0
    ) -> list[str]:
        """Wait for a decision on `host` for `client` whose outcome contains `outcome`."""
        deadline = time.monotonic() + timeout
        while True:
            found = [line for line in self.decisions(client, host) if outcome in line]
            if found:
                return found
            if time.monotonic() > deadline:
                seen = self.decisions(client)
                raise TimeoutError(
                    f"no {outcome!r} decision for {host} from {client}; the proxy said: {seen}"
                )
            await asyncio.sleep(POLL)


def record_result(name: str, data: object) -> None:
    """Leave a result where the workflow's host-side checks can read it."""
    with contextlib.suppress(FileExistsError):
        RESULTS.mkdir(mode=0o755, parents=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
