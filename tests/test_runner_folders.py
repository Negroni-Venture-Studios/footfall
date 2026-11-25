"""Where the runner writes: only below the runs folder it holds open, never through a link.

The runner is root and the orchestrator is not, so nothing the orchestrator, or code under test,
can do to a folder may change where the runner writes, what it gives to the containers' user,
what modes it sets, or which folder Docker mounts (audit finding F-02, ruling R66). These tests
plant links where the audit showed the runner followed them, as one user; in a deployment only
root can write in the runs folder at all, which `test_the_runs_folder_must_be_roots_alone`
holds the runner to.
"""

import os
import resource
import stat
from pathlib import Path
from typing import Any

import pytest

from footfall.runner.service import (
    DISK_REASON,
    LIFETIME_REASON,
    SWEPT_REASON,
    RunnerError,
    RunnerService,
    RunnerSetupError,
    _open_path,
)
from tests.runner_fakes import (
    BROWSERS_IMAGE,
    IMAGES,
    ORCHESTRATOR,
    PROXY,
    FakeEngine,
    FakeProxy,
    Harness,
    make_harness,
    recording,
)

RUN = "r_abcdefghijklmnopqrstuvwxyz"
MARKER = "marker-7391.txt"


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return make_harness(tmp_path / "h")


def outside_folder(tmp_path: Path) -> Path:
    outside = tmp_path / "outside-4417"
    outside.mkdir()
    return outside


def contents(folder: Path) -> list[str]:
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


