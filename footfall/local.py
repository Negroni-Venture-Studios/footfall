"""One run of a local folder: what `footfall test` does, from the manifest to the results.

The orchestrator holds the Anthropic key and drives the browsers; the runner, over its socket,
holds Docker. Nothing here starts a container or opens the Docker socket (AGENTS.md rule 1).
A run copies the folder into the workspaces root and asks the runner to prepare it, to start
one copy of the app for each persona and one browser container, and to stop it all at the end,
whatever happened in between. It copies only the files git tracks, so that a `.env` the
developer keeps out of git never reaches a container (AGENTS.md rule 3), whatever the tested
branch's own `.gitignore` says. Untracked files that git does not ignore are copied only when
the caller asks for them, and each is named on standard error first, since the branch's
`.gitignore` decides which they are. A folder outside git is copied only when the caller asks
for every file.

When the manifest has a `[login]` section, the run makes a throwaway username and password.
The password reaches a container only through the seed step's environment (AGENTS.md rule 3).
Footfall signs each persona in before it starts when the brief starts signed in. Either way
the tester is given the account in its first message, and every executor redacts the
password from the trace and the report wherever it appears whole, once characters that show
nothing are taken out, and whatever whitespace stands where it has whitespace. Otherwise the
match is exact: the password typed in two parts, or written in another case, reversed,
encoded or with other characters between its own, is not found, and stays. No log line and
no printed table holds the tester's text at all. A screenshot can still show the password,
if the app displays what was typed in plain text. Hiding it better, in text as in
screenshots, is left to issue #7.

Each persona runs in its own browser context, and a persona that fails, whatever the reason,
ends alone: its context is closed and the others carry on. Each one's report is written as it
ends, and the run's summary once all have.
"""

import asyncio
import base64
import contextlib
import errno
import functools
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, NoReturn, Protocol, TextIO
from urllib.parse import quote as url_quote

from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError

from footfall.actions import mask, no_legacy_commands
from footfall.browsers import Engine, SignInError, connect_run_server, open_context, sign_in
from footfall.manifest import (
    Brief,
    Manifest,
    is_dot_git,
    load_brief,
    load_manifest,
    require_listed,
)
from footfall.personas import Device, Persona, PersonaError, load_devices, load_personas, select
from footfall.runner.service import RunnerError
from footfall.tester import loop as tester_loop
from footfall.tester.folder import FOLDER_MODE, Folder
from footfall.tester.guard import Guard
from footfall.tester.loop import Budget, RunSpend, run_persona
from footfall.tester.model import model_from_spec
from footfall.tester.report import OBSERVATIONS, PersonaReport, clean_text, strip_invisible
from footfall.tester.screen import quote
from footfall.tester.tools import REDACTED, Executor, Redactor
from footfall.tester.trace import Trace

__all__ = [
    "DEFAULT_PROXY_CONTAINER",
    "DEFAULT_WORKSPACES",
    "PROXY_PORT",
    "LocalRunError",
    "NotReadyError",
    "OutputError",
    "Runner",
    "format_table",
    "new_run_id",
    "test_folder",
    "wait_ready",
]

LOG = logging.getLogger(__name__)

DEFAULT_WORKSPACES: Final = Path("/var/lib/footfall/workspaces")
DEFAULT_PROXY_CONTAINER: Final = "footfall-proxy"
PROXY_PORT: Final = 3128
READY_POLL: Final = 0.25  # seconds between two tries of the ready path
READY_TRY: Final = 2.0  # seconds one try may take
GOTO_TIMEOUT_MS: Final = 30_000
LOG_TAIL_CAP: Final = 20_000
GIT_TIMEOUT: Final = 60.0
# Playwright's client keeps no timer for the timeouts it passes to the browser, so every wait
# on the run's browser container is bounded here too. Read when used, so that a test can
# shorten them.
CLOSE_TIMEOUT = 10.0  # seconds for a persona's context to close, after which it is abandoned
# The most a run may spend on model calls unless the operator says otherwise: the design's
# worst case for the default personas, twelve at the default ceiling of $1 each.
RUN_COST_CEILING_USD: Final = 12.0
# Seconds a persona may take beyond what its model calls may: setting up its context and
# signing in, which take at most about two and a quarter minutes by their own timeouts, and
# the browser's part of the turns that run past the time limit, about a minute each.
BROWSER_ALLOWANCE = 600.0
_FOLDER_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# A folder opened only to go through it, which needs search permission and not read: O_PATH
# on Linux, O_SEARCH on macOS, and a plain read where the system has neither.
_SEARCH: Final = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY
# The configuration files of a git folder, which can hold a token in a remote's address.
_GIT_CONFIGS: Final = ("config", "config.worktree")
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_CHUNK: Final = 1024 * 1024
_GIT_PASSED: Final = ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL")
# The errors that mean one part of a path is a link or a file, not a folder.
_NOT_A_FOLDER: Final = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})

type ConnectBrowser = Callable[[str, str], Awaitable[Browser]]


RUNC_WARNING: Final = (
    "footfall: warning: FOOTFALL_RUNTIME=runc starts containers without gVisor, so code "
    "under test runs on the host's own kernel. Use it for development only, never on a server."
)


class Runner(Protocol):
    """What a run needs from the runner; `RunnerClient` is the real one. `prepare` returns the
    runtime the run's containers start under, as the runner says, or None."""

    async def prepare(
        self,
        run_id: str,
        workspace: Path,
        runtime: str,
        install: str | None,
        seed: str | None,
        egress: Sequence[str],
        env: Mapping[str, str],
    ) -> str | None: ...

    async def start_app(
        self, run_id: str, copy: str, start: str, port: int, egress: Sequence[str]
    ) -> str: ...

    async def start_browsers(self, run_id: str, assets: Sequence[str]) -> str: ...

    async def logs(self, run_id: str, copy: str) -> str: ...

    async def stop(self, run_id: str) -> None: ...


class LocalRunError(Exception):
    """A run that could not go ahead. `log_tail` holds the app's last output, if there is one."""

    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


class NotReadyError(LocalRunError):
    """An app copy did not answer its ready path in time."""


class OutputError(LocalRunError):
    """The results folder already holds results that this run would overwrite, or a link
    where results would go."""


def new_run_id() -> str:
    """A fresh run id: `r_` and 26 characters of lower-case base32, 128 random bits."""
    return "r_" + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


async def _answers(host: str, port: int, request: bytes) -> bool:
    """Whether one GET of the ready path gets a 2xx or 3xx answer within `READY_TRY`."""
    try:
        async with asyncio.timeout(READY_TRY):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(request)
                await writer.drain()
                status_line = await reader.readline()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
    except (OSError, TimeoutError, ValueError):
        return False  # ValueError: a line longer than the reader's limit
    parts = status_line.split()
    return len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1][:1] in (b"2", b"3")


