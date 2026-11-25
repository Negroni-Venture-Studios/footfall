"""Proofs that need a run and a look from outside it, so they run on the CI host.

The isolation workflow runs this file on the host, with `FOOTFALL_ISOLATION_HOST=1`, after the
tests in the orchestrator and before the check that no run left anything behind. It drives the
runner as the orchestrator does, by running a few lines of Python in the orchestrator's own
container, and looks at the runner and the proxy with `docker exec`, as their root user.
"""

import json
import os
import secrets
import socket
import subprocess
from typing import Any

import pytest

from footfall.local import new_run_id
from footfall.runner.engine import DockerEngine
from footfall.runner.spec import LIMITS

pytestmark = pytest.mark.isolation

ON_HOST = os.environ.get("FOOTFALL_ISOLATION_HOST") == "1"
ORCHESTRATOR = "footfall-orchestrator"
RUNNER = "footfall-runner"
PROXY = "footfall-proxy"
PROXY_PORT = 3128
RUNS = "/var/lib/footfall-runs"
LISTENING = "0A"  # the state /proc/net/tcp gives a listening socket

# Run in the orchestrator: one runner call, as footfall test would make it, and a line of JSON.
_DRIVE = """
import asyncio, json, shutil, sys
from footfall.runner.client import RunnerClient
from footfall.runner.service import RunnerError
from tests.isolation.live import RUNNER_SOCKET, WORKSPACES, make_workspace

async def main(op, run_id):
    client = RunnerClient(RUNNER_SOCKET)
    try:
        if op == "prepare":
            await client.prepare(run_id, make_workspace(run_id), "python3.13", None, None, [], {})
            return {"ok": True}
        if op == "start":
            return {"address": await client.start_app(run_id, "anna", "sleep 300", 8000, [])}
        if op == "browsers":
            await client.start_browsers(run_id, [])  # its endpoint holds a secret: never shown
            return {"ok": True}
        await client.stop(run_id)
        shutil.rmtree(WORKSPACES / run_id, ignore_errors=True)
        return {"ok": True}
    except RunnerError as error:
        return {"error": str(error)}

print(json.dumps(asyncio.run(main(sys.argv[1], sys.argv[2]))))
"""


@pytest.fixture
def engine() -> DockerEngine:
    if not ON_HOST:
        pytest.skip("runs on the CI host, where the Docker socket is")
    return DockerEngine()


def in_container(container: str, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container, *command],  # noqa: S607  # the CI host's docker
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def drive(op: str, run_id: str) -> dict[str, Any]:
    """Ask the runner for `op` on a run, from the orchestrator, and return what it said."""
    done = in_container(ORCHESTRATOR, "python", "-c", _DRIVE, op, run_id)
    assert done.returncode == 0, done.stderr[-2000:]
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    assert isinstance(answer, dict)
    return answer


def record(name: str, data: object) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:  # noqa: PTH123
            out.write(f"\n**{name}**: `{json.dumps(data, sort_keys=True)}`\n")
    print(name, json.dumps(data, sort_keys=True))


def test_a_link_planted_below_a_runs_folder_is_refused(engine: DockerEngine) -> None:
    """Ruling R66: the runner writes below a run's folder only through folders it holds,
    never through a link. Root, in the runner's own container, the only user that can write
    in the runs folder, plants a `copies` link to an outside folder between prepare and the
    start of an app; the start is refused and nothing reaches the outside folder. The control
    is a second run, prepared and started the same way without the link."""
    planted, control = new_run_id(), new_run_id()
    outside = f"/tmp/outside-{secrets.token_hex(6)}"  # noqa: S108  # in the runner's own /tmp
    try:
        assert drive("prepare", planted) == {"ok": True}
        assert drive("prepare", control) == {"ok": True}
        plant = in_container(
            RUNNER, "sh", "-c", f"mkdir {outside} && ln -s {outside} {RUNS}/{planted}/copies"
        )
        assert plant.returncode == 0, plant.stderr
        refused = drive("start", planted)
        started = drive("start", control)
        left = in_container(RUNNER, "sh", "-c", f"ls -A {outside} | wc -l")
        record(
            "a link planted below a run's folder",
            {"planted": refused, "control": sorted(started), "outside": left.stdout.strip()},
        )
        assert "link" in refused.get("error", ""), refused
        assert "address" in started, started
        assert left.stdout.strip() == "0"
    finally:
        drive("stop", planted)
        drive("stop", control)
        in_container(RUNNER, "rm", "-rf", outside)
    gone = in_container(RUNNER, "sh", "-c", f"ls -A {RUNS} | wc -l")
    assert gone.stdout.strip() == "0"  # the link went with its run, and nothing it named


