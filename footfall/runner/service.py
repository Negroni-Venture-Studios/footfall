"""The runner's work: prepare a run, start its containers, read their logs and stop it.

The runner is the only part of Footfall that holds the Docker socket, and it runs as root, so it
trusts nothing it is asked. Every container's settings come from `spec.container_config`; a
request supplies only a run id, copy names, commands, a port and host names, and each is checked
here before anything starts. A workspace must lie inside the workspaces root and hold nothing but
files, folders and symbolic links. Every copy keeps those links as links and never follows one,
so a link planted in a workspace cannot pull a host file into a container.

A container is allowed out through the egress proxy only once its address on the run's network is
known, and its allow list is revoked when it goes. Each run has a disk budget, checked after each
preparation step and every `disk_interval` seconds while its containers live; a run that goes
over it is stopped, and `status` says why.

The methods block, since Docker calls do. The server runs each request in a thread of its own,
so each run carries a lock, and a long wait for a step to finish lets go of it so that `stop`, or
the disk watch, can still end the run.
"""

import contextlib
import functools
import logging
import os
import re
import shutil
import stat
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from footfall.hosts import HostError, validate_host_name
from footfall.manifest import RUNTIMES
from footfall.runner.engine import Engine, EngineError
from footfall.runner.spec import APP_UID, container_config

logger = logging.getLogger(__name__)

REGISTRIES: Final = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
DISK_REASON: Final = "disk budget exceeded"
BROWSERS_PORT: Final = 3000

# The browsers listen on every interface of their own container, which is on the run's internal
# network alone.
_BROWSERS_COMMAND: Final = (
    "playwright",
    "run-server",
    "--port",
    str(BROWSERS_PORT),
    "--host",
    "0.0.0.0",  # noqa: S104
)
_RUN_ID: Final = re.compile(r"r_[a-z2-7]{26}")
_COPY: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")
_PREPARE: Final = "prepare"  # the copy name of the install and seed containers
_SEED_ENV: Final = frozenset({"FOOTFALL_USERNAME", "FOOTFALL_PASSWORD"})
_SPEC_RUNTIMES: Final = frozenset({"runsc", "runc"})
_LOG_LINES: Final = 200
_TAIL_CHARS: Final = 20_000
_REDACTED: Final = "[redacted]"
_BLOCK: Final = 512  # st_blocks counts 512-byte units
_CHUNK: Final = 1024 * 1024


class RunnerError(Exception):
    """A request the runner refused, or a step that failed. `log_tail` holds the step's output."""

    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


class ProxyControl(Protocol):
    """What the runner needs from the egress proxy: an allow list for each container's address.

    Implementations raise OSError when the proxy cannot be reached.
    """

    def allow(self, ip: str, run_id: str, hosts: Sequence[str]) -> None: ...
    def revoke(self, ip: str) -> None: ...
    def reached(self, run_id: str) -> list[str]: ...


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
    containers: dict[str, str | None] = field(default_factory=dict)  # name -> address
    copies: set[str] = field(default_factory=set)

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
    except (EngineError, OSError, ValueError) as error:
        raise RunnerError(str(error) or type(error).__name__) from error


# ---------------------------------------------------------------- the filesystem


def _reraise(error: OSError) -> None:
    raise error


def _kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "a FIFO"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device file"
    return "a special file"


def _refuse_special_files(top: Path, what: str) -> None:
    """Raise RunnerError if anything below `top` is not a file, a folder or a symbolic link."""
    for folder, _dirs, files, dirfd in os.fwalk(top, follow_symlinks=False, onerror=_reraise):
        for name in files:
            mode = os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode) or stat.S_ISDIR(mode)):
                where = Path(folder, name).relative_to(top)
                raise RunnerError(
                    f"{what} contains {_kind(mode)} at {where}; "
                    "only files, folders and symbolic links are copied"
                )


