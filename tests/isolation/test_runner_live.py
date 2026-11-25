"""The runner in the real stack: its socket, its binds and its disk budget.

The test compose file gives the runner a small disk budget, `FOOTFALL_DISK_BUDGET_MIB`, so
that a seed can go over it in seconds rather than by writing gigabytes.
"""

import os
import secrets
import stat
from pathlib import Path

import pytest

from footfall.local import new_run_id
from footfall.runner.client import RunnerClient
from footfall.runner.service import DISK_REASON, RunnerError
from tests.isolation.live import PROBE, RUNNER_SOCKET, NewRun

pytestmark = pytest.mark.isolation

ORCHESTRATOR_GROUP = 10001


async def test_the_runner_socket_is_root_and_the_orchestrators_group(
    runner: RunnerClient,
) -> None:
    info = RUNNER_SOCKET.lstat()
    assert stat.S_ISSOCK(info.st_mode)
    assert (info.st_uid, info.st_gid) == (0, ORCHESTRATOR_GROUP)
    assert stat.S_IMODE(info.st_mode) == 0o660
    assert os.getgid() == ORCHESTRATOR_GROUP
    assert os.getuid() != 0
    assert await runner.status(new_run_id()) is None  # the orchestrator can use it


def test_the_orchestrator_cannot_reach_the_runs_folder() -> None:
    """The root runner writes below the runs folder and names its folders to Docker, so the
    orchestrator, which is not root, must have no way to rename anything there (audit F-02).
    It does not mount the folder at all, and its own state folder holds no runs folder."""
    assert not Path("/var/lib/footfall-runs").exists()
    assert not Path("/var/lib/footfall/runs").exists()
    assert Path("/var/lib/footfall/workspaces").is_dir()  # the control: what it does mount


async def test_the_seed_writes_into_the_folder_each_app_copy_starts_from(
    new_run: NewRun,
) -> None:
    # Docker reads a bind's source on the host, so if the runs folder had another path in
    # the runner than on the host, the seed would write somewhere no copy is made from.
    mark = secrets.token_hex(8)
    live = await new_run(seed=f"python3 seed.py && {PROBE} write seed-mark.txt {mark}")
    found = await live.probe("reader", "read", "seed-mark.txt")
    assert found["seed-mark.txt"] == mark
    sandbox = await live.probe("sandbox", "sandbox")
    assert sandbox["not_the_apps"] == []  # the seed's files too belong to the app's user


async def _stopped_for_disk(new_run: NewRun, seed: str) -> None:
    run_id = new_run_id()
    with pytest.raises(RunnerError) as raised:
        await new_run(run_id=run_id, seed=seed)
    assert DISK_REASON in str(raised.value)
    assert await RunnerClient(RUNNER_SOCKET).status(run_id) == DISK_REASON


async def test_a_seed_that_writes_past_the_disk_budget_is_stopped(new_run: NewRun) -> None:
    # It would write a GiB and then wait, so only the runner's watch can end it in time.
    await _stopped_for_disk(new_run, f"{PROBE} fill 1024")


async def test_a_sparse_file_counts_at_its_full_size(new_run: NewRun) -> None:
    await _stopped_for_disk(new_run, f"{PROBE} sparse 1024")


async def test_a_seed_that_makes_a_deep_tree_is_stopped_and_its_folder_removed(
    new_run: NewRun,
) -> None:
    """Audit V-01: code under test chooses how deep its tree is. A chain of 2,000 folders
    with 255-byte names is past the measure's depth, and past what the runner can reach at
    all, since its container's confinement names each file by its whole path, so the run is
    stopped for its disk, and the runner removes it holding a few folders open and no long
    path: the status carries no stop that could not finish, which it would if the folder had
    stayed."""
    await _stopped_for_disk(new_run, f"{PROBE} chain 2000")
