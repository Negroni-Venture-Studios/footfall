"""Footfall's command line.

    footfall test <folder> [--personas ids] [--brief name] [--focus text] [--model spec]
                           [--out dir] [--cost-ceiling dollars] [--untracked | --all-files]
    footfall runner
    footfall proxy

`footfall test` runs the testers against a local folder and writes their results. It copies
only the files git tracks, unless `--untracked` adds the untracked files git does not ignore
or `--all-files` copies every file. `footfall runner` serves the runner, the one process that
holds the Docker socket, on its Unix socket. `footfall proxy` serves the egress proxy and its
control socket. The development compose file, `deploy/dev/compose.yaml`, runs the three
together.

Settings come from the environment:

| Variable | Default |
|---|---|
| `FOOTFALL_RUNNER_SOCKET` | `/run/footfall/runner.sock` |
| `FOOTFALL_PROXY_CONTROL` | `/run/footfall-proxy/proxy.sock` |
| `FOOTFALL_WORKSPACES` | `/var/lib/footfall/workspaces` |
| `FOOTFALL_RUNS` | `/var/lib/footfall-runs` |
| `FOOTFALL_RUNTIME` | `runsc` |
| `FOOTFALL_PROXY_CONTAINER` | `footfall-proxy` |
| `FOOTFALL_ORCHESTRATOR_CONTAINER` | `footfall-orchestrator` |
| `FOOTFALL_DISK_BUDGET_MIB` | `8192` |
| `FOOTFALL_RUN_LIFETIME_MINUTES` | `90` |
| `FOOTFALL_RUN_COST_CEILING_USD` | `12` |

`FOOTFALL_RUNTIME=runc` starts containers without gVisor, for development on a machine that
lacks it, and every command then prints a warning. `FOOTFALL_DISK_BUDGET_MIB` is the most
disk, in MiB, that the runner lets one run use, counting every copy of its app.
`FOOTFALL_RUN_LIFETIME_MINUTES` is the longest a run may last before the runner stops it,
whether or not its caller is still there. `FOOTFALL_RUN_COST_CEILING_USD`, which
`--cost-ceiling` overrides, is the most a `footfall test` run may spend on model calls, in
dollars from 0.01 to 1000: once its personas together reach it, none starts another turn. The
tested repository cannot raise it. `FOOTFALL_RUNS` must belong to root and be writable
by root alone, as must every folder above it, or the runner will not start. The runner sees
the folders above it only as its own container shows them, so on the host, where Docker finds
the folder, each folder above it must be root's alone too.
"""

import argparse
import asyncio
import logging
import os
import re
import signal
import sys
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO

from footfall.actions import MaskError, no_legacy_commands
from footfall.browsers import BrowserConnectError
from footfall.local import PROXY_PORT, RUNC_WARNING, LocalRunError, test_folder
from footfall.manifest import ManifestError
from footfall.personas import PersonaError
from footfall.proxy.control import ProxyControlClient, serve_control
from footfall.proxy.policy import Policy
from footfall.proxy.server import ProxyServer
from footfall.runner.client import RunnerClient
from footfall.runner.engine import DockerEngine
from footfall.runner.server import serve
from footfall.runner.service import RunnerError, RunnerService, RunnerSetupError
from footfall.tester.loop import Budget
from footfall.tester.model import DEFAULT_MODEL, ModelSpecError
from footfall.tester.report import clean_text, strip_invisible

__all__ = ["ALL_FILES_WARNING", "RUNC_WARNING", "Settings", "main", "run_proxy", "run_runner"]

ALL_FILES_WARNING: Final = (
    "footfall test: warning: --all-files copies every file in the folder into the app's "
    "containers, including files git would ignore, such as a .env with real keys in it."
)
RUNTIMES: Final = ("runsc", "runc")
# The images the runner starts, as `docker build` tags them for development.
RUNTIME_IMAGES: Final = {"python3.13": "footfall-python3.13:dev", "node26": "footfall-node26:dev"}
BROWSERS_IMAGE: Final = "footfall-browsers:dev"
DEFAULT_OUT: Final = Path("footfall-results")
_CONTAINER_NAME: Final = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
_MIB: Final = 1024**2
_MOST_MIB: Final = 16 * 1024**2  # 16 TiB, far beyond any disk a run could have
_MOST_MINUTES: Final = 7 * 24 * 60  # a week, far beyond any run's own limits
_MOST_DOLLARS: Final = 1000  # far beyond the default personas' worst case, $12
_DOLLARS: Final = re.compile(r"[0-9]{1,4}(\.[0-9]{1,2})?")
_LOG_TAIL_CAP: Final = 20_000
# What each line of the app's output is printed behind: not white space, which the GitHub
# Actions runner trims before it looks for a command, and not a character it can skip.
_QUOTED: Final = "| "
_LINE_CAP: Final = 1_000  # the most characters printed of an error message, or of a problem
_PROBLEMS_CAP: Final = 20_000  # the most characters printed of a manifest's problems
_STOPPED: Final = 130  # the exit code when a command is stopped by a signal