def _copy_file(source: str, target: str) -> str:
    """Copy one regular file for `copytree`, refusing anything else and never following a link.

    Opening without blocking and checking what was opened, rather than the name, means a FIFO
    or a link swapped in after the check is refused instead of read. Set-user-id, set-group-id
    and sticky bits are dropped.
    """
    reader = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(reader)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{source} is {_kind(info.st_mode)}, which is not copied")
        writer = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while chunk := os.read(reader, _CHUNK):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(writer, view) :]
            os.fchmod(writer, stat.S_IMODE(info.st_mode) & 0o777)
            os.utime(writer, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(writer)
    finally:
        os.close(reader)
    return target


def _copy_tree(source: Path, target: Path) -> None:
    """Copy a folder, keeping symbolic links as links."""
    try:
        shutil.copytree(source, target, symlinks=True, copy_function=_copy_file)
    except shutil.Error as error:
        problems = error.args[0] if error.args and isinstance(error.args[0], list) else []
        detail = str(problems[0][2]) if problems else str(error)
        raise RunnerError(f"could not copy {source.name}: {detail}") from error


def _disk_usage(top: Path) -> int:
    """Bytes of disk used below `top`, never following a link and counting hard links once."""
    seen: set[tuple[int, int]] = set()
    try:
        total = top.lstat().st_blocks * _BLOCK
        for _folder, dirs, files, dirfd in os.fwalk(top, follow_symlinks=False):
            for name in (*dirs, *files):
                try:
                    info = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # removed while being measured
                if info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
                    if (info.st_dev, info.st_ino) in seen:
                        continue
                    seen.add((info.st_dev, info.st_ino))
                total += info.st_blocks * _BLOCK
    except FileNotFoundError:
        return 0
    return total


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
        disk_budget: int = 2 * 1024**3,
        disk_interval: float = 15.0,
        chown: Callable[[Path], None] = _chown_to_app,
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
        self._lock = threading.Lock()  # guards _runs and _reasons; never held while taking a run's
        self._runs: dict[str, _Run] = {}
        self._reasons: dict[str, str] = {}

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
        """Make the run's network and prepared copy of the workspace, then run install and seed.

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
            source = self._workspace(workspace)
            directory = self._runs_root / run_id
            self._refuse_stopped(run_id)
            if directory.is_symlink() or directory.exists():
                raise RunnerError(f"run {run_id} already exists")
            _refuse_special_files(source, "the workspace")
            secrets = tuple(v for k, v in env.items() if k == "FOOTFALL_PASSWORD" and v)
            run = self._register(_Run(run_id, image, directory, secrets))
            with run.lock:
                try:
                    self._prepare_locked(run, source, install, seed, hosts, seed_env)
                except BaseException:
                    if not run.closed:
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
                    run.copies.discard(copy)
                    if not run.closed:
                        self._forget_locked(run, _container_name(run_id, "app", copy))
                        self._attempt([], f"delete copy {copy}", self._copy_dir(run, copy))
                    raise
                return f"{address}:{port}"

    def start_browsers(self, run_id: str, assets: Sequence[str]) -> str:
        """Start the run's browser container and return its Playwright endpoint."""
        with _as_runner_error():
            _check_run_id(run_id)
            hosts = _hosts(assets, "assets")
            run = self._live(run_id)
            with run.lock:
                self._check_ready(run)
                if run.browsers:
                    raise RunnerError(f"the browsers are already started for run {run_id}")
                name = _container_name(run_id, "browsers", "browsers")
                config = container_config(
                    role="browsers",
                    run_id=run_id,
                    copy="browsers",
                    image=self._browsers_image,
                    command=_BROWSERS_COMMAND,
                    workspace=None,
                    network=run.network,
                    proxy_url=run.proxy_url,
                    env={},
                    runtime=self._runtime,
                )
                try:
                    address = self._launch_locked(run, name, config, hosts)
                except BaseException:
                    if not run.closed:
                        self._forget_locked(run, name)
                    raise
                run.browsers = True
                return f"ws://{address}:{BROWSERS_PORT}/"

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
        label and its names. RunnerError lists anything that could not be removed.
        """
        with _as_runner_error():
            _check_run_id(run_id)
            with self._lock:
                run = self._runs.get(run_id)
            if run is None:
                problems = self._sweep(run_id, {})
            else:
                with run.lock:
                    problems = self._teardown_locked(run)
            if problems:
                raise RunnerError("; ".join(problems))

    def status(self, run_id: str) -> str | None:
        """Why the runner stopped a run of its own accord, or None if it has not."""
        _check_run_id(run_id)
        with self._lock:
            return self._reasons.get(run_id)

    def check_disks(self) -> None:
        """Stop every run that is over its disk budget."""
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            try:
                over = _disk_usage(run.directory) > self._disk_budget
            except Exception:
                logger.exception("could not measure the disk use of run %s", run.run_id)
                continue
            if over:
                with run.lock:
                    if not run.closed:
                        logger.warning("run %s is over its disk budget; stopping it", run.run_id)
                        self._stop_for_locked(run, DISK_REASON)

    def watch(self, stop: threading.Event) -> None:
        """Check the runs' disk use every `disk_interval` seconds until `stop` is set."""
        while not stop.wait(self._disk_interval):
            try:
                self.check_disks()
            except Exception:
                logger.exception("the disk check failed")

    # ------------------------------------------------------------ preparing

    def _prepare_locked(
        self,
        run: _Run,
        source: Path,
        install: str | None,
        seed: str | None,
        hosts: list[str],
        seed_env: dict[str, str],
    ) -> None:
        self._engine.create_network(run.network, {"footfall.run": run.run_id})
        for container in (self._proxy_container, self._orchestrator_container):
            self._engine.connect(run.network, container)
        proxy_address = self._engine.address(self._proxy_container, run.network)
        run.proxy_url = f"http://{proxy_address}:{self._proxy_port}"

        self._runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run.directory.mkdir(mode=0o700)
        prepared = run.directory / "prepared"
        _copy_tree(source, prepared)
        self._chown_tree(prepared)
        self._check_disk_locked(run)

        if install is not None:
            install_hosts = list(dict.fromkeys([*REGISTRIES, *hosts]))
            self._step_locked(run, "install", install, install_hosts, {})
        if seed is not None:
            self._step_locked(run, "seed", seed, hosts, seed_env)
        _refuse_special_files(prepared, "the prepared app")
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
            workspace=run.directory / "prepared",
            network=run.network,
            proxy_url=run.proxy_url,
            env=env,
            runtime=self._runtime,
        )
        self._launch_locked(run, name, config, hosts)
        try:
            code = self._wait_unlocked(run, name)
        except (EngineError, OSError):
            self._check_open(run)  # a stop while waiting explains the failure better
            raise
        self._check_open(run)
        over = code == 0 and _disk_usage(run.directory) > self._disk_budget
        tail = self._tail(run, name) if code != 0 or over else ""
        self._discard_locked(run, name)
        if code is None:
            raise RunnerError(f"{role} did not finish within {self._step_timeout:g} seconds", tail)
        if code != 0:
            raise RunnerError(f"{role} failed with exit code {code}", tail)
        if over:
            self._stop_for_locked(run, DISK_REASON)
            raise RunnerError(DISK_REASON, tail)

    def _wait_unlocked(self, run: _Run, name: str) -> int | None:
        """Wait for a container to exit, without the run's lock; None if it ran out of time."""
        run.lock.release()
        try:
            return self._engine.wait(name, self._step_timeout)
        except TimeoutError:
            return None
        finally:
            run.lock.acquire()

    def _check_disk_locked(self, run: _Run) -> None:
        if _disk_usage(run.directory) > self._disk_budget:
            self._stop_for_locked(run, DISK_REASON)
            raise RunnerError(DISK_REASON)

    # ------------------------------------------------------------ apps

    def _copy_dir(self, run: _Run, copy: str) -> Callable[[], None]:
        return functools.partial(_remove_tree, run.directory / "copies" / copy)

    def _start_app_locked(
        self, run: _Run, copy: str, start: str, port: int, hosts: list[str]
    ) -> str:
        copies = run.directory / "copies"
        copies.mkdir(mode=0o700, exist_ok=True)
        target = copies / copy
        _copy_tree(run.directory / "prepared", target)
        self._chown_tree(target)
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
        )
        return self._launch_locked(run, _container_name(run.run_id, "app", copy), config, hosts)

    # ------------------------------------------------------------ containers

    def _launch_locked(self, run: _Run, name: str, config: dict[str, Any], hosts: list[str]) -> str:
        """Create and start a container, then allow it `hosts`. Returns its address."""
        run.containers[name] = None  # recorded first, so that cleanup finds a half-made one
        self._engine.create(name, config)
        self._engine.start(name)
        address = self._engine.address(name, run.network)
        run.containers[name] = address
        self._proxy.allow(address, run.run_id, hosts)
        return address

    def _discard_locked(self, run: _Run, name: str) -> None:
        """Revoke a container's allow list and remove it."""
        address = run.containers.get(name)
        try:
            if address is not None:
                self._proxy.revoke(address)
        finally:
            self._engine.remove(name)
        del run.containers[name]

    def _forget_locked(self, run: _Run, name: str) -> None:
        """Discard a container after a failure, logging rather than raising what goes wrong."""
        if name not in run.containers:
            return
        address = run.containers.pop(name)
        if address is not None:
            self._attempt([], f"revoke {name}", functools.partial(self._proxy.revoke, address))
        self._attempt([], f"remove {name}", functools.partial(self._engine.remove, name))

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

    def _chown_tree(self, top: Path) -> None:
        self._chown(top)
        for folder, dirs, files, _dirfd in os.fwalk(top, follow_symlinks=False, onerror=_reraise):
            for name in (*dirs, *files):
                self._chown(Path(folder, name))

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

    def _refuse_stopped(self, run_id: str) -> None:
        with self._lock:
            reason = self._reasons.get(run_id)
        if reason is not None:
            raise RunnerError(f"run {run_id} was stopped: {reason}")

    def _register(self, run: _Run) -> _Run:
        with self._lock:
            reason = self._reasons.get(run.run_id)
            if reason is not None:
                raise RunnerError(f"run {run.run_id} was stopped: {reason}")
            if run.run_id in self._runs:
                raise RunnerError(f"run {run.run_id} already exists")
            self._runs[run.run_id] = run
        return run

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
            self._reasons.setdefault(run.run_id, reason)
        self._teardown_locked(run)

    def _teardown_locked(self, run: _Run) -> list[str]:
        run.closed = True
        problems = self._sweep(run.run_id, run.containers)
        run.containers.clear()
        with self._lock:
            if self._runs.get(run.run_id) is run:
                del self._runs[run.run_id]
        return problems

    def _sweep(self, run_id: str, known: Mapping[str, str | None]) -> list[str]:
        """Remove every container, connection, network and folder of a run, as far as possible.

        Containers the runner knows are removed by name, then any others carrying the run's
        label. Each allow list is revoked before its container is removed. Returns a
        description of each thing that could not be done.
        """
        network = _network_name(run_id)
        problems: list[str] = []
        for name, address in list(known.items()):
            if address is not None:
                revoke = functools.partial(self._proxy.revoke, address)
                self._attempt(problems, f"revoke the allow list of {name}", revoke)
            self._attempt(problems, f"remove {name}", functools.partial(self._engine.remove, name))
        listing = functools.partial(self._engine.containers, f"footfall.run={run_id}")
        for container in self._attempt(problems, "list the run's containers", listing) or []:
            try:
                address = self._engine.address(container, network)
            except (EngineError, OSError):
                address = None
            if address is not None:
                revoke = functools.partial(self._proxy.revoke, address)
                self._attempt(problems, f"revoke the allow list of {container}", revoke)
            remove = functools.partial(self._engine.remove, container)
            self._attempt(problems, f"remove {container}", remove)
        for container in (self._proxy_container, self._orchestrator_container):
            disconnect = functools.partial(self._engine.disconnect, network, container)
            self._attempt(problems, f"disconnect {container} from {network}", disconnect)
        remove_network = functools.partial(self._engine.remove_network, network)
        self._attempt(problems, f"remove the network {network}", remove_network)
        remove_folder = functools.partial(_remove_tree, self._runs_root / run_id)
        self._attempt(problems, f"delete the folder of run {run_id}", remove_folder)
        return problems

    @staticmethod
    def _attempt[T](problems: list[str], what: str, action: Callable[[], T]) -> T | None:
        """Do one cleanup step; log and record a failure rather than stop the cleanup."""
        try:
            return action()
        except Exception as error:
            logger.exception("could not %s", what)
            problems.append(f"could not {what}: {error}")
            return None
