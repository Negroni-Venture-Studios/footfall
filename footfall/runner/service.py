"""The runner's work: prepare a run, start its containers, read their logs and stop it.

The runner is the only part of Footfall that holds the Docker socket, and it runs as root, so it
trusts nothing it is asked. Every container's settings come from `spec.container_config`; a
request supplies only a run id, copy names, commands, a port and host names, and each is checked
here before anything starts. A workspace must lie inside the workspaces root and hold nothing but
files, folders and symbolic links, at most `MAX_DEPTH` folders deep.

Every copy is one pass by file descriptor: each folder is opened relative to its parent and
never through a link, and links are copied as links. A link planted in a workspace, or swapped in
while it is read, therefore cannot pull a host file into a container. The copy also relies on
nothing writing to its source meanwhile. No container ever mounts the workspace, and install and
seed are removed before `prepared` is copied for each app.

A container is allowed out through the egress proxy only once its address on the run's network is
known, and its allow list is revoked when it goes; a stop also revokes every list the run holds
before its network goes. Each run has a disk budget, measured as what a
copy of its folder would write: before and after every copy, after each preparation step, and
every `disk_interval` seconds while it lives. A run that goes over it, or cannot be measured, is
stopped, and `status` says why. Stopping fails closed: if a container or its allow list cannot be
removed, the run keeps its network and folder, and the watch tries again.

The methods block, since Docker calls do. The server runs each request in a thread of its own,
so each run carries a lock, and a long wait for a step to finish lets go of it so that `stop`, or
the disk watch, can still end the run.
"""

import contextlib
import errno
import functools
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Final, Literal, Protocol

from footfall.hosts import HostError, validate_host_name
from footfall.manifest import RUNTIMES
from footfall.runner.engine import Engine, EngineError
from footfall.runner.spec import APP_UID, container_config, network_config

logger = logging.getLogger(__name__)

REGISTRIES: Final = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
DISK_REASON: Final = "disk budget exceeded"
MEASURE_REASON: Final = "disk usage could not be measured"
# A run whose files keep vanishing or changing under the measure could keep part of its size
# out of every measure, so this many checks in a row whose measure skipped something, and
# whose re-measure skipped something too, stop it.
UNSTEADY_MEASURES: Final = 3
UNSTEADY_REASON: Final = (
    f"{MEASURE_REASON}: the run's files kept changing while they were measured; "
    "an app should keep its scratch files in /tmp"
)
NEVER_PREPARED: Final = "stopped before it was prepared"
MAX_DEPTH: Final = 100  # folders below a workspace or prepared app
# How many stopped run ids the runner remembers, to refuse each if it is prepared again, and
# to say why it was stopped. Run ids are random, so one is reused only by mistake, and a
# mistake that comes this late is not one the runner needs to catch.
REMEMBERED_STOPS: Final = 10_000
BROWSERS_PORT: Final = 3000

# The browser container's port is on the run's internal network, which the app containers
# share. Playwright's run-server launches a browser for any client that reaches it, and gives
# its path to anyone who asks `GET /json`, so it listens on loopback alone. In front of it,
# the image's gate (images/browsers/gate.js) listens on every interface and passes on only a
# WebSocket upgrade for a path made fresh for each run, which only the orchestrator is told.
# run-server checks the path too. The image's own command is a default this always replaces.
_GATE: Final = "/opt/footfall/gate.js"
_RUN_SERVER_PORT: Final = 3001
_BROWSERS_PATH_BYTES: Final = 24


def _browsers_command(path: str) -> tuple[str, ...]:
    return (
        "node",
        _GATE,
        "--host",
        "0.0.0.0",  # noqa: S104
        "--port",
        str(BROWSERS_PORT),
        "--upstream",
        str(_RUN_SERVER_PORT),
        "--path",
        path,
        "--",
        "playwright",
        "run-server",
        "--port",
        str(_RUN_SERVER_PORT),
        "--host",
        "127.0.0.1",
        "--path",
        path,
    )


_RUN_ID: Final = re.compile(r"r_[a-z2-7]{26}")
_COPY: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")
_PREPARE: Final = "prepare"  # the copy name of the install and seed containers
_PREPARED: Final = "prepared"
_SEED_ENV: Final = frozenset({"FOOTFALL_USERNAME", "FOOTFALL_PASSWORD"})
_SPEC_RUNTIMES: Final = frozenset({"runsc", "runc"})
_LOG_LINES: Final = 200
_TAIL_CHARS: Final = 20_000
_REDACTED: Final = "[redacted]"
_BLOCK: Final = 512  # st_blocks counts 512-byte units
_PAGE: Final = 4096  # the least any name is charged, so the budget also bounds entries
_RUN_DEPTH: Final = MAX_DEPTH + 2  # below a run's folder: copies, the copy, then its tree
_RETRY_FIRST: Final = 15.0
_RETRY_MOST: Final = 600.0
_CHUNK: Final = 1024 * 1024
_FOLDER_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class RunnerError(Exception):
    """A request the runner refused, or a step that failed. `log_tail` holds the step's output."""

    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


class _TooDeep(Exception):
    """A tree is deeper than the measure goes, which counts as over budget."""


class _OverBudget(Exception):
    """A copy would write more than the run's disk budget allows."""