async def wait_ready(address: str, path: str, timeout: float) -> None:
    """Poll `http://<address><path>` until it answers with a 2xx or 3xx status.

    `address` is the "host:port" the runner gave. Raises TimeoutError after `timeout`
    seconds. Redirects are not followed: an answer is enough to show the app is up.
    """
    host, _, port = address.rpartition(":")
    target = url_quote(path, safe="/?&=%:@!$'()*+,;~-._")
    request = (
        f"GET {target} HTTP/1.1\r\nHost: {address}\r\nUser-Agent: footfall\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    async with asyncio.timeout(timeout):
        while not await _answers(host, int(port), request):
            await asyncio.sleep(READY_POLL)


def format_table(reports: Sequence[PersonaReport]) -> str:
    """The run's results as a table: one line for each persona, then the total, then a line
    saying that the testers' text in the results is observations."""
    rows = [("persona", "status", "findings", "actions", "cost")]
    for report in reports:
        rows.append(
            (
                report.persona,
                report.status,
                str(len(report.findings)),
                str(report.actions),
                f"${report.cost_usd:.2f}",
            )
        )
    rows.append(
        (
            "total",
            "",
            str(sum(len(r.findings) for r in reports)),
            str(sum(r.actions for r in reports)),
            f"${sum(r.cost_usd for r in reports):.2f}",
        )
    )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    return "\n".join([*lines, "", OBSERVATIONS])


def _failed(
    persona: Persona, device: Device, reason: str, started: float, cost_usd: float
) -> PersonaReport:
    return PersonaReport(
        persona=persona.id,
        device=device.name,
        status="failed",
        reason=reason,
        score=None,
        goal_reached=None,
        summary="",
        findings=[],
        actions=0,
        cost_usd=cost_usd,
        seconds=time.monotonic() - started,
    )


def _write_json(folder: Folder, name: str, data: object, password: str | None) -> None:
    """Write `data` as JSON to the new file `name` in `folder`, with the password taken out
    once more in case anything held it. The file is made exclusively, which never follows a
    link, even one to nothing: a name already there, link or not, raises FileExistsError. It
    is the operator's alone, mode 0o600."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if password:
        text = text.replace(password, REDACTED)
    folder.write(name, text.encode("utf-8"))


type _Identity = tuple[int, int]  # (st_dev, st_ino)


class _Linked:
    """The files in the repository's git folders that have more than one name.

    A hostile archive, once extracted, can leave a hard link in the worktree to any file in
    the git folder: the repository's configuration, a submodule's in `modules/`, another
    worktree's in `worktrees/`, or, for a submodule, its superproject's, each of which can
    hold a token. Walking the git folders is costly, so they are walked only when a file to
    copy has another name too, and then once, each folder once: a folder that lies inside
    another, as a linked worktree's lies in the common one, is walked with it. The walk
    fails closed: a folder in it that cannot be read refuses the copy.
    """

    def __init__(self, folders: Sequence[Path] = (), problem: str | None = None) -> None:
        self._problem = problem  # why the git folders could not be found, if they could not
        tops: list[Path] = []
        for folder in sorted({Path(os.path.realpath(f)) for f in folders}, key=lambda p: p.parts):
            if not any(folder.is_relative_to(top) for top in tops):
                tops.append(folder)
        self._folders = tuple(tops)
        self._found: frozenset[_Identity] | None = None

    def holds(self, info: os.stat_result) -> bool:
        """Whether the file `info` describes is also one of the git folders' files.

        A file with one name is never one of them. For a file with another name, when the
        git folders could not be found, LocalRunError refuses the copy.
        """
        if info.st_nlink <= 1:
            return False
        if self._problem is not None:
            raise LocalRunError(
                f"a file to copy has another name, and {self._problem}, so Footfall cannot "
                f"check that the other name is not in the repository's git folder; {_LINK_ADVICE}"
            )
        if not self._folders:
            return False
        if self._found is None:
            self._found = self._collect()
        return (info.st_dev, info.st_ino) in self._found

    def _collect(self) -> frozenset[_Identity]:
        def unreadable(error: OSError) -> NoReturn:
            why = error.strerror or type(error).__name__
            raise LocalRunError(
                f"a file to copy has another name, and {_shown(error.filename)}, in the "
                f"repository's git folder, could not be read to check that the other name is "
                f"not there ({why}); {_LINK_ADVICE}"
            )

        found: set[_Identity] = set()
        for top in self._folders:
            for path, _, files in os.walk(top, onerror=unreadable):  # never into a link
                for name in files:
                    try:
                        info = (Path(path) / name).lstat()
                    except FileNotFoundError:
                        continue  # gone since the folder was read, as git tidies up
                    except OSError as error:
                        unreadable(error)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                        found.add((info.st_dev, info.st_ino))
        return frozenset(found)


_NO_LINKS: Final = _Linked()


@dataclass(frozen=True)
class _Listing:
    """The files git lists for a folder's copy, and the identities of the repository's git
    folder and its common folder, which differ for a linked worktree, and of the
    configuration files in them, with the files in those folders that have other names."""

    names: list[PurePosixPath]
    git_dirs: frozenset[_Identity]
    untracked: frozenset[PurePosixPath] = frozenset()  # listed by --others, not in the index
    linked: _Linked = _NO_LINKS


_GIT_ADVICE: Final = "pass --all-files to copy every file in the folder, ignored ones included"
# Where --all-files was passed and still needs git's answer about superprojects.
_SUPERPROJECT_ADVICE: Final = (
    "--all-files still asks git whether the folder is in a submodule, to check that no file "
    "is another name for a superproject's configuration; make the repository and any "
    "superproject readable to git where Footfall runs"
)
# Where the hard-link check cannot run, --all-files does not help: it runs the same check.
_LINK_ADVICE: Final = (
    "remove the files' other names, the hard links, or make the repository's git folders "
    "readable where Footfall runs, for example by mounting them as well as the folder"
)
_SMALL_FILE: Final = 4096  # the most read of a .git file or a commondir file
_MOST_SUPERPROJECTS: Final = 32


def _git_env() -> dict[str, str]:
    """The only environment git is given.

    A repository's own configuration can make git run a program, as a lazy fetch through a
    promisor remote whose upload-pack is a shell command does, and that program would get
    git's environment. So git gets no key and nothing else of Footfall's: only where to find
    programs, the user's own configuration (whose excludes file may be what keeps their
    `.env` ignored), and the language. It may not fetch lazily, use any transport at all,
    ask at a terminal, or take a lock in the folder.
    """
    env = {name: os.environ[name] for name in _GIT_PASSED if name in os.environ}
    env |= {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
    }
    return env


def _run_git(
    git: str, folder: Path, arguments: Sequence[str], *, advice: str = _GIT_ADVICE
) -> bytes:
    """Run git in `folder` with `_git_env`, no input and a time limit; return its output.

    The filesystem monitor, which a repository's configuration could point at a program, and
    the untracked cache are off. git's own check of the repository's owner stays on: a
    repository someone else owns is read only where the system configuration trusts it, as
    the footfall image trusts its mount point, /repo. git runs in a session of its own, with
    no controlling terminal, so a configuration that names /dev/tty fails at once rather than
    read the developer's terminal. Raises LocalRunError when git hangs, cannot start or
    fails, ending with `advice`.
    """
    command = [git, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *arguments]
    try:
        done = subprocess.run(  # noqa: S603  # a fixed command, in the folder
            command,
            cwd=folder,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise LocalRunError(
            f"git took more than {GIT_TIMEOUT:g} seconds to read {_shown(folder)}; {advice}"
        ) from None
    except OSError as error:
        why = error.strerror or type(error).__name__
        raise LocalRunError(f"git could not be started ({why}); {advice}") from None
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        why = clean_text(said[0], 200, single_line=True) if said else "no reason given"
        raise LocalRunError(f"git cannot read the files in {_shown(folder)} ({why}); {advice}")
    return done.stdout


def _git() -> str:
    git = shutil.which("git")
    if git is None:
        raise LocalRunError(
            f"git is not installed, so Footfall cannot tell which files to copy; {_GIT_ADVICE}"
        )
    return git


def _git_folder_paths(git: str, folder: Path, *, advice: str = _GIT_ADVICE) -> list[Path]:
    """The repository's git folder and its common folder, as git names them, with `advice`
    in the error if git cannot.

    Each path is taken exactly as git prints it, but for the newline: git keeps a trailing
    space in a .git file's path, and a folder with that name is not the one without it.
    """
    paths = []
    for which in ("--absolute-git-dir", "--git-common-dir"):
        where = _run_git(git, folder, ["rev-parse", "--path-format=absolute", which], advice=advice)
        paths.append(Path(os.fsdecode(where.removesuffix(b"\n"))))
    return paths


def _superproject_git_folders(git: str, folder: Path, *, advice: str = _GIT_ADVICE) -> list[Path]:
    """When `folder` is in a submodule, the git folders of its superproject, and of that
    one's when it is a submodule too, out to the outermost; otherwise none. Raises
    LocalRunError when there are more than `_MOST_SUPERPROJECTS` of them, and with `advice`
    when git cannot say."""
    found: list[Path] = []
    current = folder
    # One question more than the limit allows answers, so that the last says whether the
    # outermost allowed is the outermost there is.
    for level in range(_MOST_SUPERPROJECTS + 1):
        asked = ["rev-parse", "--show-superproject-working-tree"]
        where = _run_git(git, current, asked, advice=advice)
        superproject = os.fsdecode(where.removesuffix(b"\n"))
        if not superproject:
            return found
        if level == _MOST_SUPERPROJECTS:
            break
        current = Path(superproject)
        found += _git_folder_paths(git, current, advice=advice)
    raise LocalRunError(
        f"{_shown(folder)} lies in submodules more than {_MOST_SUPERPROJECTS} levels deep"
    )


def _git_folders(
    git: str, folder: Path, paths: Sequence[Path] | None = None
) -> frozenset[_Identity]:
    """The identities of the repository's git folder and of its common folder, which for a
    linked worktree is another folder and holds the configuration, and of the configuration
    files in each, `config` and `config.worktree`. No name may lead into such a folder, and
    no file that is one of those files, by a hard link under any name, is copied. `paths`
    are the folders, as `_git_folder_paths` finds them, when the caller has them already.
    """
    identities: set[_Identity] = set()
    for path in _git_folder_paths(git, folder) if paths is None else paths:
        try:
            info = path.stat()
        except OSError as error:
            why = error.strerror or type(error).__name__
            raise LocalRunError(
                f"cannot find the git folder of {_shown(folder)} ({why}); {_GIT_ADVICE}"
            ) from None
        identities.add((info.st_dev, info.st_ino))
        for name in _GIT_CONFIGS:
            try:
                config = (path / name).lstat()
            except OSError:
                continue  # not there, which is usual for config.worktree
            if stat.S_ISREG(config.st_mode):
                identities.add((config.st_dev, config.st_ino))
    return frozenset(identities)


def _git_files(folder: Path, *, untracked: bool = False) -> _Listing:
    """The files git tracks in `folder`, and with `untracked` the untracked files it does not
    ignore too, and the identities of the repository's git folders, which no name may lead
    into.

    Which untracked files git ignores is decided by the `.gitignore` files in the folder,
    which the tested branch writes, and a negation there outranks the developer's own
    exclude files. So they are listed only when asked for.

    A sparse checkout is refused, as is any file git skips in the working tree: there, a
    checkout leaves a file of the developer's in place under a name the branch tracks, and
    that file would be copied as the branch's.

    No file to copy may be another name for a file in the git folders git names, in its
    superprojects' when it is a submodule, or in those found as git finds them, as
    `_discovered_git_folders` does. git names no superproject for a nested repository that
    is not a submodule, and discovery finds the repository around it. When discovery cannot
    follow a `.git`, a file with another name is refused, and no other.

    Raises LocalRunError when git is missing, hangs, or cannot read the folder.
    """
    git = _git()

    def listed(*which: str) -> set[PurePosixPath]:
        output = _run_git(git, folder, ["ls-files", "-z", *which])
        return {PurePosixPath(os.fsdecode(n)) for n in output.split(b"\0") if n}

    # Each name behind its status tag, "S " for a file git skips in the working tree.
    tagged = _run_git(git, folder, ["ls-files", "-z", "-t", "--cached"])
    entries = [entry for entry in tagged.split(b"\0") if entry]
    asked = ["config", "--bool", "--default", "false", "--get", "core.sparseCheckout"]
    sparse = _run_git(git, folder, asked).strip() == b"true"
    if sparse or any(entry.startswith(b"S ") for entry in entries):
        raise LocalRunError(
            f"{_shown(folder)} is a sparse checkout, or git skips some of its files in the "
            "working tree, so a file there may be one of yours under a name the branch "
            "tracks rather than the branch's own; test a full checkout, or pass --all-files"
        )
    tracked = {PurePosixPath(os.fsdecode(entry[2:])) for entry in entries}
    others = listed("--others", "--exclude-standard") - tracked if untracked else set()
    paths = _git_folder_paths(git, folder)
    named = [*paths, *_superproject_git_folders(git, folder)]
    try:
        linked = _Linked([*named, *_discovered_git_folders(folder)])
    except _Undiscoverable as problem:
        linked = _Linked(problem=str(problem))
    return _Listing(
        sorted(tracked | others), _git_folders(git, folder, paths), frozenset(others), linked
    )


def _worktree_top(folder: Path) -> Path:
    """The top of the git worktree `folder` lies in, all of which the tested branch decides,
    or `folder` itself when git names none, as for a folder outside git."""
    git = shutil.which("git")
    if git is None:
        return folder
    try:
        where = _run_git(git, folder, ["rev-parse", "--show-toplevel"])
    except LocalRunError:
        return folder
    top = os.fsdecode(where.removesuffix(b"\n"))
    return Path(top) if top else folder


def _known_git_folders(folder: Path) -> frozenset[_Identity]:
    """The git folders of the repository `folder` is in, or none when git cannot say."""
    try:
        return _git_folders(_git(), folder)
    except LocalRunError:
        return frozenset()


class _Undiscoverable(Exception):
    """A `.git` that Footfall will not follow, or a git folder it names that is not one."""


def _read_small(path: Path) -> bytes:
    """At most `_SMALL_FILE` bytes of a regular file, never through a link and never from a
    FIFO or a device, which could block or never end. Raises _Undiscoverable otherwise, for
    any error in opening or reading it, and for a NUL, which no path holds."""

    def unreadable(error: OSError) -> _Undiscoverable:
        return _Undiscoverable(f"Footfall cannot read {_shown(path)} ({error.strerror})")

    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise _Undiscoverable(f"{_shown(path)} is not a plain file")
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise unreadable(error) from None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise _Undiscoverable(f"{_shown(path)} is not a plain file")
        data = os.read(handle, _SMALL_FILE)
    except OSError as error:
        raise unreadable(error) from None
    finally:
        os.close(handle)
    if b"\0" in data:
        raise _Undiscoverable(f"{_shown(path)} holds a NUL")
    return data


def _first_line(data: bytes) -> str:
    return os.fsdecode(data.split(b"\n", 1)[0].removesuffix(b"\r"))


def _git_shaped(path: Path) -> bool:
    return _shaped_like_a_git_folder(lambda name: _kind(str(path / name)))


def _must_be_git_shaped(path: Path, named_by: Path | None = None) -> Path:
    """`path`, which `named_by` names, if it is shaped like a git folder; raises
    _Undiscoverable if not. A `.git` folder names itself, and is given no `named_by`."""
    if _git_shaped(path):
        return path
    if named_by is None:
        raise _Undiscoverable(f"{_shown(path)} is not shaped like a git folder")
    raise _Undiscoverable(
        f"{_shown(named_by)} names {_shown(path)}, which is not shaped like a git folder"
    )


def _discovered_git_folders(folder: Path) -> list[Path]:
    """The git folders that `folder` and each folder above it name, found as git finds them,
    without git: a `.git` folder, the folder a `.git` file's `gitdir:` line names, with the
    folder its `commondir` names, and a folder that is itself shaped like a git folder, as an
    implicit bare repository is. None at all means `folder` is in no repository; for a
    submodule, the superproject's are among them.

    Only plain files are read, never through a link, and at most `_SMALL_FILE` bytes of
    each, and each folder named must be shaped like a git folder. Unlike git, Footfall does
    not follow a `.git` that is a link, and does not stop at the first repository or at a
    file system's edge, so an odd `.git` above the repository counts against it: where it
    differs from git it can only refuse more. Raises _Undiscoverable for what it will not
    follow or cannot read.
    """
    found: list[Path] = []
    try:
        start = folder.resolve()
        for here in (start, *start.parents):
            entry = here / ".git"
            try:
                info = entry.lstat()
            except FileNotFoundError:
                if _git_shaped(here):
                    found.append(here)
                continue
            except OSError as error:
                raise _Undiscoverable(
                    f"Footfall cannot read {_shown(entry)} ({error.strerror})"
                ) from None
            if stat.S_ISDIR(info.st_mode):
                found.append(_must_be_git_shaped(entry))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _Undiscoverable(
                    f"{_shown(entry)} is a link or another kind of file, which Footfall does "
                    "not follow"
                )
            line = _first_line(_read_small(entry))
            if not line.startswith("gitdir: "):
                raise _Undiscoverable(
                    f"Footfall cannot tell where the git folder {_shown(entry)} names is"
                )
            gitdir = _must_be_git_shaped(here / line.removeprefix("gitdir: "), entry)
            found.append(gitdir)
            commondir = gitdir / "commondir"
            try:
                commondir.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _Undiscoverable(
                    f"Footfall cannot read {_shown(commondir)} ({error.strerror})"
                ) from None
            common = gitdir / _first_line(_read_small(commondir))
            found.append(_must_be_git_shaped(common, commondir))
    except ValueError as error:  # a path the operating system cannot take
        raise _Undiscoverable(f"a git folder's path cannot be used ({error})") from None
    return found


def _known_linked(folder: Path) -> _Linked:
    """The linked files of the git folders of the repository `folder` is in, and of its
    superproject's when it is in a submodule, for a copy of every file in the folder.

    Footfall looks in the folder and each folder above it for a `.git`, or for a folder that
    is itself shaped like a git folder, as git does. Finding neither is the one case that
    adds nothing: `folder` is in no repository. Otherwise git is asked too. When git cannot
    read the repository at all, as when another user owns it, which is what --all-files is
    for, or git is not installed, the folders Footfall found stand in; when git reads it but
    cannot say whether it is a submodule, the copy is refused. When Footfall cannot find
    them, a file with another name is refused, and no other.
    """
    try:
        discovered = _discovered_git_folders(folder)
    except _Undiscoverable as problem:
        return _Linked(problem=str(problem))  # refuses only a file with another name
    if not discovered:
        return _NO_LINKS
    try:
        git = _git()
        own = _git_folder_paths(git, folder)
    except LocalRunError:  # git is missing, or cannot read the repository
        return _Linked(discovered)
    superprojects = _superproject_git_folders(git, folder, advice=_SUPERPROJECT_ADVICE)
    return _Linked([*own, *superprojects, *discovered])


def _name_untracked(names: Iterable[PurePosixPath]) -> None:
    """Name each untracked file the copy takes on standard error, before it is copied, so
    that a person can see a `.env` arrive. Each name is quoted, made one line and cut short,
    without control or invisible characters and with any `##[` broken, since a name is the
    tested branch's to choose and could otherwise write to the terminal, forge a line, or
    give a GitHub Actions job a command."""
    shown = [no_legacy_commands(strip_invisible(_shown(name))) for name in sorted(names)]
    if not shown:
        return
    print(
        "footfall test: --untracked copies these files, which git neither tracks nor ignores, "
        "as the folder's own .gitignore decides:",
        file=sys.stderr,
    )
    for name in shown:
        print(f"  {name}", file=sys.stderr)


def _refuse_a_git_folder(base: Path, git_dirs: frozenset[_Identity]) -> None:
    """Refuse a folder to copy that is a git folder, or lies inside one."""
    for path in (base, *base.parents):
        info = path.stat()
        if (info.st_dev, info.st_ino) in git_dirs:
            raise LocalRunError(
                f"cannot copy {_shown(base)}: it is, or is inside, the repository's git folder"
            )


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or path.is_relative_to(folder)


def _shown(name: object) -> str:
    """A name from git's output or the disk, quoted and safe to print."""
    return quote(str(name), 120)


