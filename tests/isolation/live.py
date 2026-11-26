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
import re
import secrets
import shutil
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from footfall.actions import mask
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


type NewRun = Callable[..., Awaitable["LiveRun"]]


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
    """Whether the isolation tests may connect to `host`: a private address, but never a
    link-local one, such as the cloud's metadata service, nor the unspecified address."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private and not (address.is_link_local or address.is_unspecified)


@dataclass
class LiveRun:
    """One prepared run, and what the test started in it."""

    run_id: str
    client: RunnerClient
    workspace: Path
    account: tuple[str, str] = field(repr=False)  # holds the throwaway password: never shown
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
    """An account for a run's seed step, made as `footfall test` makes one, its password
    masked in a GitHub Actions log before anything can print it."""
    password = secrets.token_urlsafe(24)
    mask(password)
    return "tester-" + secrets.token_hex(4), password


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


_OUTCOME: Final = re.compile(r" port=\S+ (.*)$")


def decision_outcome(line: str) -> str:
    """The outcome of one of the proxy's decision lines: all that follows `port=N `."""
    found = _OUTCOME.search(line)
    return found.group(1) if found else ""


def allowed(line: str) -> bool:
    """Whether the line says the proxy allowed a connection: in full, as the last line before
    the rest are summarised, or in a summary."""
    outcome = decision_outcome(line)
    return (
        outcome == "allowed"
        or outcome.startswith("allowed;")
        or (outcome.startswith("summary of ") and outcome.endswith(" more connections: allowed"))
    )


def refused(line: str, reason: str | None = None) -> bool:
    """Whether the proxy refused, and, given a reason, refused for that reason."""
    outcome = decision_outcome(line)
    if not outcome.startswith("refused"):
        return False
    return reason is None or reason in outcome.partition(": ")[2]


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

    def all_decisions(self) -> list[str]:
        """Every decision the proxy has logged since this was made, for any client."""
        return [line for line in self.read() if "footfall.proxy.server:" in line.split()]

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
        """Wait for a decision on `host` for `client`: "allowed", or a refusal whose reason
        holds `outcome`."""
        deadline = time.monotonic() + timeout

        def matches(line: str) -> bool:
            return allowed(line) if outcome == "allowed" else refused(line, outcome)

        while True:
            found = [line for line in self.decisions(client, host) if matches(line)]
            if found:
                return found
            if time.monotonic() > deadline:
                seen = self.decisions(client)
                raise TimeoutError(
                    f"no {outcome!r} decision for {host} from {client}; the proxy said: {seen}"
                )
            await asyncio.sleep(POLL)


def fact(name: str, data: object) -> None:
    """Write what a test found about gVisor or Docker to the job's log, as it is found, so
    that docs/notes/gvisor-differences.md can be checked against every run. It goes straight
    to file descriptor 1, which pytest's sys-level capture leaves alone. Never a secret."""
    os.write(1, f"FOOTFALL-FACT {name}: {json.dumps(data, sort_keys=True)}\n".encode())


def record_result(name: str, data: object) -> None:
    """Leave a result where the workflow's host-side checks can read it."""
    with contextlib.suppress(FileExistsError):
        RESULTS.mkdir(mode=0o755, parents=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