def _listeners(container: str, port: int) -> list[str]:
    """The addresses a container listens on at `port`, from its /proc/net/tcp and tcp6."""
    found: list[str] = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        text = in_container(container, "cat", table).stdout
        for line in text.splitlines()[1:]:
            fields = line.split()
            address, _, hex_port = fields[1].partition(":")
            if fields[3] != LISTENING or int(hex_port, 16) != port:
                continue
            if len(address) == 8:
                found.append(socket.inet_ntoa(int(address, 16).to_bytes(4, "little")))
            else:
                found.append(f"[{address}]")  # never expected: any IPv6 listener is wrong
    return sorted(found)


def test_the_proxy_listens_only_at_a_live_runs_address(engine: DockerEngine) -> None:
    """Ruling R70: the egress proxy listens at its own address on each run's network while
    the run lives, and nowhere else: not on every interface, and not on Docker's default
    bridge, where any container could reach it."""
    run_id = new_run_id()
    try:
        assert drive("prepare", run_id) == {"ok": True}
        on_run = engine.address(PROXY, f"footfall-{run_id}")
        while_live = _listeners(PROXY, PROXY_PORT)
        bridge = engine.address(PROXY, "bridge")
        try:
            with socket.create_connection((bridge, PROXY_PORT), timeout=10):
                on_bridge = "connected"
        except OSError as error:
            on_bridge = type(error).__name__
    finally:
        drive("stop", run_id)
    after = _listeners(PROXY, PROXY_PORT)
    record(
        "where the proxy listens",
        {"while the run lives": while_live, "on the bridge": on_bridge, "after": after},
    )
    assert while_live == [on_run]
    assert on_bridge == "ConnectionRefusedError"
    assert after == []


def _inspect(engine: DockerEngine, what: str) -> dict[str, Any]:
    answer = json.loads(engine._request("GET", what)[1])
    assert isinstance(answer, dict)
    return answer


def test_the_daemon_holds_what_the_spec_gives_containers_the_runner_started(
    engine: DockerEngine,
) -> None:
    """Audit N-09: the probes see a container from inside, where gVisor may stop something
    before a limit does, so the daemon's own record of an app container and a browser
    container that the runner started is read too, for each setting runner/spec.py fixes."""
    run_id = new_run_id()
    network = f"footfall-{run_id}"
    held: dict[str, Any] = {}
    try:
        assert drive("prepare", run_id) == {"ok": True}
        assert "address" in drive("start", run_id)
        assert drive("browsers", run_id) == {"ok": True}
        for role, name in (
            ("app", f"footfall-{run_id}-app-anna"),
            ("browsers", f"footfall-{run_id}-browsers-browsers"),
        ):
            inspected = _inspect(engine, f"/containers/{name}/json")
            host = inspected["HostConfig"]
            limits = LIMITS[role]  # type: ignore[index]
            held[role] = {
                key: host.get(key)
                for key in (
                    "Runtime",
                    "Memory",
                    "MemorySwap",
                    "NanoCpus",
                    "PidsLimit",
                    "CapAdd",
                    "CapDrop",
                    "SecurityOpt",
                    "ReadonlyRootfs",
                    "Privileged",
                    "NetworkMode",
                )
            }
            held[role]["mounts"] = [
                (mount["Type"], mount["Destination"], mount["RW"])
                for mount in inspected.get("Mounts") or []
            ]
            assert host["Runtime"] == "runsc"
            assert (host["Memory"], host["MemorySwap"]) == (limits.memory_bytes,) * 2
            assert (host["NanoCpus"], host["PidsLimit"]) == (limits.nano_cpus, limits.pids)
            assert (host["CapDrop"], host.get("CapAdd") or []) == (["ALL"], [])
            assert host["SecurityOpt"] == ["no-new-privileges:true"]
            assert (host["ReadonlyRootfs"], host["Privileged"]) == (True, False)
            assert host["NetworkMode"] == network
            assert list(inspected["NetworkSettings"]["Networks"]) == [network]
        assert held["app"]["mounts"] == [("bind", "/workspace", True)]
        assert held["browsers"]["mounts"] == []
        networked = _inspect(engine, f"/networks/{network}")
        held["network"] = {"Internal": networked["Internal"]}
        assert networked["Internal"] is True
    finally:
        drive("stop", run_id)
    record("what the daemon holds for containers the runner started", held)