_DEFAULTS: Final = {
    "FOOTFALL_RUNNER_SOCKET": "/run/footfall/runner.sock",
    # A folder of its own, which only the proxy and the runner mount.
    "FOOTFALL_PROXY_CONTROL": "/run/footfall-proxy/proxy.sock",
    "FOOTFALL_WORKSPACES": "/var/lib/footfall/workspaces",
    "FOOTFALL_RUNS": "/var/lib/footfall-runs",
    "FOOTFALL_RUNTIME": "runsc",
    "FOOTFALL_PROXY_CONTAINER": "footfall-proxy",
    "FOOTFALL_ORCHESTRATOR_CONTAINER": "footfall-orchestrator",
    "FOOTFALL_DISK_BUDGET_MIB": "8192",
    # A persona stops by its own limits within about an hour of the run's start, install and
    # seed included (docs/design.md, Limits and spend), so this is margin, not a limit on work.
    "FOOTFALL_RUN_LIFETIME_MINUTES": "90",
    # The design's worst case for a run of the default personas: twelve at $1 each.
    "FOOTFALL_RUN_COST_CEILING_USD": "12",
}


class SettingsError(ValueError):
    """A setting in the environment that Footfall cannot use."""


def dollars(text: str, name: str) -> float:
    """A cost ceiling in dollars, from 0.01 to `_MOST_DOLLARS`, with at most two decimal
    places. Raises SettingsError, naming `name`, for anything else."""
    if not _DOLLARS.fullmatch(text) or not 0 < float(text) <= _MOST_DOLLARS:
        raise SettingsError(
            f"{name} must be a number of dollars from 0.01 to {_MOST_DOLLARS}, such as 12 or "
            f"7.50, not {text!r}"
        )
    return float(text)


@dataclass(frozen=True)
class Settings:
    """Footfall's settings, from the environment."""

    runner_socket: Path
    proxy_control: Path
    workspaces: Path
    runs: Path
    runtime: str
    proxy_container: str
    orchestrator_container: str
    disk_budget: int  # bytes
    run_lifetime: int  # seconds
    run_cost_ceiling: float  # dollars, for every model call of a footfall test run

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        def get(name: str) -> str:
            return env.get(name) or _DEFAULTS[name]

        def path(name: str) -> Path:
            value = Path(get(name))
            if not value.is_absolute():
                raise SettingsError(f"{name} must be an absolute path, not {str(value)!r}")
            return value

        def container(name: str) -> str:
            value = get(name)
            if not _CONTAINER_NAME.fullmatch(value):
                raise SettingsError(
                    f"{name} must be a container name of lower-case letters, digits, dots "
                    f"and hyphens, not {value!r}"
                )
            return value

        def whole(name: str, unit: str, most: int) -> int:
            # Set but empty is an error too: a limit is never the default in silence.
            value = env[name] if name in env else _DEFAULTS[name]
            if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= most:
                raise SettingsError(
                    f"{name} must be a whole number of {unit} from 1 to {most}, not {value!r}"
                )
            return int(value)

        runtime = get("FOOTFALL_RUNTIME")
        if runtime not in RUNTIMES:
            raise SettingsError(f"FOOTFALL_RUNTIME must be runsc or runc, not {runtime!r}")
        return cls(
            runner_socket=path("FOOTFALL_RUNNER_SOCKET"),
            proxy_control=path("FOOTFALL_PROXY_CONTROL"),
            workspaces=path("FOOTFALL_WORKSPACES"),
            runs=path("FOOTFALL_RUNS"),
            runtime=runtime,
            proxy_container=container("FOOTFALL_PROXY_CONTAINER"),
            orchestrator_container=container("FOOTFALL_ORCHESTRATOR_CONTAINER"),
            disk_budget=whole("FOOTFALL_DISK_BUDGET_MIB", "MiB", _MOST_MIB) * _MIB,
            run_lifetime=whole("FOOTFALL_RUN_LIFETIME_MINUTES", "minutes", _MOST_MINUTES) * 60,
            # Set but empty is an error too, as for the limits above.
            run_cost_ceiling=dollars(
                env.get(
                    "FOOTFALL_RUN_COST_CEILING_USD", _DEFAULTS["FOOTFALL_RUN_COST_CEILING_USD"]
                ),
                "FOOTFALL_RUN_COST_CEILING_USD",
            ),
        )