def _kind(name: str, folder: int | None = None) -> int | None:
    """The file type bits of `name`, in the open folder `folder` or as a path, never
    following a link; None for a name that is not there or cannot be read."""
    try:
        return stat.S_IFMT(os.stat(name, dir_fd=folder, follow_symlinks=False).st_mode)
    except OSError:
        return None


def _shaped_like_a_git_folder(at: Callable[[str], int | None]) -> bool:
    """Whether a folder looks like a git folder: a HEAD file and an objects folder, as a
    repository's own has, or a commondir file, as a linked worktree's has. Any of them may
    be a link, as HEAD is with `core.preferSymlinkRefs`. `at` gives the type of a name in
    the folder, never following a link."""
    file_or_link = (stat.S_IFREG, stat.S_IFLNK)
    has_objects = at("HEAD") in file_or_link and at("objects") in (stat.S_IFDIR, stat.S_IFLNK)
    return has_objects or at("commondir") in file_or_link


def _warn_left_out(where: object) -> None:
    LOG.warning(
        "left out %s: it is shaped like a git folder, with HEAD and objects or a commondir, "
        "and may hold a repository's configuration",
        _shown(where),
    )


class _InAGitFolder(Exception):
    """An untracked name lies in a folder shaped like a git folder, and is left out."""

    def __init__(self, where: str) -> None:
        super().__init__(where)
        self.where = where