def test_a_copies_folder_that_is_a_link_is_refused(h: Harness, tmp_path: Path) -> None:
    """Audit F-02, part A: a `copies` entry planted as a link to an outside folder between
    prepare and start_app is never written through, and no container mounts it."""
    outside = outside_folder(tmp_path)
    h.workspace(files={MARKER: "the app's own file\n"})
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    (h.runs / RUN / "copies").symlink_to(outside)

    with pytest.raises(RunnerError, match="link"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert contents(outside) == []
    assert not any(outside in path.parents for path in h.chowned)
    assert h.engine.names("create") == []


def test_a_run_folder_swapped_for_another_is_never_written_through(
    h: Harness, tmp_path: Path
) -> None:
    """The run's folder renamed aside and a link put in its place: the runner refuses the link
    rather than following it to the folder it names."""
    outside = outside_folder(tmp_path)
    h.workspace(files={MARKER: "the app's own file\n"})
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    (h.runs / RUN).rename(h.runs / "aside")
    (outside / "prepared").mkdir()
    (outside / "prepared" / "planted.txt").write_text("planted\n")
    (h.runs / RUN).symlink_to(outside)

    with pytest.raises(RunnerError, match="link"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert contents(outside) == ["prepared", "prepared/planted.txt"]
    assert h.engine.names("create") == []


def test_a_folder_swapped_during_a_copy_never_has_its_mode_changed(tmp_path: Path) -> None:
    """Audit F-02, part B: folders the copy has just made, swapped for links to outside entries
    while it runs, are neither written into nor given new modes when the copy closes them."""
    outside = outside_folder(tmp_path)
    stand_in_file = outside / "stand-in.txt"
    stand_in_file.write_text("outside\n")
    stand_in_file.chmod(0o600)
    stand_in_folder = outside / "stand-in-folder"
    stand_in_folder.mkdir(mode=0o700)
    h: Harness  # made below, once the hook that uses it exists
    swapped: list[Path] = []

    def chown_then_swap(path: Path) -> None:
        if path.name == "inner" and not swapped:
            path.rename(path.with_name("inner-aside"))
            path.symlink_to(stand_in_folder)
            swapped.append(path)
        elif path.name == "note-5127.txt" and len(swapped) == 1:
            folder = path.parents[1]  # the copy's `a`, made before what it holds
            folder.rename(folder.with_name("a-aside"))
            folder.symlink_to(stand_in_file)
            swapped.append(folder)

    h = make_harness(tmp_path / "h", chown=chown_then_swap)
    workspace = h.workspace(files={"a/inner/note-5127.txt": "app data\n"})
    (workspace / "a").chmod(0o775)
    (workspace / "a" / "inner").chmod(0o755)
    h.service.prepare(RUN, workspace, "python3.13", None, None, [], {})

    assert len(swapped) == 2
    assert stat.S_IMODE(stand_in_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(stand_in_folder.stat().st_mode) == 0o700
    assert contents(stand_in_folder) == []
    # What was copied went to the folders the runner made, wherever they were moved.
    assert (h.runs / RUN / "prepared" / "a-aside" / "inner-aside" / "note-5127.txt").exists()


def test_a_link_planted_in_a_copy_is_removed_and_not_followed(h: Harness, tmp_path: Path) -> None:
    outside = outside_folder(tmp_path)
    (outside / "keep.txt").write_text("keep\n")
    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    (h.runs / RUN / "copies" / "anna" / "escape").symlink_to(outside)
    (h.runs / RUN / "copies" / "anna" / "deep").mkdir()
    (h.runs / RUN / "copies" / "anna" / "deep" / "escape").symlink_to(outside / "keep.txt")

    h.service.stop(RUN)
    assert not (h.runs / RUN).exists()
    assert (outside / "keep.txt").read_text() == "keep\n"


def test_the_runs_folder_moved_aside_after_start_stops_every_mount(
    h: Harness, tmp_path: Path
) -> None:
    """Docker takes a path, so the runner checks that the path still reaches the runs folder it
    opened at start before it names a folder below it to Docker."""
    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.runs.rename(h.runs.with_name("runs-aside"))
    (h.runs / RUN / "copies" / "anna").mkdir(parents=True)

    with pytest.raises(RunnerError, match="runs folder"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert h.engine.names("create") == []


# ---------------------------------------------------------------- the runs folder at start


def service(workspaces: Path, runs: Path, owner: int) -> RunnerService:
    return RunnerService(
        FakeEngine(),
        FakeProxy(),
        workspaces=workspaces,
        runs=runs,
        images=IMAGES,
        browsers_image=BROWSERS_IMAGE,
        proxy_container=PROXY,
        orchestrator_container=ORCHESTRATOR,
        runs_owner=owner,
    )


def test_the_runs_folder_is_made_roots_alone(tmp_path: Path) -> None:
    (tmp_path / "workspaces").mkdir()
    service(tmp_path / "workspaces", tmp_path / "runs", os.getuid())
    assert stat.S_IMODE((tmp_path / "runs").stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "case", ["group-writable", "other-writable", "another owner", "ancestor writable"]
)
def test_the_runs_folder_must_be_roots_alone(tmp_path: Path, case: str) -> None:
    (tmp_path / "workspaces").mkdir()
    above = tmp_path.resolve() / "above"
    runs = above / "runs"
    runs.mkdir(parents=True, mode=0o700)
    owner = os.getuid()
    match case:
        case "group-writable":
            runs.chmod(0o770)
        case "other-writable":
            runs.chmod(0o702)
        case "another owner":
            owner = os.getuid() + 1
        case "ancestor writable":
            above.chmod(0o777)
    with pytest.raises(RunnerSetupError, match="root"):
        service(tmp_path / "workspaces", runs, owner)
    # The control: the same folder, as it should be, is taken.
    above.chmod(0o700)
    runs.chmod(0o700)
    service(tmp_path / "workspaces", runs, os.getuid())


def _roots_and_sticky_or_closed(folder: Path) -> bool:
    """Whether `folder` and every folder above it are root's and writable by root alone,
    unless sticky."""
    for each in (folder, *folder.parents):
        info = each.stat()
        if info.st_uid != 0 or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            return False
    return True


def test_a_runs_folder_that_is_not_its_owners_is_refused_for_itself() -> None:
    """Part A, Minor 2: the last check, on the runs folder itself, tested on its own. The
    folder lies directly in a sticky folder of root's, so every check above it passes, and it
    is refused only because it is not the owner's."""
    import tempfile

    shared = Path("/tmp").resolve()  # noqa: S108  # made and removed here, by this test
    if not _roots_and_sticky_or_closed(shared):
        pytest.skip("the shared temporary folder is not root's and sticky here")
    runs = Path(tempfile.mkdtemp(dir=shared)).resolve()
    try:
        with pytest.raises(RunnerSetupError) as refused:
            _open_path(runs, os.getuid() + 1)
        assert str(refused.value).startswith(f"{runs} must belong to root")
        assert f"not user {os.getuid()} with mode 700" in str(refused.value)
        os.close(_open_path(runs, os.getuid()))  # the control: its own owner is taken
    finally:
        runs.rmdir()


def test_a_runs_folder_that_cannot_be_made_stops_the_runner_with_a_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Part A, Minor 3: a runs folder the runner cannot make, on a read-only disk say, is a
    setup error that says why, not an OSError with a traceback."""
    import errno as errors

    (tmp_path / "workspaces").mkdir()

    def read_only(*args: Any, **kwargs: Any) -> None:
        raise OSError(errors.EROFS, "Read-only file system")

    monkeypatch.setattr(os, "mkdir", read_only)
    with pytest.raises(RunnerSetupError, match="cannot be made: Read-only file system"):
        service(tmp_path / "workspaces", tmp_path / "runs-6612", os.getuid())


def test_a_link_in_the_runs_setting_is_refused_not_followed(tmp_path: Path) -> None:
    """Part A, Minor 7: the setting is taken as given, not resolved, so a link in it is found
    as the runner opens it, one name at a time, and refused."""
    (tmp_path / "workspaces").mkdir()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(RunnerSetupError, match="link"):
        service(tmp_path / "workspaces", tmp_path / "link" / "runs", os.getuid())
    assert not (real / "runs").exists()
    with pytest.raises(RunnerSetupError, match="link"):
        service(tmp_path / "workspaces", tmp_path / "link", os.getuid())
    # The control: the same folder, named without the link, and the same name with a part
    # that normalises away.
    service(tmp_path / "workspaces", tmp_path / "real" / "." / "runs", os.getuid())
    assert (real / "runs").is_dir()


@pytest.mark.parametrize("where", ["the folder", "a folder above it"])
def test_a_link_on_the_way_to_the_runs_folder_is_refused(tmp_path: Path, where: str) -> None:
    """The path is taken as the setting gives it, never resolved; a link found when it is
    opened, one name at a time, is refused rather than followed."""
    above = tmp_path.resolve() / "above"
    (above / "real").mkdir(parents=True, mode=0o700)
    if where == "the folder":
        (above / "runs").symlink_to(above / "real")
        path = above / "runs"
    else:
        (tmp_path / "link").symlink_to(above)
        path = tmp_path.resolve() / "link" / "real"
    with pytest.raises(RunnerSetupError, match="link"):
        _open_path(path, os.getuid())
    os.close(_open_path(above / "real", os.getuid()))  # the control


def test_a_sticky_ancestor_like_tmp_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "workspaces").mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    service(tmp_path / "workspaces", shared / "runs", os.getuid())
    assert (shared / "runs").is_dir()


# ---------------------------------------------------------------- removing a deep tree


def make_chain(top: Path, depth: int) -> None:
    folder = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=folder)
            inner = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=folder)
            os.close(folder)
            folder = inner
        os.close(os.open("leaf.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=folder))
    finally:
        os.close(folder)


def test_a_deep_tree_is_removed_with_few_descriptors(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit V-01: code under test chooses how deep the tree in its copy is, and the stop must
    remove it holding a bounded number of descriptors, not one for each level."""
    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    make_chain(h.runs / RUN / "copies" / "anna", 400)

    open_now: set[int] = set()
    most = 0
    real_open, real_close = os.open, os.close

    def counting_open(*args: object, **kwargs: object) -> int:
        nonlocal most
        fd = real_open(*args, **kwargs)  # type: ignore[arg-type]
        open_now.add(fd)
        most = max(most, len(open_now))
        return fd

    def counting_close(fd: int) -> None:
        open_now.discard(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", counting_open)
    monkeypatch.setattr(os, "close", counting_close)
    h.service.stop(RUN)
    monkeypatch.undo()
    assert not (h.runs / RUN).exists()
    assert most <= 8, f"the stop held {most} descriptors at once"


def test_a_deep_tree_is_removed_under_a_low_open_file_limit(h: Harness) -> None:
    """The same, as the audit checked it: under a limit of 256 open files, a tree 300 folders
    deep, which the measure refuses as too deep, is still removed."""
    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    make_chain(h.runs / RUN / "copies" / "anna", 300)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    in_use = sum(1 for _ in Path("/dev/fd").iterdir())
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, in_use + 64), hard))
    try:
        h.service.stop(RUN)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert not (h.runs / RUN).exists()


# ---------------------------------------------------------------- what an earlier runner left


def restarted(h: Harness, **settings: object) -> RunnerService:
    """A second runner over the same Docker, proxy and folders, as after a restart."""
    return RunnerService(
        h.engine,
        h.proxy,
        workspaces=h.workspaces,
        runs=h.runs,
        images=IMAGES,
        browsers_image=BROWSERS_IMAGE,
        proxy_container=PROXY,
        orchestrator_container=ORCHESTRATOR,
        chown=recording(h.chowned.append),
        runs_owner=os.getuid(),
        clock=h.clock,
        **settings,  # type: ignore[arg-type]
    )


OTHER = "r_" + "q" * 26
THIRD = "r_" + "t" * 26


def test_a_runner_removes_what_an_earlier_runner_left_before_it_serves(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Audit F-08, ruling R67: a run the runner has no record of is never measured or ended,
    so at start it removes every labelled container and network and every run folder."""
    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    (h.runs / OTHER / "prepared").mkdir(parents=True)  # a folder whose run Docker forgot
    (h.runs / OTHER / "prepared" / "left.txt").write_text("left\n")
    h.engine.create_network({"Name": f"footfall-{THIRD}", "Labels": {"footfall.run": THIRD}})

    later = restarted(h)
    with caplog.at_level("WARNING", logger="footfall.runner.service"):
        assert later.sweep() == sorted([RUN, OTHER, THIRD])

    assert h.engine.live == {}
    assert h.engine.networks == {}
    assert h.engine.attached == set()
    assert h.proxy.allowed == {}
    assert list(h.runs.iterdir()) == []
    for run_id in (RUN, OTHER, THIRD):
        assert ("revoke_run", run_id) in h.proxy.calls
        assert later.status(run_id) == SWEPT_REASON
        assert any(run_id in record.getMessage() for record in caplog.records)
    with pytest.raises(RunnerError, match="was stopped"):
        later.start_app(RUN, "anna", "python3 serve.py", 8000, [])


def test_a_labelled_network_of_another_name_is_left_alone_and_named(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Part A, Minor 6: the sweep removes a run's network by the name Footfall gives it, so a
    network labelled with a run id under any other name is not that network. It is named in
    the log and left, rather than logged as removed and left."""
    h.engine.create_network({"Name": "elsewhere-2286", "Labels": {"footfall.run": THIRD}})
    with caplog.at_level("WARNING", logger="footfall.runner.service"):
        assert restarted(h).sweep() == []
    assert "elsewhere-2286" in h.engine.networks
    said = [record.getMessage() for record in caplog.records]
    assert any("elsewhere-2286" in line and "alone" in line for line in said), said
    assert not any("removing" in line for line in said), said


def test_a_sweep_with_nothing_left_does_nothing(h: Harness) -> None:
    assert restarted(h).sweep() == []
    listing = ("labelled", "labelled_networks")
    assert [call for call in h.engine.calls if call[0] not in listing] == []


def test_a_sweep_that_cannot_list_what_docker_holds_stops_the_runner(h: Harness) -> None:
    from footfall.runner.engine import EngineError

    h.engine.fail("labelled", EngineError("Cannot connect to the Docker daemon"))
    with pytest.raises(EngineError):
        restarted(h).sweep()


def test_a_run_the_sweep_cannot_remove_is_kept_for_the_watch(h: Harness) -> None:
    from footfall.runner.engine import EngineError

    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.engine.fail("remove_network", EngineError("network is busy"))
    later = restarted(h)
    later.sweep()
    status = later.status(RUN)
    assert status is not None and "network is busy" in status
    assert f"footfall-{RUN}" in h.engine.networks

    h.engine.failures.clear()
    h.clock.advance(60)
    later.check_runs()
    assert h.engine.networks == {}
    assert not (h.runs / RUN).exists()
    assert later.status(RUN) == SWEPT_REASON


def test_a_folder_that_is_not_a_run_is_left_and_named(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    (h.runs / "lost+found").mkdir()
    with caplog.at_level("WARNING", logger="footfall.runner.service"):
        assert restarted(h).sweep() == []
    assert (h.runs / "lost+found").is_dir()
    assert any("lost+found" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------- a run's lifetime


def test_a_run_past_its_lifetime_is_stopped(tmp_path: Path) -> None:
    h = make_harness(tmp_path / "h", lifetime=1234.0)
    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.clock.advance(1233)
    h.service.check_runs()
    assert h.service.status(RUN) is None
    assert h.engine.live != {}
    h.clock.advance(2)
    h.service.check_runs()
    assert h.service.status(RUN) == LIFETIME_REASON
    assert h.engine.live == {}
    assert not (h.runs / RUN).exists()


def test_a_lifetime_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lifetime"):
        make_harness(tmp_path / "h", lifetime=0)


def test_a_tree_whose_paths_are_too_long_to_reach_counts_as_too_deep(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the real stack the runner's container is confined by a profile that names each file
    by its whole path, so a folder below a long enough chain of long names cannot be read at
    all (ENAMETOOLONG), well short of the measure's depth. It counts as too deep, and so over
    the budget, as a deeper tree does, rather than as a measure that failed."""
    import errno as errors

    h.workspace()
    h.service.prepare(RUN, h.workspaces / "app", "python3.13", None, None, [], {})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    (h.runs / RUN / "copies" / "anna" / "unreachable-5531").mkdir()
    real_stat = os.stat
    refused: list[str] = []

    def stat_or_too_long(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        # Only the measure's look is refused: the removal never goes so deep (see V-01).
        if name == "unreachable-5531" and not refused:
            refused.append(name)
            raise OSError(errors.ENAMETOOLONG, "File name too long", name)
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_or_too_long)
    h.service.check_runs()
    monkeypatch.undo()
    assert refused == ["unreachable-5531"]
    assert h.service.status(RUN) == DISK_REASON
    assert not (h.runs / RUN).exists()