# ---------------------------------------------------------------- the services


async def run_runner(settings: Settings) -> None:
    """Remove what an earlier runner left, then serve the runner on its socket until stopped.

    The socket's mode is 0660 and its group is the runner's own, so a runner started as
    "0:10001" can be reached by the orchestrator's user, 10001, and by nobody else.
    """
    service = RunnerService(
        DockerEngine(),
        ProxyControlClient(settings.proxy_control),
        workspaces=settings.workspaces,
        runs=settings.runs,
        images=RUNTIME_IMAGES,
        browsers_image=BROWSERS_IMAGE,
        proxy_container=settings.proxy_container,
        orchestrator_container=settings.orchestrator_container,
        proxy_port=PROXY_PORT,
        runtime=settings.runtime,
        disk_budget=settings.disk_budget,
        lifetime=settings.run_lifetime,
    )
    await asyncio.to_thread(service.sweep)
    group = os.getegid()
    logging.getLogger(__name__).info(
        "the runner is serving %s for group %d", settings.runner_socket, group
    )
    await serve(service, settings.runner_socket, group=group)


async def run_proxy(settings: Settings, *, port: int = PROXY_PORT, allowed_uid: int = 0) -> None:
    """Serve the egress proxy's control socket until stopped.

    The proxy listens on no interface of its own: the runner tells it, over the control
    socket, its address on each run's network, and it listens there on `port` for that run.
    The control socket answers only `allowed_uid`, root, which is the runner's user.
    """
    proxy = ProxyServer(Policy(), port=port)
    try:
        await serve_control(proxy, settings.proxy_control, allowed_uid=allowed_uid)
    finally:
        await proxy.close()


