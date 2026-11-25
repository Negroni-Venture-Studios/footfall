"""Checks that need the Docker socket, so they run on the CI host itself, never in Footfall.

Only the runner talks to Docker (AGENTS.md rule 1), so the tests in the orchestrator cannot
see what Docker holds. The isolation workflow runs this file on the host, with
`FOOTFALL_ISOLATION_HOST=1`, after the others have finished. It checks how the real daemon
answers the calls the engine tolerates, and that no run left a container or a network.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import pytest

from footfall.local import new_run_id
from footfall.runner.engine import DEFAULT_SOCKET, DockerEngine
from footfall.runner.spec import container_config, network_config

pytestmark = pytest.mark.isolation

ON_HOST = os.environ.get("FOOTFALL_ISOLATION_HOST") == "1"
RUNNING = "footfall-proxy"  # a container the dev stack keeps running
IMAGE = "footfall-python3.13:dev"
EVERY_STATUS = frozenset(range(100, 600))


@pytest.fixture
def engine() -> DockerEngine:
    if not ON_HOST:
        pytest.skip("runs on the CI host, where the Docker socket is")
    assert DEFAULT_SOCKET.exists(), "the host step needs the Docker socket"
    return DockerEngine()


@pytest.fixture
def network(engine: DockerEngine) -> Iterator[str]:
    """A fresh internal network, named as a run's is, that no container has joined."""
    run_id = new_run_id()
    config = network_config(run_id)
    # Labelled as a check, not a run, so that the check for leftovers ignores it.
    config["Labels"] = {"footfall.check": "engine"}
    name = str(config["Name"])
    engine.create_network(config)
    try:
        yield name
    finally:
        engine.remove_network(name)


def _record(name: str, data: dict[str, Any]) -> None:
    """Put what the daemon said in the job summary, where whoever reads the run can see it."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as out:
            out.write(f"\n**{name}**: `{json.dumps(data, sort_keys=True)}`\n")
    print(name, json.dumps(data, sort_keys=True))


def _raw(engine: DockerEngine, method: str, target: str, body: Any = None) -> tuple[int, str]:
    status, data = engine._request(method, target, body, ok=EVERY_STATUS)
    return status, data.decode("utf-8", "replace").strip()


def test_disconnecting_a_container_that_is_not_connected(
    engine: DockerEngine, network: str
) -> None:
    body = {"Container": RUNNING, "Force": True}
    forced = _raw(engine, "POST", f"/networks/{network}/disconnect", body)
    unforced = _raw(engine, "POST", f"/networks/{network}/disconnect", {"Container": RUNNING})
    missing = _raw(engine, "POST", f"/networks/footfall-{new_run_id()}/disconnect", body)
    _record(
        "dockerd's answers to a disconnect",
        {
            "not connected, forced": forced,
            "not connected, not forced": unforced,
            "no such network": missing,
        },
    )
    # The runner always forces, and a container that is already off the network, or a
    # network already gone, must count as done.
    engine.disconnect(network, RUNNING)
    engine.disconnect(f"footfall-{new_run_id()}", RUNNING)


def test_the_daemon_answers_as_the_engine_expects(engine: DockerEngine, network: str) -> None:
    run_id = new_run_id()
    name = f"footfall-{run_id}-app-check"
    config = container_config(
        role="app",
        run_id=run_id,
        copy="check",
        image=IMAGE,
        command=["sleep", "120"],
        workspace=None,
        network=network,
        proxy_url="http://192.0.2.1:3128",
        env={},
    )
    engine.create(name, config)
    try:
        engine.start(name)
        engine.start(name)  # already running: 304, which counts as started
        assert engine.address(name, network)
        assert engine.containers(f"footfall.run={run_id}")
        inspected = json.loads(_raw(engine, "GET", f"/containers/{name}/json")[1])
        assert inspected["HostConfig"]["Runtime"] == "runsc"
        assert inspected["State"]["Running"] is True
    finally:
        engine.remove(name)
    engine.remove(name)  # gone already: 404, which counts as removed
    assert engine.containers(f"footfall.run={run_id}") == []
    engine.remove_network(f"footfall-{new_run_id()}")  # gone already


def _results() -> Path:
    return Path(os.environ.get("FOOTFALL_TEST_RESULTS") or "/var/lib/footfall/test-results")


def test_no_run_left_a_container_or_a_network(engine: DockerEngine) -> None:
    containers = engine.containers("footfall.run")
    query = urlencode({"filters": json.dumps({"label": ["footfall.run"]})}, quote_via=quote)
    status, text = _raw(engine, "GET", f"/networks?{query}")
    assert status == 200
    networks = [item["Name"] for item in json.loads(text)]
    assert (containers, networks) == ([], [])
    end_to_end = json.loads((_results() / "end-to-end.json").read_text())
    run_id = end_to_end["run_id"]
    assert engine.containers(f"footfall.run={run_id}") == []
