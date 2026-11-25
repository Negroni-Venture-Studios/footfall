"""An app container under gVisor: what code under test can be, hold and reach.

Each probe runs as an app copy the runner starts, so it has exactly the settings
`runner/spec.py` gives an app. Every check that something is refused comes with a check
that the same probe can do the allowed thing, so that a broken probe cannot pass.
"""

import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from footfall.local import wait_ready
from footfall.runner.client import RunnerClient
from tests.isolation.conftest import NewRun
from tests.isolation.live import (
    MARK,
    PROBE,
    RUNNER_SOCKET,
    LiveRun,
    finish,
    lines,
    local_address_towards,
    prepare,
)

pytestmark = pytest.mark.isolation

APP_UID = 10010
NO_CAPABILITIES = "0000000000000000"
STEP = MARK + "-STEP"
# What gVisor's own kernel reports, whatever the host runs.
GVISOR_VERSION = "Linux version 4.4.0 #1 SMP Sun Jan 10 15:06:54 PST 2016"
HOST_PORT = int(os.environ.get("FOOTFALL_TEST_HOST_PORT") or "0")
HOST_ADDRESSES = (os.environ.get("FOOTFALL_TEST_HOST_ADDRESSES") or "").split()


@pytest.fixture(scope="module")
def sandbox() -> dict[str, Any]:
    """What one app container finds about itself; every test here reads the same report."""

    async def look() -> dict[str, Any]:
        live = await prepare(RunnerClient(RUNNER_SOCKET))
        try:
            return await live.probe("sandbox", "sandbox")
        finally:
            await finish(live)

    return asyncio.run(look())


# ---------------------------------------------------------------- the sandbox


def test_the_app_runs_on_gvisor_not_the_host_kernel(sandbox: dict[str, Any]) -> None:
    log = sandbox["kernel_log"]
    said_gvisor = "gVisor" in log.get("dmesg", "") + log.get("klogctl", "")
    assert said_gvisor or sandbox["proc_version"] == GVISOR_VERSION, log
    # The orchestrator runs on the host's own kernel, which the app must not see.
    assert sandbox["proc_version"] != Path("/proc/version").read_text().strip()


def test_the_app_runs_as_its_own_user_with_no_capabilities(sandbox: dict[str, Any]) -> None:
    assert (sandbox["uid"], sandbox["euid"], sandbox["gid"]) == (APP_UID, APP_UID, APP_UID)
    assert set(sandbox["groups"]) <= {APP_UID}
    status = sandbox["status"]
    for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert status.get(name, NO_CAPABILITIES) == NO_CAPABILITIES, (name, status)
    assert status["CapEff"] == NO_CAPABILITIES
    assert status["NoNewPrivs"] == "1"


def test_the_root_file_system_is_read_only(sandbox: dict[str, Any]) -> None:
    create = sandbox["create"]
    # /var/tmp is open to every user, so only a read-only root can refuse it.
    assert create["/var/tmp"] == "EROFS", create  # noqa: S108
    for folder in ("/", "/etc", "/usr/local/bin"):
        assert create[folder] in ("EROFS", "EACCES"), create
    assert create["/tmp"] == "created"  # noqa: S108
    assert create["/workspace"] == "created"
    assert sandbox["root_read_only"] or any(
        "ro" in line.split()[3].split(",") for line in sandbox["root_mount"]
    ), sandbox["root_mount"]


def test_no_socket_of_docker_or_footfall_is_there(sandbox: dict[str, Any]) -> None:
    assert sandbox["paths"] == {
        "/var/run/docker.sock": False,
        "/run/docker.sock": False,
        "/run/footfall/runner.sock": False,
        "/run/footfall-proxy/proxy.sock": False,
        "/var/lib/footfall": False,
        "/repo": False,
    }


def test_the_workspace_belongs_to_the_app_user(sandbox: dict[str, Any]) -> None:
    assert sandbox["workspace"] == {"uid": APP_UID, "gid": APP_UID}
    assert sandbox["workspace_files"] > 10  # the demo app and the probes really are there
    assert sandbox["not_the_apps"] == []