def _until_stopped[T](work: Coroutine[Any, Any, T]) -> T:
    """Run `work`, cancelling it on SIGTERM or SIGHUP as asyncio already does on Ctrl-C, so
    that its cleanup, such as stopping a run, still happens when it is stopped or its terminal
    goes."""

    async def guarded() -> T:
        task = asyncio.current_task()
        assert task is not None  # noqa: S101  # always inside asyncio.run
        loop = asyncio.get_running_loop()
        for stop in (signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(stop, task.cancel)
        return await work

    return asyncio.run(guarded())


# ---------------------------------------------------------------- footfall test


def _ids(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _inert_tail(text: str) -> str:
    """The app's last output, cleaned, made inert for a GitHub Actions job's log.

    `footfall test` can run in a job, where the runner reads a step's output for workflow
    commands: a line that starts with `::` once leading white space is trimmed, and `##[`
    anywhere in a line, in a comparison that can skip characters that show nothing. The
    output is written by the code under test, so each line is put behind a bar and every
    `##[` is broken, and only Footfall's own writes, such as its masks, are read as commands.

    This comes after the tail is cleaned and its invisible characters are stripped, since
    those steps remove characters, a zero-width space or a carriage return, say, that could
    otherwise stand in front of a `::` and move it to the start of a line.
    """
    return "\n".join(_QUOTED + no_legacy_commands(line) for line in text.splitlines())


def _printable(error: Exception) -> str:
    """An error's message, made safe to print on standard error.

    A message can quote what the tested repository wrote, such as a key in its manifest, or
    a name from its folder. So it is cleaned of control and invisible characters, made one
    line, cut short and has every `##[` broken, so that it can neither write to the terminal
    nor forge a line of Footfall's own output or a command for a GitHub Actions job. A
    ManifestError keeps one line for each problem, each cleaned the same way.
    """
    if isinstance(error, ManifestError):
        problems = [clean_text(p, _LINE_CAP, single_line=True) for p in error.problems]
        text = clean_text(str(ManifestError(problems)), _PROBLEMS_CAP)
    else:
        text = clean_text(str(error), _LINE_CAP, single_line=True)
    return no_legacy_commands(strip_invisible(text))


def _test(args: argparse.Namespace, settings: Settings) -> int:
    def say(message: str) -> None:
        print(f"footfall test: {message}", file=sys.stderr)

    if not args.model.startswith("fake:") and not os.environ.get("ANTHROPIC_API_KEY"):
        say("set ANTHROPIC_API_KEY, or try Footfall without one with --model fake:<script>")
        return 2
    if args.all_files:
        print(ALL_FILES_WARNING, file=sys.stderr)
    try:
        reports = _until_stopped(
            test_folder(
                Path(args.folder),
                persona_ids=args.personas,
                brief_name=args.brief,
                focus=args.focus,
                model_spec=args.model,
                out=args.out,
                runner=RunnerClient(settings.runner_socket),
                budget=Budget(),
                cost_ceiling_usd=(
                    settings.run_cost_ceiling if args.cost_ceiling is None else args.cost_ceiling
                ),
                workspaces=settings.workspaces,
                proxy_container=settings.proxy_container,
                all_files=args.all_files,
                untracked=args.untracked,
            )
        )
    except (RunnerError, LocalRunError) as error:
        say(_printable(error))
        if error.log_tail:
            tail = _inert_tail(strip_invisible(clean_text(error.log_tail, _LOG_TAIL_CAP)))
            print(f"The app's last output:\n{tail}", file=sys.stderr)
        return 1
    except (
        ManifestError,
        PersonaError,
        ModelSpecError,
        BrowserConnectError,
        MaskError,
        OSError,
    ) as error:
        say(_printable(error))
        return 1
    print(f"The results are in {args.out}.")
    return 1 if any(report.status == "failed" for report in reports) else 0


# ---------------------------------------------------------------- the command line


class _InertFormatter(logging.Formatter):
    """Formats a log line with the characters that show nothing stripped and every `##[`
    broken. A line can name a file or a folder of the tested branch's choosing, and in a
    GitHub Actions job the runner reads `##[` anywhere in a line of a step's output as a
    command, so a log line is made as inert as the app's own output."""

    def format(self, record: logging.LogRecord) -> str:
        return no_legacy_commands(strip_invisible(super().format(record)))


def _stderr_handler(stream: TextIO) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_InertFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    return handler


def _cost_ceiling(text: str) -> float:
    try:
        return dollars(text, "--cost-ceiling")
    except SettingsError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="footfall", description="Test web apps the way strangers would use them."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser(
        "test",
        help="test a local folder with AI personas",
        description=(
            "Test the app in a local folder. Copies only the files git tracks, unless "
            "--untracked or --all-files says otherwise, and refuses a sparse checkout "
            "without --all-files. Exits with 0 when every persona ran, "
            "1 when one failed or the run could not go ahead, and 2 for a setting that is "
            "wrong."
        ),
    )
    test.add_argument("folder", help="the folder to test, with footfall.toml at its root")
    test.add_argument(
        "--personas",
        type=_ids,
        default=[],
        help="persona ids, separated by commas (default: the manifest's)",
    )
    test.add_argument("--brief", default="default", help="the brief to use (default: default)")
    test.add_argument("--focus", default=None, help="what the testers should pay attention to")
    test.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"the model, or fake:<script> for a scripted one (default: {DEFAULT_MODEL})",
    )
    test.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"the folder for the results (default: {DEFAULT_OUT})",
    )
    test.add_argument(
        "--cost-ceiling",
        type=_cost_ceiling,
        default=None,
        metavar="DOLLARS",
        help=(
            "the most the run may spend on model calls, in dollars; once its personas reach "
            "it, none starts another turn (default: FOOTFALL_RUN_COST_CEILING_USD, or 12)"
        ),
    )
    # --all-files already copies every untracked file, so the two together are refused.
    copied = test.add_mutually_exclusive_group()
    copied.add_argument(
        "--untracked",
        action="store_true",
        help=(
            "copy the untracked files git does not ignore as well, each named on standard "
            "error first; the folder's own .gitignore decides which they are"
        ),
    )
    copied.add_argument(
        "--all-files",
        action="store_true",
        help=(
            "copy every file in the folder, ignored ones included; needed outside git, which "
            "otherwise decides what is copied"
        ),
    )
    commands.add_parser("runner", help="serve the runner, which alone holds the Docker socket")
    commands.add_parser("proxy", help="serve the egress proxy")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code."""
    if os.environ.get("FOOTFALL_RUNTIME") == "runc":
        print(RUNC_WARNING, file=sys.stderr)
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
    except SettingsError as error:
        print(f"footfall: {error}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, handlers=[_stderr_handler(sys.stderr)])
    try:
        if args.command == "test":
            return _test(args, settings)
        _until_stopped(run_runner(settings) if args.command == "runner" else run_proxy(settings))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return _STOPPED
    except RunnerSetupError as error:
        print(f"footfall runner: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