def _open_folders(
    top: int,
    parts: Sequence[str],
    *,
    create: bool,
    name: PurePosixPath,
    git_dirs: frozenset[_Identity] = frozenset(),
    untracked: bool = False,
) -> int:
    """Open the folder `parts` below the open folder `top`, one name at a time and never
    through a link, making each that is missing when `create` is set.

    Raises FileNotFoundError for a missing folder when not creating, and LocalRunError when
    any part is a link or a file, cannot be opened, or is one of the git folders `git_dirs`.
    For an `untracked` name, raises _InAGitFolder when any part is shaped like a git folder.
    """
    current = os.dup(top)
    try:
        for index, part in enumerate(parts):
            where = "/".join(parts[: index + 1])
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=current)
            try:
                inner = os.open(part, _FOLDER_FLAGS, dir_fd=current)
            except FileNotFoundError:
                raise
            except OSError as error:
                if error.errno in _NOT_A_FOLDER:
                    side = "workspace" if create else "folder"
                    raise LocalRunError(
                        f"cannot copy {_shown(name)}: {_shown(where)} in the {side} is a link "
                        "or a file, not a folder, and Footfall copies nothing through a link"
                    ) from None
                why = error.strerror or type(error).__name__
                raise LocalRunError(f"cannot open {_shown(where)} ({why})") from None
            os.close(current)
            current = inner
            if git_dirs:
                info = os.fstat(current)
                if (info.st_dev, info.st_ino) in git_dirs:
                    raise LocalRunError(
                        f"cannot copy {_shown(name)}: {_shown(where)} is the repository's own "
                        "git folder"
                    )
            if untracked and _shaped_like_a_git_folder(functools.partial(_kind, folder=current)):
                raise _InAGitFolder(where)
    except BaseException:
        os.close(current)
        raise
    return current