class ProxyControl(Protocol):
    """What the runner needs from the egress proxy: an allow list for each container's address.

    `allow` replaces any list the same run already holds for the address, and refuses, with a
    ValueError, an address another run still holds. `revoke_run` revokes every list held for a
    run. `listen` has the proxy listen at its own address on a run's network, for that run,
    until `revoke_run`; it refuses, with a ValueError, an address another run listens on or one
    it cannot listen on. Implementations raise OSError when the proxy cannot be reached.
    """

    def allow(self, ip: str, run_id: str, hosts: Sequence[str]) -> None: ...
    def revoke(self, ip: str) -> None: ...
    def revoke_run(self, run_id: str) -> None: ...
    def reached(self, run_id: str) -> list[str]: ...
    def listen(self, ip: str, run_id: str) -> None: ...


def _chown_to_app(path: Path) -> None:
    """Give a path to the containers' user, changing a link itself rather than its target."""
    os.lchown(path, APP_UID, APP_UID)


@dataclass(eq=False)
class _Run:
    """What the runner has made for one run, guarded by its lock."""

    run_id: str
    image: str
    directory: Path
    secrets: tuple[str, ...]
    lock: threading.Lock = field(default_factory=threading.Lock)
    proxy_url: str = ""
    prepared: bool = False
    closed: bool = False
    browsers: bool = False
    containers: dict[str, str | None] = field(default_factory=dict)  # name or id -> address
    copies: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)  # why the last stop did not finish
    unsteady: int = 0  # measures in a row that skipped something, guarded by the service's lock
    retry_delay: float = 0.0
    retry_at: float = 0.0

    @property
    def network(self) -> str:
        return _network_name(self.run_id)


# ---------------------------------------------------------------- checks


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else f"{text[:57]}..."


def _network_name(run_id: str) -> str:
    return f"footfall-{run_id}"


def _container_name(run_id: str, role: str, copy: str) -> str:
    return f"footfall-{run_id}-{role}-{copy}"


def _without(error: Exception, secret: str, shown: str) -> RunnerError:
    """A RunnerError saying what `error` said, with `secret` shown as `shown` instead, in its
    message and log tail alike, and with no chain back to `error`."""
    message = (str(error) or type(error).__name__).replace(secret, shown)
    tail = error.log_tail if isinstance(error, RunnerError) else ""
    return RunnerError(message, tail.replace(secret, shown))


