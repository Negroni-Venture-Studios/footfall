"""Fixtures for the isolation tests, which need Docker, gVisor and the dev stack.

They run only in `.github/workflows/isolation.yml`, which builds the images, starts
`deploy/dev/compose.yaml` with `compose.test.yaml` and runs pytest in the orchestrator
container with `FOOTFALL_ISOLATION=1`. Anywhere else the whole folder is skipped. They
refuse to run at all with `FOOTFALL_RUNTIME=runc`, since without gVisor they would prove
nothing about the sandbox. `test_docker_host.py` runs on the CI host itself, where the
Docker socket is, with `FOOTFALL_ISOLATION_HOST=1` as well.

These tests may reach pypi.org and example.com, but only from containers and only through
the egress proxy; the test process itself connects to nothing but private addresses and
Unix sockets.
"""

import os
import socket
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from footfall.runner.client import RunnerClient
from tests.isolation.live import (
    RUNNER_SOCKET,
    LiveRun,
    NewRun,
    ProxyLog,
    finish,
    is_private,
    prepare,
)

HERE = Path(__file__).resolve().parent


def refusal(env: Mapping[str, str]) -> str | None:
    """Why the isolation tests must stop before they start, or None when they may run."""
    if env.get("FOOTFALL_ISOLATION") == "1" and env.get("FOOTFALL_RUNTIME") == "runc":
        return (
            "the isolation tests refuse to run with FOOTFALL_RUNTIME=runc: without gVisor "
            "they prove nothing about the sandbox"
        )
    return None


def pytest_configure(config: pytest.Config) -> None:
    reason = refusal(os.environ)
    if reason is not None:
        pytest.exit(reason, returncode=pytest.ExitCode.USAGE_ERROR)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("FOOTFALL_ISOLATION") == "1":
        return
    skip = pytest.mark.skip(reason="the isolation tests run only with FOOTFALL_ISOLATION=1")
    for item in items:
        if item.path.is_relative_to(HERE):
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Under FOOTFALL_ISOLATION=1 a skipped isolation test fails: it would prove nothing while
    its step passed (audit N-09). The workflow runs each test where it can run."""
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if (
        os.environ.get("FOOTFALL_ISOLATION") == "1"
        and report.skipped
        and item.path.is_relative_to(HERE)
    ):
        report.outcome = "failed"
        report.longrepr = f"an isolation test may not be skipped here: {report.longrepr}"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces the suite's loopback-only guard: these tests reach the containers on Docker's
    private networks, but never a public address, which only the proxy may reach."""
    real_connect: Callable[[socket.socket, Any], None] = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else address
        if not is_private(str(host)):
            raise OSError(f"network blocked in the isolation tests: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def runner() -> RunnerClient:
    return RunnerClient(RUNNER_SOCKET)


@pytest.fixture
async def new_run(runner: RunnerClient) -> AsyncIterator[NewRun]:
    """Prepare runs of the demo app with fresh run ids; every one is stopped at the end."""
    made: list[LiveRun] = []

    async def make(**options: Any) -> LiveRun:
        run = await prepare(runner, **options)
        made.append(run)
        return run

    try:
        yield make
    finally:
        problems: list[BaseException] = []
        for run in made:
            try:
                await finish(run)
            except Exception as error:  # noqa: BLE001  # stop every run, then report
                problems.append(error)
        if problems:
            raise problems[0]


@pytest.fixture
async def run(new_run: NewRun) -> LiveRun:
    """The demo app, prepared as `footfall test` prepares it, with a fresh run id."""
    return await new_run()


@pytest.fixture
def proxy_log() -> Iterator[ProxyLog]:
    yield ProxyLog()