def _refuse_a_git_config(
    name: object, info: os.stat_result, git_dirs: frozenset[_Identity], linked: _Linked
) -> None:
    if (info.st_dev, info.st_ino) in git_dirs or linked.holds(info):
        raise LocalRunError(
            f"cannot copy {_shown(name)}: it is the repository's git configuration, or another "
            "file of its git folder, linked under another name, and can hold a token"
        )


def _copy_file(
    source: int,
    target: int,
    leaf: str,
    name: PurePosixPath,
    git_dirs: frozenset[_Identity] = frozenset(),
    linked: _Linked = _NO_LINKS,
) -> None:
    """Copy one regular file between two open folders, refusing one that changes kind, and
    one that is a git configuration file in `git_dirs`, whatever it is called."""
    reader = os.open(leaf, _READ_FLAGS, dir_fd=source)
    try:
        info = os.fstat(reader)
        if not stat.S_ISREG(info.st_mode):
            raise LocalRunError(f"cannot copy {_shown(name)}: it changed while it was copied")
        _refuse_a_git_config(name, info, git_dirs, linked)
        mode = stat.S_IMODE(info.st_mode) & 0o777  # no set-id or sticky bits
        writer = os.open(leaf, _WRITE_FLAGS, mode, dir_fd=target)
        try:
            while chunk := os.read(reader, _CHUNK):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(writer, view) :]
            os.fchmod(writer, mode)
            os.utime(writer, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(writer)
    finally:
        os.close(reader)


def _copy_files(
    folder: Path,
    workspace: Path,
    names: Sequence[PurePosixPath],
    out: Path,
    *,
    git_dirs: frozenset[_Identity] = frozenset(),
    untracked: frozenset[PurePosixPath] = frozenset(),
    linked: _Linked = _NO_LINKS,
) -> None:
    """Copy the named files into a new workspace, links as links, leaving the results out.

    Every name is opened one folder at a time from an open folder, never through a link, on
    both sides, as the runner copies. A name below a link, which a crafted index or a folder
    swapped for a link can list, is refused. So is a name in a `.git` folder, however a
    crafted index spells it, and a name in any folder `git_dirs` identifies, whatever it is
    called, since a repository's configuration can hold a token. A folder to copy that is a
    git folder, or lies in one, is refused whole. So is a folder of its own, such as a
    submodule, whose files git does not list. An `untracked` name in a folder shaped like a
    git folder, such as a nested repository's git folder beside its worktree, is left out,
    with one warning for each such folder; a tracked one is copied, since a repository may
    commit a git folder as a fixture, and any clone would hold it. A file that is one of the
    git configuration files in `git_dirs`, by a hard link under another name, is refused. A
    name that is gone is skipped.
    """
    base = folder.resolve()
    results = out.resolve()
    left_out = results.relative_to(base).parts if _inside(results, base) else None
    _refuse_a_git_folder(base, git_dirs)
    workspace.mkdir()
    source_top = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        top = os.fstat(source_top)
        if (top.st_dev, top.st_ino) in git_dirs:
            raise LocalRunError(f"cannot copy {_shown(base)}: it is the repository's git folder")
        target_top = os.open(workspace, _FOLDER_FLAGS)
        warned: set[str] = set()
        try:
            for name in names:
                parts = name.parts
                if not parts or name.is_absolute() or any(p in (".", "..") for p in parts):
                    # git never adds such a name, but a crafted index can hold one, and it
                    # would lead out of the folder.
                    raise LocalRunError(
                        f"cannot copy {_shown(name)}: it is not a name inside the folder"
                    )
                if any(is_dot_git(part) for part in parts):
                    raise LocalRunError(
                        f"cannot copy {_shown(name)}: it is in the repository's git folder"
                    )
                if left_out is not None and parts[: len(left_out)] == left_out:
                    continue
                try:
                    _copy_name(
                        source_top,
                        target_top,
                        name,
                        git_dirs,
                        untracked=name in untracked,
                        linked=linked,
                    )
                except _InAGitFolder as shaped:
                    if shaped.where not in warned:
                        warned.add(shaped.where)
                        _warn_left_out(shaped.where)
        finally:
            os.close(target_top)
    finally:
        os.close(source_top)


def _copy_name(
    source_top: int,
    target_top: int,
    name: PurePosixPath,
    git_dirs: frozenset[_Identity] = frozenset(),
    *,
    untracked: bool = False,
    linked: _Linked = _NO_LINKS,
) -> None:
    """Copy one name between two open folders, as `_copy_files` describes."""
    parents, leaf = name.parts[:-1], name.parts[-1]
    try:
        source = _open_folders(
            source_top, parents, create=False, name=name, git_dirs=git_dirs, untracked=untracked
        )
    except FileNotFoundError:
        return  # tracked, but its folder was deleted since
    try:
        try:
            info = os.stat(leaf, dir_fd=source, follow_symlinks=False)
        except FileNotFoundError:
            return  # tracked, but deleted since
        if stat.S_ISDIR(info.st_mode):
            raise LocalRunError(
                f"{_shown(name)} is a folder git lists without its files, such as a "
                "submodule, another repository, or a tracked file now replaced by a folder; "
                f"{_GIT_ADVICE}"
            )
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise LocalRunError(f"cannot copy {_shown(name)}: it is not a file, a link or a folder")
        target = _open_folders(target_top, parents, create=True, name=name)
        try:
            if stat.S_ISLNK(info.st_mode):
                os.symlink(os.readlink(leaf, dir_fd=source), leaf, dir_fd=target)
            else:
                _copy_file(source, target, leaf, name, git_dirs, linked)
        finally:
            os.close(target)
    finally:
        os.close(source)


def _copy_folder(
    folder: Path,
    workspace: Path,
    out: Path,
    git_dirs: frozenset[_Identity] = frozenset(),
    linked: _Linked = _NO_LINKS,
) -> None:
    """Copy all of the folder into its workspace, links as links, leaving out the results,
    any `.git`, however it is spelt, any folder `git_dirs` identifies, whatever it is
    called, and any folder shaped like a git folder, with a warning for each, since a
    repository's configuration can hold a token. A file with another name that is a file
    of the repository's git folders, as `linked` finds them, is refused. A folder is left
    out by its name, by its shape, or because `git_dirs`, git's own answer when it can read
    the repository, names it. So a git folder git does not name, such as a nested
    repository's moved one, or any git folder when git cannot read the repository, is left
    out by its name or its shape alone. The hard-link check does not depend on git, since
    `linked` finds the git folders as git would when git cannot."""
    base = folder.resolve()
    _refuse_a_git_folder(base, git_dirs)
    if _shaped_like_a_git_folder(lambda name: _kind(str(base / name))):
        raise LocalRunError(f"cannot copy {_shown(base)}: it is shaped like a git folder")
    results = out.resolve()

    def left_out(path: Path, name: str) -> bool:
        if is_dot_git(name) or _inside(path.resolve(), results):
            return True
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            _refuse_a_git_config(path.relative_to(base), info, git_dirs, linked)
        if not stat.S_ISDIR(info.st_mode):
            return False
        if (info.st_dev, info.st_ino) in git_dirs:
            return True
        if _shaped_like_a_git_folder(lambda inner: _kind(str(path / inner))):
            _warn_left_out(path.relative_to(base))
            return True
        return False

    def ignore(where: str, names: list[str]) -> set[str]:
        return {name for name in names if left_out(Path(where) / name, name)}

    shutil.copytree(base, workspace, symlinks=True, ignore=ignore)


class _Run:
    """One run's state, so that each persona's part can be written once."""

    def __init__(
        self,
        *,
        run_id: str,
        manifest: Manifest,
        brief: Brief,
        devices: Mapping[str, Device],
        descriptors: Mapping[str, Any],
        focus: str | None,
        model_spec: str,
        out: Path,
        results: "_Results",
        budget: Budget,
        spend: RunSpend,
        proxy_url: str,
        account: tuple[str, str] | None,
    ) -> None:
        self.run_id = run_id
        self.spend = spend
        self.results = results
        self.manifest = manifest
        self.brief = brief
        self.devices = devices
        self.descriptors = descriptors
        self.focus = focus
        self.model_spec = model_spec
        self.out = out
        self.budget = budget
        self.proxy_url = proxy_url
        self.account = account
        self.password = account[1] if account else None
        self.redact = frozenset({self.password}) if self.password else frozenset[str]()

    def limit(self) -> float:
        """The most seconds one persona may take, from setting up its context to its report:
        the time limit, as far past it as the loop lets model calls run, and
        `BROWSER_ALLOWANCE`. With the default budget it is 3,060 s."""
        budget = self.budget
        return (
            budget.max_seconds
            + tester_loop.MIN_TURN
            + budget.grace_turns * tester_loop.TURN_FLOOR
            + BROWSER_ALLOWANCE
        )

    async def persona(self, persona: Persona, browser: Browser, address: str) -> PersonaReport:
        """Set up and run one persona, and write its report. Never raises but to be cancelled.

        The persona as a whole, set-up and sign-in included, must end within `limit()`
        seconds, or it is stopped and failed, so that a browser container that stops
        answering cannot hold it, and the run, open without end.
        """
        device = self.devices[persona.device]
        started = time.monotonic()
        folder = self.results.personas[persona.id]
        app_origin = f"http://{address}"
        context = None
        screenshots: Folder | None = None
        limit = self.limit()
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                context = await open_context(
                    browser, persona, device, self.descriptors, app_origin, self.proxy_url
                )
                guard = Guard(
                    app_origin,
                    frozenset(self.manifest.browser.assets),
                    redact=Redactor(self.redact),
                )
                await guard.install(context)
                page = await context.new_page()
                await page.goto(app_origin + "/", timeout=GOTO_TIMEOUT_MS)
                login = self.manifest.login
                if self.brief.start == "signed-in" and login is not None and self.account:
                    await sign_in(page, login, *self.account, app_origin)
                screenshots = folder.make("screenshots")
                executor = Executor(
                    page,
                    guard,
                    Trace(folder.path / "trace.jsonl", folder),
                    screenshots,
                    touch=bool(self.descriptors[device.playwright].get("has_touch")),
                    redact=self.redact,
                )
                report = await run_persona(
                    persona=persona,
                    device=device,
                    executor=executor,
                    model=model_from_spec(self.model_spec),
                    brief=self.brief,
                    focus=self.focus,
                    app_name=self.manifest.name,
                    budget=self.budget,
                    account=self.account,
                    spend=self.spend,
                )
        except SignInError as error:
            report = self._failed(persona, device, str(error), started)
        except TimeoutError:
            reason = "TimeoutError"
            if deadline.expired():
                reason = (
                    f"the persona ran past its hard limit of {limit:g} s, so Footfall stopped "
                    "it; its actions are not counted, and its cost leaves out the model call "
                    "it stopped"
                )
            report = self._failed(persona, device, reason, started)
        except Exception as error:  # noqa: BLE001  # any failure ends this persona, not the run
            # The class alone: a message may quote the page, or what was typed into it.
            report = self._failed(persona, device, type(error).__name__, started)
        finally:
            if context is not None:
                await _close(context, browser)
            if screenshots is not None:
                screenshots.close()
        try:
            _write_json(folder, "report.json", report.to_json(), self.password)
        except OSError as error:
            # The report is still returned, and goes in the summary and the table.
            path = folder.path / "report.json"
            LOG.error("could not write %s: %s", path, error.strerror or type(error).__name__)
        LOG.info("persona %s: %s", persona.id, report.status)
        return report

    def _failed(
        self, persona: Persona, device: Device, reason: str, started: float
    ) -> PersonaReport:
        """A failed persona's report, with what its model calls cost so far, which the run's
        spend holds even when the persona was stopped in the middle of its loop."""
        return _failed(persona, device, reason, started, self.spend.spent_by(persona.id))

    def summary(self, reports: Sequence[PersonaReport]) -> dict[str, Any]:
        return {
            "note": OBSERVATIONS,
            "run_id": self.run_id,
            "app": self.manifest.name,
            "brief": self.brief.name,
            "model": self.model_spec,
            "personas": [
                {
                    "persona": r.persona,
                    "device": r.device,
                    "status": r.status,
                    "reason": r.reason,
                    "findings": len(r.findings),
                    "actions": r.actions,
                    "cost_usd": r.cost_usd,
                    "seconds": r.seconds,
                }
                for r in reports
            ],
            "cost_usd": sum(r.cost_usd for r in reports),
        }


async def _close(context: BrowserContext, browser: Browser) -> None:
    """Close a persona's context within `CLOSE_TIMEOUT`.

    A context that does not close in time means the browser container has stopped
    answering, so the connection to it is closed, which happens on Footfall's side and ends
    every call still waiting on it, those of the other personas on that browser included:
    a container that does not answer one persona will not answer the others.
    """
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT):
            await context.close()
    except PlaywrightError:
        return
    except TimeoutError:
        LOG.warning(
            "a context did not close within %g s, so the connection to the run's browser "
            "was closed",
            CLOSE_TIMEOUT,
        )
        with contextlib.suppress(PlaywrightError, TimeoutError):
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await browser.close()