def _check_run_id(run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise RunnerError(f"invalid run id: {_show(run_id)}")


def _check_copy(copy: str) -> None:
    if not _COPY.fullmatch(copy):
        raise RunnerError(f"invalid copy name: {_show(copy)}")


def _check_command(command: str | None, what: str) -> None:
    if command is not None and (not command.strip() or "\x00" in command):
        raise RunnerError(f"{what} must be a command that is not blank and has no NUL character")


def _check_port(port: int) -> None:
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise RunnerError(f"port must be between 1 and 65535, not {_show(port)}")


def _hosts(hosts: Sequence[str], what: str) -> list[str]:
    """Return the host names in their checked, lower-case form, without repeats."""
    if isinstance(hosts, str):
        raise RunnerError(f"{what} must be a list of host names")
    names: list[str] = []
    for host in hosts:
        try:
            name = validate_host_name(host)
        except HostError as error:
            raise RunnerError(f"{what}: {error}") from error
        if name not in names:
            names.append(name)
    return names


def _seed_env(env: Mapping[str, str]) -> dict[str, str]:
    for key, value in env.items():
        if key not in _SEED_ENV:
            raise RunnerError(f"env key not allowed: {_show(key)}")
        if "\x00" in value or "\n" in value:
            raise RunnerError(f"env value for {key} contains NUL or a newline")
    return dict(env)


@contextlib.contextmanager
def _as_runner_error() -> Iterator[None]:
    """Turn the failures the runner expects (Docker, the proxy, the disk) into RunnerError."""
    try:
        yield
    except (EngineError, OSError, ValueError, RecursionError) as error:
        raise RunnerError(str(error) or type(error).__name__) from error


# ---------------------------------------------------------------- the filesystem


def _kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device file"
    return "a special file"


def _open_folder(root: Path, relative: PurePath) -> int:
    """Open a folder below a trusted `root` one name at a time, never through a link."""
    folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in relative.parts:
            if name in (".", ".."):
                raise RunnerError(f"not a plain path: {_show(str(relative))}")
            inner = os.open(name, _FOLDER_FLAGS, dir_fd=folder)
            os.close(folder)
            folder = inner
    except BaseException:
        os.close(folder)
        raise
    return folder


class _Changed(Exception):
    """A folder was not there, or not the same folder, when the walk came to open it."""

    def __init__(self, where: PurePath) -> None:
        super().__init__(str(where))
        self.where = where


class _Deep(Exception):
    """A folder lies deeper below the top than the walk goes."""

    def __init__(self, where: PurePath) -> None:
        super().__init__(str(where))
        self.where = where


_TOP: Final = PurePath()


@dataclass
class _Tally:
    """What a walk that goes on past changes skipped: names that vanished, folders changed."""

    skipped: int = 0


def _walk(
    folder: int, max_depth: int, *, tally: _Tally | None, here: PurePath = _TOP
) -> Generator[tuple[PurePath, int, str, os.stat_result]]:
    """Yield (where, its descriptor, name, lstat) for everything below an open folder.

    The walk goes by descriptor, depth first, reading each folder as it goes, so it holds one
    listing and one descriptor for each folder on the path it is on, and nothing for the
    folders it has finished or not yet reached. Each folder is yielded before anything in it.
    A folder is opened by name without following a link, and must then be the folder that was
    listed, so a folder swapped for a link is never walked through, or even opened. A folder
    more than `max_depth` below the top raises `_Deep`. With a `tally`, a name removed or a
    folder changed while the walk reads is skipped and counted, and the walk goes on; without
    one, a removed name raises its error and a changed folder raises `_Changed`.
    """
    with os.scandir(folder) as listing:
        for entry in listing:
            name = entry.name
            try:
                info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                if tally is None:
                    raise
                tally.skipped += 1
                continue
            where = here / name
            if stat.S_ISDIR(info.st_mode) and len(here.parts) >= max_depth:
                raise _Deep(where)
            yield here, folder, name, info
            if not stat.S_ISDIR(info.st_mode):
                continue
            try:
                inner = os.open(name, _FOLDER_FLAGS, dir_fd=folder)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    raise
                if tally is None:
                    raise _Changed(where) from None
                tally.skipped += 1
                continue
            try:
                if os.path.samestat(info, os.fstat(inner)):
                    yield from _walk(inner, max_depth, tally=tally, here=where)
                elif tally is None:
                    raise _Changed(where)
                else:
                    tally.skipped += 1
            finally:
                os.close(inner)


def _entries(folder: int, what: str) -> Generator[tuple[PurePath, int, str, os.stat_result]]:
    """Yield (where, its descriptor, name, lstat) for everything below an open folder.

    The walk goes by descriptor, depth first, and never into or through a link. It refuses
    anything but files, folders and links, a tree more than `MAX_DEPTH` folders deep, and a
    folder that turns into something else before it is walked. Each folder is yielded before
    anything in it.
    """
    try:
        for here, dirfd, name, info in _walk(folder, MAX_DEPTH, tally=None):
            if not stat.S_ISDIR(info.st_mode) and not (
                stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            ):
                raise RunnerError(
                    f"{what} contains {_kind(info.st_mode)} at {_show(str(here / name))}; "
                    "only files, folders and symbolic links are copied"
                )
            yield here, dirfd, name, info  # one at a time: a folder may hold millions
    except _Deep as deep:
        raise RunnerError(
            f"{what} is more than {MAX_DEPTH} folders deep at {_show(str(deep.where))}"
        ) from None
    except _Changed as changed:
        where = _show(str(changed.where))
        raise RunnerError(f"{what} changed while it was read, at {where}") from None


def _copy_file(folder: int, name: str, target: Path, allowed: int) -> int:
    """Copy one regular file, named relative to an open folder; return the bytes written.

    Checking the opened descriptor rather than the name means a FIFO or a link swapped in is
    refused instead of read, and opening without blocking means a FIFO cannot hang the runner.
    The target must not exist. Set-user-id, set-group-id and sticky bits are dropped. Raises
    _OverBudget once more than `allowed` bytes have been read.
    """
    reader = os.open(name, _READ_FLAGS, dir_fd=folder)
    try:
        info = os.fstat(reader)
        if not stat.S_ISREG(info.st_mode):
            raise RunnerError(
                f"{_show(name)} changed into {_kind(info.st_mode)} while it was copied"
            )
        writer = os.open(target, _WRITE_FLAGS, 0o600)
        written = 0
        try:
            while chunk := os.read(reader, _CHUNK):
                written += len(chunk)
                if written > allowed:
                    raise _OverBudget
                view = memoryview(chunk)
                while view:
                    view = view[os.write(writer, view) :]
            os.fchmod(writer, stat.S_IMODE(info.st_mode) & 0o777)
            os.utime(writer, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(writer)
    finally:
        os.close(reader)
    return written


def _copy_tree(
    root: Path,
    relative: PurePath,
    target: Path,
    *,
    limit: int,
    chown: Callable[[Path], None],
    what: str,
) -> int:
    """Copy the folder `relative` below `root` to a new `target` in one pass; return the bytes.

    Links stay links. Every entry made is passed to `chown`. Each entry is charged as the
    measure charges it, at least one 4 KiB block and a folder at its size, and _OverBudget is
    raised once the charges come to more than `limit`.
    """
    source = _open_folder(root, relative)
    try:
        top = os.fstat(source)
        folders = [(target, top.st_mode)]
        target.mkdir(mode=0o700)
        chown(target)
        written = _size(top)
        for here, dirfd, name, info in _entries(source, what):
            if written > limit:
                raise _OverBudget
            destination = target / here / name
            if stat.S_ISDIR(info.st_mode):
                destination.mkdir(mode=0o700)
                folders.append((destination, info.st_mode))
                written += _size(info)  # a folder of many names is larger than a block
            elif stat.S_ISLNK(info.st_mode):
                link = os.readlink(name, dir_fd=dirfd)
                destination.symlink_to(link)
                written += _charge(len(os.fsencode(link)))
            else:
                written += _charge(_copy_file(dirfd, name, destination, limit - written))
            chown(destination)
        if written > limit:
            raise _OverBudget
        for path, mode in reversed(folders):  # last, so that no folder is closed to its contents
            path.chmod(stat.S_IMODE(mode) & 0o777)
    finally:
        os.close(source)
    return written


def _scan_tree(root: Path, relative: PurePath, what: str) -> None:
    """Refuse a folder below `root` holding anything a copy would refuse."""
    source = _open_folder(root, relative)
    try:
        for _entry in _entries(source, what):
            pass
    finally:
        os.close(source)


def _charge(size: int) -> int:
    """A size rounded up to whole 4 KiB blocks, and never less than one."""
    return max(_PAGE, -(-size // _PAGE) * _PAGE)


def _size(info: os.stat_result) -> int:
    return max(_charge(info.st_size), info.st_blocks * _BLOCK)


def _measure(top: Path, max_depth: int) -> tuple[int, int]:
    """Bytes that copying `top` would write, never following a link, and how many entries the
    measure skipped because they vanished or changed while it read.

    Every name counts at the larger of its size, rounded up to whole 4 KiB blocks, and its
    allocated blocks, and at least one block. So a sparse file counts in full, each hard link
    counts again, as a copy writes them, and the budget bounds how many entries a run holds.
    A missing `top` measures 0. A folder more than `max_depth` below `top` raises _TooDeep,
    so the walk never holds more descriptors than that. A folder swapped for a link while it
    is measured is never opened through the link. An entry that vanishes, or a folder that
    changes, is skipped and counted: what it held is not in the total, which is why a run
    whose measures keep skipping is stopped. Any other failure is raised.
    """
    try:
        info = top.lstat()
    except FileNotFoundError:
        return 0, 0
    total = _size(info)
    if not stat.S_ISDIR(info.st_mode):
        return total, 0
    try:
        folder = os.open(top, _FOLDER_FLAGS)
    except FileNotFoundError:
        return 0, 0
    tally = _Tally()
    try:
        if not os.path.samestat(info, os.fstat(folder)):
            return 0, 1  # replaced while being measured
        for _, _, _, entry in _walk(folder, max_depth, tally=tally):
            total += _size(entry)
    except _Deep as deep:
        raise _TooDeep(f"more than {max_depth} folders deep at {_show(str(deep.where))}") from None
    finally:
        os.close(folder)
    return total, tally.skipped


def _measure_settled(top: Path, max_depth: int) -> tuple[int, int]:
    """A measure, taken again at once if it skipped something. A file or folder replaced
    once, as a dev server replaces a cache folder while it starts, is found by the second;
    a run that keeps changing its files makes both skip, and then the larger of the two is
    the usage, since each missed what it skipped."""
    usage, skipped = _measure(top, max_depth)
    if skipped:
        again, skipped = _measure(top, max_depth)
        usage = max(usage, again) if skipped else again
    return usage, skipped


def _disk_usage(top: Path, max_depth: int = MAX_DEPTH) -> int:
    """Bytes that copying `top` would write, as `_measure` counts them."""
    return _measure(top, max_depth)[0]


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


# ---------------------------------------------------------------- the service


class RunnerService:
    """Prepares runs and starts, inspects and stops their containers, with fixed settings."""

    def __init__(
        self,
        engine: Engine,
        proxy: ProxyControl,
        *,
        workspaces: Path,
        runs: Path,
        images: Mapping[str, str],
        browsers_image: str,
        proxy_container: str,
        orchestrator_container: str,
        proxy_port: int = 3128,
        step_timeout: float = 300,
        runtime: str = "runsc",
        disk_budget: int = 8 * 1024**3,
        disk_interval: float = 15.0,
        chown: Callable[[Path], None] = _chown_to_app,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if runtime not in _SPEC_RUNTIMES:
            raise ValueError(f"runtime must be runsc or runc, not {runtime!r}")
        unknown = sorted(set(images) - set(RUNTIMES))
        if unknown:
            raise ValueError(f"images for unknown runtimes: {', '.join(unknown)}")
        if not 1 <= proxy_port <= 65535:
            raise ValueError(f"proxy_port out of range: {proxy_port}")
        if step_timeout <= 0 or disk_budget <= 0 or disk_interval <= 0:
            raise ValueError("step_timeout, disk_budget and disk_interval must be positive")
        workspaces_root, runs_root = workspaces.resolve(), runs.resolve()
        if workspaces_root.is_relative_to(runs_root) or runs_root.is_relative_to(workspaces_root):
            raise ValueError("the workspaces and runs folders must not contain each other")
        if runtime == "runc":
            logger.warning("containers will start with runc, without gVisor: development only")

        self._engine = engine
        self._proxy = proxy
        self._workspaces = workspaces_root
        self._runs_root = runs_root
        self._images = dict(images)
        self._browsers_image = browsers_image
        self._proxy_container = proxy_container
        self._orchestrator_container = orchestrator_container
        self._proxy_port = proxy_port
        self._step_timeout = step_timeout
        self._runtime = runtime
        self._disk_budget = disk_budget
        self._disk_interval = disk_interval
        self._chown = chown
        self._clock = clock
        self._lock = threading.Lock()  # guards the three below; never held while taking a run's
        self._runs: dict[str, _Run] = {}
        self._reasons: dict[str, str] = {}
        # The latest run ids stopped, oldest first, so that none of them is used twice.
        self._stopped: dict[str, None] = {}

    # ------------------------------------------------------------ operations

    def prepare(
        self,
        run_id: str,
        workspace: Path,
        runtime: str,
        install: str | None,
        seed: str | None,
        egress: Sequence[str],
        env: Mapping[str, str],
    ) -> None:
        """Copy the workspace into the run, make its network, then run install and seed.

        On any failure everything made for the run is removed again, and RunnerError carries
        the failing step's last output.
        """
        with _as_runner_error():
            _check_run_id(run_id)
            image = self._images.get(runtime) if runtime in RUNTIMES else None
            if image is None:
                raise RunnerError(f"unknown runtime: {_show(runtime)}")
            _check_command(install, "install")
            _check_command(seed, "seed")
            hosts = _hosts(egress, "egress")
            seed_env = _seed_env(env)
            relative = self._workspace(workspace).relative_to(self._workspaces)
            directory = self._runs_root / run_id
            self._refuse_stopped(run_id)
            if directory.is_symlink() or directory.exists():
                raise RunnerError(f"run {run_id} already exists")
            secrets = tuple(v for k, v in env.items() if k == "FOOTFALL_PASSWORD" and v)
            run = _Run(run_id, image, directory, secrets)
            with run.lock:
                # Registered only once locked, so that a stop arriving now waits for prepare.
                self._register(run)
                try:
                    self._prepare_locked(run, relative, install, seed, hosts, seed_env)
                except BaseException:
                    # Even if a stop has closed the run meanwhile, sweep up whatever was made.
                    self._teardown_locked(run)
                    raise

    def start_app(
        self, run_id: str, copy: str, start: str, port: int, egress: Sequence[str]
    ) -> str:
        """Start a copy of the prepared app running `start`, and return its "address:port"."""
        with _as_runner_error():
            _check_run_id(run_id)
            _check_copy(copy)
            _check_command(start, "start")
            _check_port(port)
            hosts = _hosts(egress, "egress")
            run = self._live(run_id)
            with run.lock:
                self._check_ready(run)
                if copy in run.copies:
                    raise RunnerError(f"copy {copy} already exists in run {run_id}")
                run.copies.add(copy)
                try:
                    address = self._start_app_locked(run, copy, start, port, hosts)
                except BaseException:
                    name = _container_name(run_id, "app", copy)
                    if not run.closed and self._forget_locked(run, name):
                        run.copies.discard(copy)
                        remove = functools.partial(_remove_tree, run.directory / "copies" / copy)
                        self._attempt([], f"delete copy {copy}", remove)
                    raise
                return f"{address}:{port}"

    def start_browsers(self, run_id: str, assets: Sequence[str]) -> str:
        """Start the run's browser container and return its Playwright endpoint.

        The endpoint holds the server's secret path, so it goes to the caller and nowhere else,
        not even in the message of an error that quotes it.
        """
        with _as_runner_error():
            _check_run_id(run_id)
            hosts = _hosts(assets, "assets")
            run = self._live(run_id)
            with run.lock:
                self._check_ready(run)
                if run.browsers:
                    raise RunnerError(f"the browsers are already started for run {run_id}")
                name = _container_name(run_id, "browsers", "browsers")
                path = "/" + secrets.token_urlsafe(_BROWSERS_PATH_BYTES)
                config = container_config(
                    role="browsers",
                    run_id=run_id,
                    copy="browsers",
                    image=self._browsers_image,
                    command=_browsers_command(path),
                    workspace=None,
                    network=run.network,
                    proxy_url=run.proxy_url,
                    env={},
                    runtime=self._runtime,
                    proxy_name=self._proxy_container,
                )
                failure: RunnerError | None = None
                try:
                    address = self._launch_locked(run, name, config, hosts)
                except Exception as error:  # noqa: BLE001  # any kind is replaced, none is lost
                    failure = _without(error, path, "[the browsers' path]")
                except BaseException:
                    if not run.closed:
                        self._forget_locked(run, name)
                    raise
                if failure is not None:
                    # Cleaned up and raised here, apart from the error, whose message or chain
                    # may name the path, so that neither the traceback of a clean-up that fails
                    # too, which is logged, nor this error leads back to it.
                    if not run.closed:
                        self._forget_locked(run, name)
                    raise failure
                run.browsers = True
                return f"ws://{address}:{BROWSERS_PORT}{path}"

    def logs(self, run_id: str, copy: str) -> str:
        """Return the last lines an app copy wrote, with the run's password taken out."""
        with _as_runner_error():
            _check_run_id(run_id)
            _check_copy(copy)
            run = self._live(run_id)
            with run.lock:
                self._check_ready(run)
                name = _container_name(run_id, "app", copy)
                if copy not in run.copies or name not in run.containers:
                    raise RunnerError(f"run {run_id} has no copy called {copy}")
                text = self._engine.logs(name, tail=_LOG_LINES)
            return self._clean(run, text)

    def stop(self, run_id: str) -> None:
        """Remove everything the run has, and its folder. Stopping a run twice is harmless.

        A run the runner has no record of, perhaps from before a restart, is found by its
        label and its names, and is recorded as stopped before it was prepared. A run id among
        the latest `REMEMBERED_STOPS` stopped is never prepared again. RunnerError lists anything
        that could not be removed; the run then keeps its network and folder, and the watch
        tries again.
        """
        with _as_runner_error():
            _check_run_id(run_id)
            with self._lock:
                run = self._runs.get(run_id)
                # A stop can overtake its own prepare; recording it makes that prepare refuse.
                never = run is None and run_id not in self._stopped
                self._remember_stop_locked(run_id, NEVER_PREPARED if never else None)
            if run is None:
                run = _Run(run_id, "", self._runs_root / run_id, ())
            with run.lock:
                problems = self._teardown_locked(run)
            if problems:
                raise RunnerError("; ".join(problems))

    def status(self, run_id: str) -> str | None:
        """Why the runner stopped a run of its own accord, and what is left of a stop that
        could not finish; None for a run that is live, or gone without either."""
        _check_run_id(run_id)
        with self._lock:
            reason = self._reasons.get(run_id)
            run = self._runs.get(run_id)
        problems = list(run.problems) if run is not None else []
        if not problems:
            return reason
        note = "could not finish stopping the run: " + "; ".join(problems)
        return f"{reason}; {note}" if reason else note

    def check_runs(self) -> None:
        """Stop every live run over its disk budget or that cannot be measured, and try again
        to finish every stop that could not, waiting longer after each failure."""
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            reason: str | None = None
            if run.closed:
                if self._clock() >= run.retry_at:
                    with run.lock:
                        if run.closed and run.problems and self._clock() >= run.retry_at:
                            self._teardown_locked(run)
                continue
            try:
                usage, skipped = _measure_settled(run.directory, _RUN_DEPTH)
                if usage > self._disk_budget:
                    reason = DISK_REASON
                elif self._unsteady(run, skipped):
                    reason = UNSTEADY_REASON
            except _TooDeep:
                reason = DISK_REASON
            except Exception:
                logger.exception("could not measure the disk use of run %s", run.run_id)
                reason = MEASURE_REASON
            if reason is not None:
                with run.lock:
                    if not run.closed:
                        logger.warning("stopping run %s: %s", run.run_id, reason)
                        self._stop_for_locked(run, reason)

    def watch(self, stop: threading.Event) -> None:
        """Call `check_runs` every `disk_interval` seconds until `stop` is set."""
        while not stop.wait(self._disk_interval):
            try:
                self.check_runs()
            except Exception:
                logger.exception("the check of the runs failed")

    # ------------------------------------------------------------ preparing

    def _prepare_locked(
        self,
        run: _Run,
        relative: PurePath,
        install: str | None,
        seed: str | None,
        hosts: list[str],
        seed_env: dict[str, str],
    ) -> None:
        self._runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run.directory.mkdir(mode=0o700)
        # No container ever mounts the workspace, so nothing changes it while it is copied.
        prepared = run.directory / _PREPARED
        self._copy_locked(run, self._workspaces, relative, prepared, "the workspace")

        self._engine.create_network(network_config(run.run_id))
        for container in (self._proxy_container, self._orchestrator_container):
            self._engine.connect(run.network, container)
        proxy_address = self._engine.address(self._proxy_container, run.network)
        run.proxy_url = f"http://{proxy_address}:{self._proxy_port}"
        # The proxy listens on no interface until told, so that only a run's containers reach
        # it, at its address on their own network; revoke_run closes that again.
        self._proxy.listen(proxy_address, run.run_id)

        if install is not None:
            install_hosts = list(dict.fromkeys([*REGISTRIES, *hosts]))
            self._step_locked(run, "install", install, install_hosts, {})
        if seed is not None:
            self._step_locked(run, "seed", seed, hosts, seed_env)
        _scan_tree(self._runs_root, PurePath(run.run_id, _PREPARED), "the prepared app")
        run.prepared = True

    def _step_locked(
        self,
        run: _Run,
        role: Literal["install", "seed"],
        command: str,
        hosts: list[str],
        env: dict[str, str],
    ) -> None:
        """Run one preparation step in its own container and wait for it to finish."""
        name = _container_name(run.run_id, role, _PREPARE)
        config = container_config(
            role=role,
            run_id=run.run_id,
            copy=_PREPARE,
            image=run.image,
            command=("sh", "-c", command),
            workspace=run.directory / _PREPARED,
            network=run.network,
            proxy_url=run.proxy_url,
            env=env,
            runtime=self._runtime,
            proxy_name=self._proxy_container,
        )
        self._launch_locked(run, name, config, hosts)
        try:
            code = self._wait_unlocked(run, name)
        except (EngineError, OSError):
            self._check_open(run)  # a stop while waiting explains the failure better
            raise
        self._check_open(run)
        tail = self._tail(run, name)
        self._discard_locked(run, name)
        if code is None:
            raise RunnerError(f"{role} did not finish within {self._step_timeout:g} seconds", tail)
        if code != 0:
            raise RunnerError(f"{role} failed with exit code {code}", tail)
        try:
            self._check_budget_locked(run)
        except RunnerError as error:
            raise RunnerError(str(error), tail) from error

    def _wait_unlocked(self, run: _Run, name: str) -> int | None:
        """Wait for a container to exit, without the run's lock; None if it ran out of time."""
        run.lock.release()
        try:
            return self._engine.wait(name, self._step_timeout)
        except TimeoutError:
            return None
        finally:
            run.lock.acquire()

    # ------------------------------------------------------------ the disk budget

    def _check_budget_locked(self, run: _Run, incoming: Path | None = None) -> int:
        """Return the run's disk use, after stopping the run and raising if that use, plus a
        copy of `incoming`, is over budget or cannot be measured."""
        try:
            usage, skipped = _measure_settled(run.directory, _RUN_DEPTH)
            extra = 0 if incoming is None else _disk_usage(incoming)
        except _TooDeep as deep:
            self._stop_for_locked(run, DISK_REASON)
            raise RunnerError(f"{DISK_REASON}: {deep}") from None
        except (OSError, RecursionError) as error:
            self._stop_for_locked(run, MEASURE_REASON)
            raise RunnerError(MEASURE_REASON) from error
        if usage + extra > self._disk_budget:
            self._stop_for_locked(run, DISK_REASON)
            raise RunnerError(DISK_REASON)
        if self._unsteady(run, skipped):
            self._stop_for_locked(run, UNSTEADY_REASON)
            raise RunnerError(UNSTEADY_REASON)
        return usage

    def _unsteady(self, run: _Run, skipped: int) -> bool:
        """Count a settled measure of the run that still skipped something, and say whether
        the last `UNSTEADY_MEASURES` all did; one that skipped nothing starts the count again."""
        with self._lock:
            run.unsteady = run.unsteady + 1 if skipped else 0
            return run.unsteady >= UNSTEADY_MEASURES

    def _copy_locked(
        self, run: _Run, root: Path, relative: PurePath, target: Path, what: str
    ) -> None:
        """Copy a folder into the run, checking its budget before, during and after."""
        usage = self._check_budget_locked(run, root / relative)
        try:
            _copy_tree(
                root,
                relative,
                target,
                limit=self._disk_budget - usage,
                chown=self._chown,
                what=what,
            )
        except _OverBudget:
            self._stop_for_locked(run, DISK_REASON)
            raise RunnerError(DISK_REASON) from None
        self._check_budget_locked(run)

    # ------------------------------------------------------------ apps

    def _start_app_locked(
        self, run: _Run, copy: str, start: str, port: int, hosts: list[str]
    ) -> str:
        copies = run.directory / "copies"
        copies.mkdir(mode=0o700, exist_ok=True)
        target = copies / copy
        # Install and seed were removed before prepare returned, and app containers mount only
        # their own copies, so no container can change `prepared` while it is copied.
        prepared = PurePath(run.run_id, _PREPARED)
        self._copy_locked(run, self._runs_root, prepared, target, "the prepared app")
        config = container_config(
            role="app",
            run_id=run.run_id,
            copy=copy,
            image=run.image,
            command=("sh", "-c", start),
            workspace=target,
            network=run.network,
            proxy_url=run.proxy_url,
            env={"PORT": str(port)},
            runtime=self._runtime,
            proxy_name=self._proxy_container,
        )
        return self._launch_locked(run, _container_name(run.run_id, "app", copy), config, hosts)

    # ------------------------------------------------------------ containers

    def _launch_locked(self, run: _Run, name: str, config: dict[str, Any], hosts: list[str]) -> str:
        """Create and start a container, then allow it `hosts`. Returns its address."""
        earlier = run.containers.get(name)
        if earlier is not None:
            # An earlier container of this name whose allow list could not be revoked: revoke
            # it now, or keep it on record and start nothing.
            self._proxy.revoke(earlier)
        run.containers[name] = None  # recorded first, so that cleanup finds a half-made one
        self._engine.create(name, config)
        self._engine.start(name)
        address = self._engine.address(name, run.network)
        run.containers[name] = address
        self._proxy.allow(address, run.run_id, hosts)
        return address

    def _remove_container_locked(self, run: _Run, name: str, problems: list[str]) -> bool:
        """Revoke a container's allow list and remove it; forget it only if both succeed."""
        address = run.containers.get(name)
        revoked = address is None or self._attempt(
            problems,
            f"revoke the allow list of {name}",
            functools.partial(self._proxy.revoke, address),
        )
        removed = self._attempt(
            problems, f"remove {name}", functools.partial(self._engine.remove, name)
        )
        if revoked and removed:
            run.containers.pop(name, None)
        return revoked and removed

    def _discard_locked(self, run: _Run, name: str) -> None:
        problems: list[str] = []
        if not self._remove_container_locked(run, name, problems):
            raise RunnerError("; ".join(problems))

    def _forget_locked(self, run: _Run, name: str) -> bool:
        """Remove a container after a failure, logging rather than raising what goes wrong."""
        return name not in run.containers or self._remove_container_locked(run, name, [])

    def _tail(self, run: _Run, name: str) -> str:
        try:
            text = self._engine.logs(name, tail=_LOG_LINES)
        except (EngineError, OSError):
            return ""
        return self._clean(run, text)

    def _clean(self, run: _Run, text: str) -> str:
        """Take the run's password out of a log and keep at most its last `_TAIL_CHARS`."""
        for secret in run.secrets:
            text = text.replace(secret, _REDACTED)
        return text[-_TAIL_CHARS:]

    # ------------------------------------------------------------ the record of runs

    def _workspace(self, workspace: Path) -> Path:
        """Return the workspace's real path, which must be a folder inside the workspaces root."""
        if not workspace.is_absolute():
            raise RunnerError(f"workspace must be an absolute path: {_show(str(workspace))}")
        try:
            resolved = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise RunnerError(f"workspace cannot be found: {_show(str(workspace))}") from error
        if (
            resolved == self._workspaces
            or not resolved.is_relative_to(self._workspaces)
            or not resolved.is_dir()
        ):
            raise RunnerError(f"workspace must be a folder inside {self._workspaces}")
        return resolved

    def _refuse_stopped_locked(self, run_id: str) -> None:
        """Refuse a run id that has been stopped; the caller holds the service's lock."""
        reason = self._reasons.get(run_id)
        if reason is not None:
            raise RunnerError(f"run {run_id} was stopped: {reason}")
        if run_id in self._stopped:
            raise RunnerError(f"run {run_id} was stopped, and a run id is never used again")

    def _refuse_stopped(self, run_id: str) -> None:
        with self._lock:
            self._refuse_stopped_locked(run_id)

    def _register(self, run: _Run) -> None:
        with self._lock:
            self._refuse_stopped_locked(run.run_id)
            if run.run_id in self._runs:
                raise RunnerError(f"run {run.run_id} already exists")
            self._runs[run.run_id] = run

    def _live(self, run_id: str) -> _Run:
        self._refuse_stopped(run_id)
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise RunnerError(f"run {run_id} has not been prepared")
        return run

    def _check_open(self, run: _Run) -> None:
        if run.closed:
            with self._lock:
                reason = self._reasons.get(run.run_id)
            raise RunnerError(f"run {run.run_id} was stopped" + (f": {reason}" if reason else ""))

    def _check_ready(self, run: _Run) -> None:
        self._check_open(run)
        if not run.prepared:
            raise RunnerError(f"run {run.run_id} has not been prepared")

    # ------------------------------------------------------------ stopping

    def _stop_for_locked(self, run: _Run, reason: str) -> None:
        with self._lock:
            self._remember_stop_locked(run.run_id, reason)
        self._teardown_locked(run)

    def _remember_stop_locked(self, run_id: str, reason: str | None) -> None:
        """Record a stop, and its reason if it has one, forgetting the oldest stops past
        `REMEMBERED_STOPS`, apart from a run still on record; the caller holds the lock."""
        if reason is not None:
            self._reasons.setdefault(run_id, reason)
        self._stopped.setdefault(run_id, None)
        excess = len(self._stopped) - REMEMBERED_STOPS
        if excess > 0:
            forgotten = [old for old in self._stopped if old not in self._runs][:excess]
            for old in forgotten:
                del self._stopped[old]
                self._reasons.pop(old, None)

    def _teardown_locked(self, run: _Run) -> list[str]:
        """Remove everything the run has, failing closed; return what could not be done.

        Containers go first, each with its allow list. Once all of them are gone, every allow
        list the proxy still holds for the run is revoked, which reaches a stray whose address
        could not be read. Only then does the network go, since Docker may then give its
        addresses to another run's containers, and an allow list left behind would follow the
        address. A run that could not be stopped stays on record with its network and folder,
        for the watch to try again.
        """
        run.closed = True
        problems: list[str] = []
        for name in list(run.containers):
            self._remove_container_locked(run, name, problems)
        if not problems:
            self._remove_strays_locked(run, problems)
        if not problems:
            revoke_run = functools.partial(self._proxy.revoke_run, run.run_id)
            self._attempt(problems, f"revoke the allow lists of run {run.run_id}", revoke_run)
        if not problems:
            network = run.network
            for container in (self._proxy_container, self._orchestrator_container):
                disconnect = functools.partial(self._engine.disconnect, network, container)
                self._attempt(problems, f"disconnect {container} from {network}", disconnect)
            remove_network = functools.partial(self._engine.remove_network, network)
            self._attempt(problems, f"remove the network {network}", remove_network)
            remove_folder = functools.partial(_remove_tree, run.directory)
            self._attempt(problems, f"delete the folder of run {run.run_id}", remove_folder)
        with self._lock:
            run.problems = problems
            if problems:
                # Wait longer before each retry: one that cannot succeed must not hog the runner.
                run.retry_delay = min(_RETRY_MOST, max(_RETRY_FIRST, run.retry_delay * 2))
                run.retry_at = self._clock() + run.retry_delay
                self._runs.setdefault(run.run_id, run)
            elif self._runs.get(run.run_id) is run:
                del self._runs[run.run_id]
        return problems

    def _remove_strays_locked(self, run: _Run, problems: list[str]) -> None:
        """Remove containers carrying the run's label that the runner has no record of."""
        try:
            strays = self._engine.containers(f"footfall.run={run.run_id}")
        except Exception as error:
            logger.exception("could not list the containers of run %s", run.run_id)
            problems.append(f"could not list the containers of run {run.run_id}: {error}")
            return
        for container in strays:
            try:
                run.containers[container] = self._engine.address(container, run.network)
            except (EngineError, OSError) as error:
                # Its allow list cannot be revoked without its address, so the network stays.
                logger.exception("could not read the address of %s", container)
                problems.append(f"could not read the address of {container}: {error}")
                run.containers[container] = None
            self._remove_container_locked(run, container, problems)

    @staticmethod
    def _attempt(problems: list[str], what: str, action: Callable[[], object]) -> bool:
        """Do one cleanup step; log and record a failure rather than stop the cleanup."""
        try:
            action()
        except Exception as error:
            logger.exception("could not %s", what)
            problems.append(f"could not {what}: {error}")
            return False
        return True