def test_the_environment_holds_no_secret(sandbox: dict[str, Any]) -> None:
    names = set(sandbox["environment"])
    assert {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "FOOTFALL", "PORT"} <= names
    for forbidden in ("FOOTFALL_PASSWORD", "FOOTFALL_USERNAME", "ANTHROPIC_API_KEY"):
        assert forbidden not in names
    risky = {"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"}
    named = [n for n in names - {"GPG_KEY"} if any(word in n.upper() for word in risky)]
    assert named == []


# ---------------------------------------------------------------- limits


async def test_allocating_600_mib_kills_the_allocation(run: LiveRun) -> None:
    await run.start("memory", f"{PROBE} memory 100 600")
    (control,) = await run.wait_for("memory", STEP, where=lambda s: s.get("allocated") == 100)
    assert control == {"allocated": 100, "returncode": 0, "held": True}
    try:
        (outcome,) = await run.wait_for(
            "memory", STEP, where=lambda s: s.get("allocated") == 600, timeout=150
        )
    except TimeoutError:
        # Under gVisor the process the host's kernel kills may be the whole sandbox, and
        # then the probe never reports. Either way the 600 MiB must never have been held.
        outcome = None
    logs = await run.logs("memory")
    assert '"allocating": 600' in logs
    assert lines(logs, MARK) == [] or outcome is not None
    if outcome is not None:
        assert outcome["held"] is False, outcome
        assert outcome.get("signal") == "SIGKILL" or outcome.get("memory_error"), outcome


async def test_a_fork_bomb_stops_at_the_process_limit(run: LiveRun) -> None:
    result = await run.probe("processes", "processes", "1000")
    assert result["refused"] == "EAGAIN", result
    assert 0 < result["started"] < 256, result


# ---------------------------------------------------------------- what it can reach


def _routes() -> tuple[str, list[str]]:
    """This orchestrator's default gateway, the host's address on Docker's own bridge, and
    the first address of each other network it is on, which Docker gives the host."""
    gateway = ""
    firsts: list[str] = []
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        destination, via, mask = (int(fields[i], 16) for i in (1, 2, 7))
        if destination == 0 and via:
            gateway = socket.inet_ntoa(via.to_bytes(4, "little"))
        elif destination and mask:
            network = ipaddress.ip_network(
                (socket.inet_ntoa(destination.to_bytes(4, "little")), bin(mask).count("1"))
            )
            firsts.append(str(network.network_address + 1))
    return gateway, firsts


async def test_the_app_cannot_reach_the_internet_the_metadata_address_or_the_host(
    run: LiveRun,
) -> None:
    gateway, firsts = _routes()
    host_targets = sorted({gateway, *firsts, *HOST_ADDRESSES} - {""})
    assert HOST_PORT and host_targets, "the workflow names the host's addresses and a port"
    # The host listens on HOST_PORT on every address: the orchestrator, on Docker's own
    # bridge, reaches it there, so a refusal from the app is the sandbox's doing.
    _, writer = await asyncio.open_connection(gateway, HOST_PORT)
    writer.close()
    targets = [
        "1.1.1.1:443",
        "8.8.8.8:53",
        "example.com:443",
        "169.254.169.254:80",
        *(f"{host}:{HOST_PORT}" for host in host_targets),
        *(f"{host}:22" for host in host_targets),
    ]
    reached = await run.probe("connect", "connect", *targets)
    assert [target for target, outcome in reached.items() if outcome == "connected"] == []


async def test_the_orchestrator_answers_on_no_port(run: LiveRun) -> None:
    pages, _ = await run.serve("pages", "pages", 8000)
    page_host = pages.rpartition(":")[0]
    orchestrator = local_address_towards(page_host)
    resolved = await run.probe("resolve", "resolve", "footfall-orchestrator")
    assert resolved["footfall-orchestrator"] == [orchestrator]
    scan = await run.probe("scan", "scan", f"{orchestrator}:1-65535", f"{page_host}:8000")
    assert scan[f"{page_host}:8000"]["open"] == [8000]  # the scan finds what listens
    assert scan[f"{orchestrator}:1-65535"]["open"] == [], scan


async def test_one_runs_app_cannot_reach_anothers(new_run: NewRun) -> None:
    first = await new_run()
    second = await new_run()
    address = await first.start("app", "python3 serve.py --host 0.0.0.0", port=8080)
    await wait_ready(address, "/healthz", 30)
    same = await first.probe("same-run", "connect", address)
    other = await second.probe("other-run", "connect", address)
    assert same == {address: "connected"}
    assert other[address] != "connected", other