def _clean_tail(text: str, password: str | None) -> str:
    """A log tail, safe to print: no terminal control characters and the password out.

    It is cleaned, then stripped of characters that show nothing, then redacted, as the
    executor does, so that cleaning can never join a password split by such a character.
    It is cut to its last `LOG_TAIL_CAP` characters only then, so no cut leaves part of one.
    """
    text = clean_text(text, sys.maxsize)
    if password:
        text = strip_invisible(text).replace(password, REDACTED)
    return text[-LOG_TAIL_CAP:]


async def _ready(runner: Runner, run: _Run, copy: str, address: str) -> None:
    app = run.manifest.app
    try:
        await wait_ready(address, app.ready, app.ready_timeout)
    except TimeoutError:
        try:
            tail = await runner.logs(run.run_id, copy)
        except Exception:  # noqa: BLE001  # the timeout is the news; the logs are a bonus
            LOG.warning("could not read the logs of the copy for %s", copy)
            tail = ""
        raise NotReadyError(
            f"the copy of the app for {copy} did not answer {app.ready} within "
            f"{app.ready_timeout} seconds",
            _clean_tail(tail, run.password),
        ) from None


_MOST_LEVELS: Final = 4096  # folders above one that are believed, before giving up


def _lies_in(fd: int, top: _Identity) -> bool:
    """Whether the open folder `fd` is the folder `top` identifies or lies inside it, as the
    file system has it, whatever names led there. Each folder above is opened only to search
    it. Raises OutputError when one cannot be opened."""
    current = os.dup(fd)
    try:
        for _ in range(_MOST_LEVELS):
            info = os.fstat(current)
            if (info.st_dev, info.st_ino) == top:
                return True
            try:
                parent = os.open("..", _SEARCH | os.O_DIRECTORY, dir_fd=current)
            except OSError as error:
                why = error.strerror or type(error).__name__
                raise OutputError(
                    f"cannot tell whether the results would lie inside the tested checkout: a "
                    f"folder above them cannot be opened ({why})"
                ) from None
            os.close(current)
            current = parent
            above = os.fstat(current)
            if (above.st_dev, above.st_ino) == (info.st_dev, info.st_ino):
                return False  # the root, which is its own parent
        return True  # deeper than any real folder: treated as inside, where nothing is followed
    finally:
        os.close(current)


