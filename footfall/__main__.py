"""Footfall's command line.

    footfall test <folder> [--personas ids] [--brief name] [--focus text] [--model spec]
                           [--out dir]
    footfall runner
    footfall proxy

`footfall test` runs the testers against a local folder and writes their results. `footfall
runner` serves the runner, the one process that holds the Docker socket, on its Unix socket.
`footfall proxy` serves the egress proxy and its control socket. The development compose
file, `deploy/dev/compose.yaml`, runs the three together.

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

`FOOTFALL_RUNTIME=runc` starts containers without gVisor, for development on a machine that
lacks it, and every command then prints a warning. `FOOTFALL_DISK_BUDGET_MIB` is the most
disk, in MiB, that the runner lets one run use, counting every copy of its app.
`FOOTFALL_RUNS` must belong to root and be writable by root alone, as must every folder above
it, or the runner will not start.
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
from typing import Any, Final

from footfall.actions import MaskError
from footfall.browsers import BrowserConnectError
from footfall.local import PROXY_PORT, LocalRunError, test_folder
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

RUNC_WARNING: Final = (
    "footfall: warning: FOOTFALL_RUNTIME=runc starts containers without gVisor, so code "
    "under test runs on the host's own kernel. Use it for development only, never on a server."
)
ALL_FILES_WARNING: Final = (
    "footfall test: warning: --all-files copies every file in the folder into the app's "
    "containers, including files git would ignore, such as a .env with real keys in it."
)
RUNTIMES: Final = ("runsc", "runc")
# The images the runner starts, as `docker build` tags them for development.
RUNTIME_IMAGES: Final = {"python3.13": "footfall-python3.13:dev", "node22": "footfall-node22:dev"}
BROWSERS_IMAGE: Final = "footfall-browsers:dev"
DEFAULT_OUT: Final = Path("footfall-results")
_CONTAINER_NAME: Final = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
_MIB: Final = 1024**2
_MOST_MIB: Final = 16 * 1024**2  # 16 TiB, far beyond any disk a run could have
_LOG_TAIL_CAP: Final = 20_000
# What each line of the app's output is printed behind: not white space, which the GitHub
# Actions runner trims before it looks for a command, and not a character it can skip.
_QUOTED: Final = "| "
# The older form of a workflow command, which the runner looks for anywhere in a line.
_LEGACY_COMMAND: Final = "##["
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
}


class SettingsError(ValueError):
    """A setting in the environment that Footfall cannot use."""


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

        def mebibytes(name: str) -> int:
            # Set but empty is an error too: a budget is never the default in silence.
            value = env[name] if name in env else _DEFAULTS[name]
            if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= _MOST_MIB:
                raise SettingsError(
                    f"{name} must be a whole number of MiB from 1 to {_MOST_MIB}, not {value!r}"
                )
            return int(value) * _MIB

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
            disk_budget=mebibytes("FOOTFALL_DISK_BUDGET_MIB"),
        )


# ---------------------------------------------------------------- the services


async def run_runner(settings: Settings) -> None:
    """Serve the runner on its socket until stopped.

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
    )
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
    """Run `work`, cancelling it on SIGTERM as asyncio already does on Ctrl-C, so that its
    cleanup, such as stopping a run, still happens."""

    async def guarded() -> T:
        task = asyncio.current_task()
        assert task is not None  # noqa: S101  # always inside asyncio.run
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
        return await work

    return asyncio.run(guarded())


# ---------------------------------------------------------------- footfall test


def _ids(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _no_legacy_commands(text: str) -> str:
    """`text` with every `##[` broken, so that the runner finds no command in the older form."""
    return text.replace(_LEGACY_COMMAND, "## [")


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
    return "\n".join(_QUOTED + _no_legacy_commands(line) for line in text.splitlines())


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
    return _no_legacy_commands(strip_invisible(text))


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
                workspaces=settings.workspaces,
                proxy_container=settings.proxy_container,
                all_files=args.all_files,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="footfall", description="Test web apps the way strangers would use them."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser(
        "test",
        help="test a local folder with AI personas",
        description=(
            "Test the app in a local folder. Exits with 0 when every persona ran, 1 when "
            "one failed or the run could not go ahead, and 2 for a setting that is wrong."
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
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
