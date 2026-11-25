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

import pytest

from footfall.runner.service import RunnerError, RunnerService, RunnerSetupError, _open_path
from tests.runner_fakes import (
    BROWSERS_IMAGE,
    IMAGES,
    ORCHESTRATOR,
    PROXY,
    FakeEngine,
    FakeProxy,
    Harness,
    make_harness,
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


@pytest.mark.parametrize("where", ["the folder", "a folder above it"])
def test_a_link_on_the_way_to_the_runs_folder_is_refused(tmp_path: Path, where: str) -> None:
    """The path is resolved once, from the setting; a link found when it is opened, one name
    at a time, is refused rather than followed."""
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