def _open_results(out: Path, tested: Path) -> Folder:
    """Open the results folder `out`, one part at a time from the root, or from the working
    folder when `out` is relative, making each part that is missing, mode 0o700.

    `tested` is the checkout under test, all of which is the tested branch's, so in any
    folder that lies inside it a part that is a link is refused with OutputError, whatever
    path led there. Anywhere else a link is the operator's own, as /var and /tmp are on a
    Mac, or a home folder can be, and is followed. `out` itself is never followed when it is
    a link, wherever it is.
    """
    try:
        info = tested.stat()
    except OSError as error:
        raise OutputError(f"cannot read {_shown(tested)} ({error.strerror})") from None
    top = (info.st_dev, info.st_ino)
    first = 1 if out.is_absolute() else 0
    parts = out.parts[first:]
    # Every folder above the results is opened only to go through it, which needs search
    # permission alone; the results folder itself is opened to be read.
    current = os.open(out.anchor or ".", _SEARCH | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts, start=1):
            where = Path(*out.parts[: first + index])
            if part != "..":
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, FOLDER_MODE, dir_fd=current)
            above_out = index < len(parts)
            follow = above_out and (part == ".." or not _lies_in(current, top))
            access = _SEARCH if above_out else os.O_RDONLY
            flags = access | os.O_DIRECTORY | (0 if follow else os.O_NOFOLLOW)
            try:
                inner = os.open(part, flags, dir_fd=current)
            except OSError as error:
                if error.errno in _NOT_A_FOLDER:
                    raise OutputError(
                        f"{_shown(where)} is a link or a file, and Footfall writes no results "
                        "through a link; choose another results folder with --out"
                    ) from None
                why = error.strerror or type(error).__name__
                raise OutputError(f"cannot open {_shown(where)} ({why})") from None
            os.close(current)
            current = inner
    except BaseException:
        os.close(current)
        raise
    return Folder(current, out)


@dataclass
class _Results:
    """The results folder and each persona's folder in it, all held open, so that nothing is
    written by path once they are made."""

    out: Folder
    personas: dict[str, Folder]

    def tidy(self) -> None:
        """Remove the persona folders a failed run left empty, so that they block no later
        run."""
        for name in self.personas:
            with contextlib.suppress(OSError):
                self.out.remove(name)  # only an empty folder

    def close(self) -> None:
        for folder in self.personas.values():
            folder.close()
        self.out.close()


def _prepare_results(out: Path, personas: Sequence[Persona], tested: Path) -> _Results:
    """Open the results folder, making it if it is missing, make a folder in it for each
    persona, refusing to overwrite what an earlier run left there, and return them all, open.

    The results folder may lie inside `tested`, the checkout under test, whose branch
    decides what is there. `_open_results` says which links on the way there it refuses;
    in the results folder, OutputError refuses a link, even one to nothing, where
    summary.json or a persona's folder would go. From then on every result is written by
    name in a folder held open, never by path, so a link put in place of one later moves
    nothing.
    """
    folder = _open_results(out, tested)
    made: dict[str, Folder] = {}
    try:
        names = [*(p.id for p in personas), "summary.json"]
        for name in names:
            if folder.kind(name) == stat.S_IFLNK:
                raise OutputError(
                    f"{out / name} is a link, and Footfall writes no results through one; "
                    "choose another results folder with --out"
                )
        taken = [name for name in names if folder.kind(name) is not None]
        if taken:
            raise OutputError(
                f"{out / taken[0]} already exists; choose another results folder with --out, "
                "or move the old results away"
            )
        for persona in personas:
            made[persona.id] = folder.make(persona.id)
    except BaseException:
        _Results(folder, made).close()
        raise
    return _Results(folder, made)


