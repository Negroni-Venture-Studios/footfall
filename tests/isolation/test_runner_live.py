"""The runner in the real stack: its socket, its binds and its disk budget.

The test compose file gives the runner a small disk budget, `FOOTFALL_DISK_BUDGET_MIB`, so
that a seed can go over it in seconds rather than by writing gigabytes.
"""

import os
import secrets
import stat

import pytest

from footfall.local import new_run_id
from footfall.runner.client import RunnerClient
from footfall.runner.service import DISK_REASON, RunnerError
from tests.isolation.conftest import NewRun
from tests.isolation.live import PROBE, RUNNER_SOCKET

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