async def test_folder(
    folder: Path,
    *,
    persona_ids: Sequence[str],
    brief_name: str,
    focus: str | None,
    model_spec: str,
    out: Path,
    runner: Runner,
    budget: Budget,
    cost_ceiling_usd: float = RUN_COST_CEILING_USD,
    connect_browser: ConnectBrowser | None = None,
    workspaces: Path = DEFAULT_WORKSPACES,
    proxy_container: str = DEFAULT_PROXY_CONTAINER,
    stdout: TextIO | None = None,
    all_files: bool = False,
    untracked: bool = False,
) -> list[PersonaReport]:
    """Test a local folder with the named personas (the manifest's, when none are named).

    Only the files git tracks are copied. With `untracked`, so are the untracked files git
    does not ignore, each named on standard error first. With `all_files`, which a folder
    outside git needs, every file is copied; the two cannot be asked for together, and
    ValueError says so.

    `budget` is each persona's; `cost_ceiling_usd` is the most the whole run may spend on
    model calls, after which no persona starts another turn.

    `connect_browser` takes an engine and the run's Playwright endpoint and returns a
    browser; by default it connects to the run's browser container. Writes the results into
    `out` and prints a table of them. The runner is asked to stop the run however this ends.
    Raises ManifestError, PersonaError, ModelSpecError, OutputError, NotReadyError,
    RunnerError, BrowserConnectError, or MaskError when a secret could not be masked in a
    GitHub Actions log, when the run cannot go ahead.
    """
    if all_files and untracked:
        raise ValueError("untracked and all_files cannot both be asked for")
    spend = RunSpend(cost_ceiling_usd)
    manifest = load_manifest(folder)
    brief = load_brief(folder, manifest, brief_name)
    devices = load_devices()
    personas = select(load_personas(devices), list(persona_ids) or list(manifest.test.personas))
    model_from_spec(model_spec)  # a spec that names nothing runnable stops the run here
    listing = None
    if not all_files:
        listing = await asyncio.to_thread(
            functools.partial(_git_files, folder, untracked=untracked)
        )
        # Both were read from the folder, never through a link; neither is used unless the
        # run would copy it too.
        require_listed(manifest, brief, listing.names)
        _name_untracked(listing.untracked)

    run_id = new_run_id()
    account = None
    if manifest.login is not None:
        account = ("tester-" + secrets.token_urlsafe(6), secrets.token_urlsafe(24))
        mask(account[1])  # in a GitHub Actions job, before anything can print it
    workspace = workspaces / run_id
    stream = stdout or sys.stdout
    async with contextlib.AsyncExitStack() as stack:
        playwright = await stack.enter_async_context(async_playwright())
        connect = connect_browser or functools.partial(connect_run_server, playwright)
        missing = [d.playwright for d in devices.values() if d.playwright not in playwright.devices]
        if missing:
            raise PersonaError(f"Playwright has no device called {missing[0]!r}")
        # The branch decides its whole checkout, not only the folder tested in it.
        checkout = await asyncio.to_thread(_worktree_top, folder)
        results = _prepare_results(out, personas, checkout)
        stack.callback(results.close)
        run = _Run(
            run_id=run_id,
            manifest=manifest,
            brief=brief,
            devices=devices,
            descriptors=playwright.devices,
            focus=focus,
            model_spec=model_spec,
            out=out,
            results=results,
            budget=budget,
            spend=spend,
            proxy_url=f"http://{proxy_container}:{PROXY_PORT}",
            account=account,
        )
        try:
            reports = await _go(run, runner, connect, personas, folder, workspace, listing)
            _write_json(results.out, "summary.json", run.summary(reports), run.password)
            print(format_table(reports), file=stream)
        except BaseException as error:
            if isinstance(error, LocalRunError | RunnerError):
                # The runner redacts its logs and messages, but by plain replacement, which a
                # password cut by a character that shows nothing gets past; and its message
                # can quote a path the seed named.
                error.log_tail = _clean_tail(error.log_tail, run.password)
                error.args = (_clean_tail(str(error), run.password),)
            await _finish(runner, run_id, workspace, quiet=True)
            results.tidy()
            raise
        await _finish(runner, run_id, workspace, quiet=False)
    return reports


test_folder.__test__ = False  # type: ignore[attr-defined]  # not a pytest test, despite its name


async def _go(
    run: _Run,
    runner: Runner,
    connect: ConnectBrowser,
    personas: Sequence[Persona],
    folder: Path,
    workspace: Path,
    listing: _Listing | None,
) -> list[PersonaReport]:
    """Prepare the app, start its copies and the browsers, and run every persona.

    `listing` is the files git lists to copy, or None to copy every file in the folder.
    """
    manifest = run.manifest
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if listing is None:
        git_dirs = await asyncio.to_thread(_known_git_folders, folder)
        linked = await asyncio.to_thread(_known_linked, folder)
        await asyncio.to_thread(_copy_folder, folder, workspace, run.out, git_dirs, linked)
    else:
        await asyncio.to_thread(
            functools.partial(
                _copy_files,
                folder,
                workspace,
                listing.names,
                run.out,
                git_dirs=listing.git_dirs,
                untracked=listing.untracked,
                linked=listing.linked,
            )
        )
    env: dict[str, str] = {}
    if run.account is not None:
        env = {"FOOTFALL_USERNAME": run.account[0], "FOOTFALL_PASSWORD": run.account[1]}
    app = manifest.app
    LOG.info("preparing run %s", run.run_id)
    runtime = await runner.prepare(
        run.run_id, workspace, app.runtime, app.install, app.seed, app.egress, env
    )
    # The runner knows whether its containers start without gVisor, whatever this process's
    # own setting says; the command line has already warned when that says runc too.
    if runtime == "runc" and os.environ.get("FOOTFALL_RUNTIME") != "runc":
        print(RUNC_WARNING, file=sys.stderr)
    addresses: dict[str, str] = {}
    for persona in personas:
        addresses[persona.id] = await runner.start_app(
            run.run_id, persona.id, app.start, app.port, app.egress
        )
    # The first copy that is not ready ends the wait for the others.
    waits = [asyncio.create_task(_ready(runner, run, p, a)) for p, a in addresses.items()]
    try:
        await asyncio.gather(*waits)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)

    endpoint = await runner.start_browsers(run.run_id, manifest.browser.assets)
    engines: list[Engine] = sorted({run.devices[p.device].engine for p in personas})
    browsers: dict[str, Browser] = {}
    try:
        for engine in engines:
            browsers[engine] = await connect(engine, endpoint)
        LOG.info("running %d personas", len(personas))
        # Every persona ends before anything is raised, so that none is still running when
        # its browser closes and the run stops.
        ended = await asyncio.gather(
            *(
                run.persona(p, browsers[run.devices[p.device].engine], addresses[p.id])
                for p in personas
            ),
            return_exceptions=True,
        )
        reports: list[PersonaReport] = []
        for outcome in ended:
            if isinstance(outcome, BaseException):
                raise outcome
            reports.append(outcome)
        return reports
    finally:
        for browser in browsers.values():
            with contextlib.suppress(PlaywrightError):
                await browser.close()


async def _finish(runner: Runner, run_id: str, workspace: Path, *, quiet: bool) -> None:
    """Stop the run and delete its workspace. A stop that fails is raised, unless `quiet`,
    when the run is ending on another error, which is then the one raised."""
    try:
        await runner.stop(run_id)
    except Exception:
        if not quiet:
            raise
        LOG.exception("could not stop run %s", run_id)
    finally:
        await asyncio.to_thread(shutil.rmtree, workspace, ignore_errors=True)
