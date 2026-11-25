"""The command line: `footfall test`, `footfall runner` and `footfall proxy`, and their settings."""

import asyncio
import contextlib
import os
import signal
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

import footfall.__main__ as cli
from footfall.__main__ import ALL_FILES_WARNING, RUNC_WARNING, Settings, main
from footfall.actions import MaskError
from footfall.local import NotReadyError
from footfall.manifest import ManifestError
from footfall.proxy.control import ProxyControlClient
from footfall.runner.client import RunnerClient
from footfall.runner.service import RunnerError, RunnerService
from footfall.tester.loop import Budget
from footfall.tester.model import DEFAULT_MODEL
from footfall.tester.report import PersonaReport, clean_text, strip_invisible

ENV_NAMES = (
    "FOOTFALL_RUNNER_SOCKET",
    "FOOTFALL_PROXY_CONTROL",
    "FOOTFALL_WORKSPACES",
    "FOOTFALL_RUNS",
    "FOOTFALL_RUNTIME",
    "FOOTFALL_PROXY_CONTAINER",
    "FOOTFALL_ORCHESTRATOR_CONTAINER",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def report(persona: str, status: Any = "finished") -> PersonaReport:
    return PersonaReport(persona, "Pixel 7", status, "r", None, None, "", [], 1, 0.0, 1.0)


class Recorder:
    """Stands in for `test_folder`, recording what it was asked to do."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Path, dict[str, Any]]] = []
        self.result = result if result is not None else [report("grace")]
        self.error = error

    async def __call__(self, folder: Path, **options: Any) -> Any:
        self.calls.append((folder, options))
        if self.error is not None:
            raise self.error
        return self.result


def test_settings_have_the_documented_defaults() -> None:
    settings = Settings.from_env({})
    assert settings == Settings(
        runner_socket=Path("/run/footfall/runner.sock"),
        # In a folder of its own, which only the proxy and the runner mount.
        proxy_control=Path("/run/footfall-proxy/proxy.sock"),
        workspaces=Path("/var/lib/footfall/workspaces"),
        runs=Path("/var/lib/footfall-runs"),
        runtime="runsc",
        proxy_container="footfall-proxy",
        orchestrator_container="footfall-orchestrator",
        disk_budget=8 * 1024**3,
        run_lifetime=90 * 60,
    )


def test_settings_come_from_the_environment() -> None:
    settings = Settings.from_env(
        {
            "FOOTFALL_RUNNER_SOCKET": "/srv/ff/r.sock",
            "FOOTFALL_PROXY_CONTROL": "/srv/ff/p.sock",
            "FOOTFALL_WORKSPACES": "/data/w",
            "FOOTFALL_RUNS": "/data/r",
            "FOOTFALL_RUNTIME": "runc",
            "FOOTFALL_PROXY_CONTAINER": "dev-proxy",
            "FOOTFALL_ORCHESTRATOR_CONTAINER": "dev-orchestrator",
            "FOOTFALL_DISK_BUDGET_MIB": "96",
            "FOOTFALL_RUN_LIFETIME_MINUTES": "45",
        }
    )
    assert settings == Settings(
        runner_socket=Path("/srv/ff/r.sock"),
        proxy_control=Path("/srv/ff/p.sock"),
        workspaces=Path("/data/w"),
        runs=Path("/data/r"),
        runtime="runc",
        proxy_container="dev-proxy",
        orchestrator_container="dev-orchestrator",
        disk_budget=96 * 1024**2,
        run_lifetime=45 * 60,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FOOTFALL_RUNTIME", "kata"),
        ("FOOTFALL_PROXY_CONTAINER", "proxy:8080"),
        ("FOOTFALL_ORCHESTRATOR_CONTAINER", "-orchestrator"),
        ("FOOTFALL_WORKSPACES", "relative/path"),
        ("FOOTFALL_DISK_BUDGET_MIB", "0"),
        ("FOOTFALL_DISK_BUDGET_MIB", "-5"),
        ("FOOTFALL_DISK_BUDGET_MIB", "1.5"),
        ("FOOTFALL_DISK_BUDGET_MIB", "lots"),
        ("FOOTFALL_DISK_BUDGET_MIB", "99999999999"),
        ("FOOTFALL_DISK_BUDGET_MIB", ""),  # set, but empty: never the default in silence
        ("FOOTFALL_DISK_BUDGET_MIB", " 512"),
    ],
)
def test_bad_settings_are_refused(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(name, value)
    recorder = Recorder()
    monkeypatch.setattr(cli, "test_folder", recorder)
    assert main(["test", "app", "--model", "fake:script.json"]) == 2
    assert name in capsys.readouterr().err
    assert recorder.calls == []


def test_cli_test_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FOOTFALL_RUNNER_SOCKET", str(tmp_path / "runner.sock"))
    monkeypatch.setenv("FOOTFALL_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setenv("FOOTFALL_PROXY_CONTAINER", "dev-proxy")
    recorder = Recorder()
    monkeypatch.setattr(cli, "test_folder", recorder)
    code = main(
        [
            "test",
            "demo-app",
            "--personas",
            "grace, kenji",
            "--brief",
            "smoke",
            "--focus",
            "the search box",
            "--model",
            "fake:script.json",
            "--out",
            "results",
        ]
    )
    assert code == 0
    ((folder, options),) = recorder.calls
    assert folder == Path("demo-app")
    runner = options.pop("runner")
    assert isinstance(runner, RunnerClient)
    assert runner._socket_path == tmp_path / "runner.sock"
    assert options == {
        "persona_ids": ["grace", "kenji"],
        "brief_name": "smoke",
        "focus": "the search box",
        "model_spec": "fake:script.json",
        "out": Path("results"),
        "budget": Budget(),
        "workspaces": tmp_path / "workspaces",
        "proxy_container": "dev-proxy",
        "all_files": False,
    }


def test_cli_test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")  # gitleaks:allow
    recorder = Recorder()
    monkeypatch.setattr(cli, "test_folder", recorder)
    assert main(["test", "app"]) == 0
    ((_, options),) = recorder.calls
    assert options["persona_ids"] == []
    assert options["brief_name"] == "default"
    assert options["focus"] is None
    assert options["model_spec"] == DEFAULT_MODEL
    assert options["out"] == Path("footfall-results")
    assert options["workspaces"] == Path("/var/lib/footfall/workspaces")
    assert options["proxy_container"] == "footfall-proxy"


def test_all_files_copies_every_file_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(cli, "test_folder", recorder)
    assert main(["test", "app", "--model", "fake:s.json", "--all-files"]) == 0
    ((_, options),) = recorder.calls
    assert options["all_files"] is True
    printed = capsys.readouterr()
    assert ALL_FILES_WARNING in printed.err
    assert ".env" in ALL_FILES_WARNING
    assert main(["test", "app", "--model", "fake:s.json"]) == 0
    assert recorder.calls[1][1]["all_files"] is False
    assert ALL_FILES_WARNING not in capsys.readouterr().err


def test_a_real_model_needs_the_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(cli, "test_folder", recorder)
    assert main(["test", "app"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert recorder.calls == []


def test_the_exit_code_says_whether_a_persona_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "test_folder", Recorder([report("grace"), report("kenji", "failed")]))
    assert main(["test", "app", "--model", "fake:s.json"]) == 1
    monkeypatch.setattr(
        cli, "test_folder", Recorder([report("grace"), report("kenji", "incomplete")])
    )
    assert main(["test", "app", "--model", "fake:s.json"]) == 0


@pytest.mark.parametrize(
    "error",
    [
        # Any message may hold what the tested repository wrote, as a key in its manifest.
        ManifestError(
            [
                "app.port: is required",
                "app.\x1b[2Jkey\nfootfall test: The results are in forged-marker.: unknown",
                "app.x##[add-mask]legacy-marker: unknown field",
            ]
        ),
        RunnerError("seed failed with exit code 1", "Traceback\n\x1b[2JValueError: bad seed"),
        NotReadyError("the copy of the app for grace did not answer /", "Listening\nCrashed"),
        MaskError("the mask \x1b]0;title\x07could not be written\nfootfall test: forged-marker"),
    ],
)
def test_a_run_that_cannot_go_ahead_says_why(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "test_folder", Recorder(error=error))
    assert main(["test", "app", "--model", "fake:s.json"]) == 1
    printed = capsys.readouterr().err
    first = str(error).splitlines()[0]
    assert clean_text(first, len(first), single_line=True) in printed
    tail = getattr(error, "log_tail", "")
    if tail:
        assert tail.splitlines()[-1].replace("\x1b[2J", "") in printed
    assert "\x1b" not in printed
    assert "\x07" not in printed
    assert "##[" not in printed
    lines = printed.splitlines()
    assert [line for line in lines if line.startswith("footfall test: ")] == [lines[0]]
    if isinstance(error, ManifestError):  # a line for each problem, as written
        assert len(lines) == 1 + len(error.problems)
        assert lines[1] == "  - app.port: is required"


def test_the_apps_last_output_cannot_give_a_github_actions_job_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Actions runner reads a line as a workflow command when it starts with `::` once
    leading white space is trimmed, and the older `##[` form anywhere in a line, comparing
    in a way that can skip characters that show nothing. Cleaning removes some characters,
    so a `::` behind one of them must still not lead a line."""
    lines = [
        "::stop-commands::tail-marker-stop",
        "::add-mask::tail-marker-mask",
        "##[error]tail-marker-legacy",
        "text then ##[warning]tail-marker-inline",
        "#\u00ad#[error]tail-marker-soft-hyphen",
        "   ::warning::tail-marker-indented",
        "\u200b::notice::tail-marker-zero-width",
        "\r::group::tail-marker-return",
        "before\u2028::debug::tail-marker-separator",
        "\x1b[2J::error::tail-marker-escape",
    ]
    tail = "\n".join(lines)
    monkeypatch.setattr(cli, "test_folder", Recorder(error=RunnerError("install failed", tail)))
    assert main(["test", "app", "--model", "fake:s.json"]) == 1
    printed = strip_invisible(capsys.readouterr().err)
    for line in printed.splitlines():
        assert not line.lstrip().startswith("::"), line
    assert "##[" not in printed
    for line in lines:  # still all there to read
        assert "tail-marker-" + line.rsplit("tail-marker-", 1)[1] in printed


def test_a_missing_folder_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOOTFALL_RUNNER_SOCKET", str(tmp_path / "nobody.sock"))
    assert main(["test", str(tmp_path / "missing"), "--model", "fake:s.json"]) == 1
    assert "footfall.toml" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "", "lots", "-1"])
def test_a_bad_disk_budget_stops_the_runner_before_it_starts(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = stub_services(monkeypatch)
    monkeypatch.setenv("FOOTFALL_DISK_BUDGET_MIB", value)
    assert main(["runner"]) == 2
    assert started == []
    assert "FOOTFALL_DISK_BUDGET_MIB must be a whole number of MiB" in capsys.readouterr().err


def test_a_runs_folder_others_can_write_stops_the_runner_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "workspaces").mkdir()
    runs = tmp_path / "runs-6203"
    runs.mkdir()
    runs.chmod(0o770)
    monkeypatch.setenv("FOOTFALL_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setenv("FOOTFALL_RUNS", str(runs))
    assert main(["runner"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("footfall runner: "), err
    assert "must belong to root and be writable by root alone" in err
    assert "runs-6203" in err


@pytest.mark.parametrize("value", ["0", "", "soon", "-5", "1.5", "10081", "\uff13"])
def test_a_bad_run_lifetime_stops_the_runner_before_it_starts(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = stub_services(monkeypatch)
    monkeypatch.setenv("FOOTFALL_RUN_LIFETIME_MINUTES", value)
    assert main(["runner"]) == 2
    assert started == []
    assert (
        "FOOTFALL_RUN_LIFETIME_MINUTES must be a whole number of minutes from 1 to 10080"
        in capsys.readouterr().err
    )


def test_a_hang_up_stops_a_command_as_a_terminate_does() -> None:
    """A lost terminal sends SIGHUP; the command must still stop its run on the way out."""
    cleaned: list[str] = []

    async def work() -> None:
        try:
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(30)
        finally:
            cleaned.append("stopped the run")

    with pytest.raises(asyncio.CancelledError):
        cli._until_stopped(work())
    assert cleaned == ["stopped the run"]


# ---------------------------------------------------------------- the runc warning


def stub_services(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    async def runner(settings: Settings) -> None:
        started.append("runner")

    async def proxy(settings: Settings) -> None:
        started.append("proxy")

    monkeypatch.setattr(cli, "run_runner", runner)
    monkeypatch.setattr(cli, "run_proxy", proxy)
    monkeypatch.setattr(cli, "test_folder", Recorder())
    return started


@pytest.mark.parametrize("argv", [["runner"], ["proxy"], ["test", "app", "--model", "fake:s.json"]])
def test_runc_warning_printed(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_services(monkeypatch)
    monkeypatch.setenv("FOOTFALL_RUNTIME", "runc")
    assert main(argv) == 0
    printed = capsys.readouterr()
    assert RUNC_WARNING in printed.err
    assert "gVisor" in RUNC_WARNING
    assert RUNC_WARNING not in printed.out


def test_runc_warning_even_when_the_command_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOOTFALL_RUNTIME", "runc")
    with pytest.raises(SystemExit):
        main(["no-such-command"])
    assert RUNC_WARNING in capsys.readouterr().err


@pytest.mark.parametrize("runtime", [None, "runsc"])
def test_no_warning_with_gvisor(
    runtime: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = stub_services(monkeypatch)
    if runtime is not None:
        monkeypatch.setenv("FOOTFALL_RUNTIME", runtime)
    assert main(["runner"]) == 0
    assert main(["proxy"]) == 0
    assert started == ["runner", "proxy"]
    assert RUNC_WARNING not in capsys.readouterr().err


# ---------------------------------------------------------------- the services


def short_dir() -> Path:
    """A folder with a short path, since a Unix socket's path has a small limit."""
    return Path(tempfile.mkdtemp(prefix="ff-", dir="/tmp"))


def a_group_no_new_file_gets(folder: Path) -> int:
    """A group this process may give a file it owns, other than either group a new file in
    `folder` would get on its own: the process's, or, on some systems, the folder's."""
    defaults = {os.getegid(), folder.stat().st_gid}
    if os.geteuid() == 0:
        return next(group for group in range(10_011, 11_000) if group not in defaults)
    others = sorted(set(os.getgroups()) - defaults)
    if not others:
        pytest.skip("this user is in no group but its own, so no other group can be given")
    return others[0]


async def test_the_runner_socket_is_for_the_runner_and_its_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []
    swept: list[bool] = []
    real = RunnerService

    def recording(*args: Any, **options: Any) -> RunnerService:
        built.append(options)
        # The runs folder must be root's; here, the test's own user stands in for root.
        service = real(*args, **options, runs_owner=os.getuid())

        # There is no Docker here to sweep; that the runner sweeps before it serves is what
        # this records.
        def sweep() -> list[str]:
            swept.append(socket_path.exists())
            return []

        monkeypatch.setattr(service, "sweep", sweep)
        return service

    monkeypatch.setattr(cli, "RunnerService", recording)
    (tmp_path / "workspaces").mkdir()
    folder = short_dir()
    # The runner gives its socket to its own group, which for a runner started as "0:10001"
    # is the orchestrator's. Here that group is one no new file gets by default, so the test
    # fails unless the socket is really given to it.
    group = a_group_no_new_file_gets(folder)
    monkeypatch.setattr(os, "getegid", lambda: group)
    try:
        settings = Settings.from_env(
            {
                "FOOTFALL_RUNNER_SOCKET": str(folder / "runner.sock"),
                "FOOTFALL_PROXY_CONTROL": str(folder / "proxy.sock"),
                "FOOTFALL_WORKSPACES": str(tmp_path / "workspaces"),
                "FOOTFALL_RUNS": str(tmp_path / "runs"),
                "FOOTFALL_RUNTIME": "runc",
                "FOOTFALL_PROXY_CONTAINER": "dev-proxy",
                "FOOTFALL_ORCHESTRATOR_CONTAINER": "dev-orchestrator",
                "FOOTFALL_DISK_BUDGET_MIB": "77",
            }
        )
        socket_path = folder / "runner.sock"

        async def check() -> None:
            info = socket_path.lstat()
            assert stat.S_ISSOCK(info.st_mode)
            assert stat.S_IMODE(info.st_mode) == 0o660
            assert info.st_gid == group
            client = RunnerClient(socket_path)
            assert await client.status("r_" + "a" * 26) is None

        task = asyncio.create_task(cli.run_runner(settings))
        try:
            for _ in range(200):
                if socket_path.exists():
                    break
                await asyncio.sleep(0.01)
            await check()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert not socket_path.exists()
    finally:
        for leftover in folder.iterdir():
            leftover.unlink()
        folder.rmdir()
    (options,) = built
    assert options["runtime"] == "runc"
    assert options["workspaces"] == tmp_path / "workspaces"
    assert options["runs"] == tmp_path / "runs"
    assert options["proxy_container"] == "dev-proxy"
    assert options["orchestrator_container"] == "dev-orchestrator"
    assert options["proxy_port"] == 3128
    assert options["disk_budget"] == 77 * 1024**2
    assert options["lifetime"] == 90 * 60
    assert swept == [False]  # swept once, before the socket was there to answer
    assert options["browsers_image"] == "footfall-browsers:dev"
    assert options["images"] == {
        "python3.13": "footfall-python3.13:dev",
        "node22": "footfall-node22:dev",
    }


async def refused(port: int) -> bool:
    """Whether a connection to 127.0.0.1 at `port` is refused, so that nothing listens there."""
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return True
    writer.close()
    return False


async def test_the_proxy_listens_only_where_the_runner_tells_it() -> None:
    folder = short_dir()
    control = folder / "proxy.sock"
    try:
        settings = Settings.from_env({"FOOTFALL_PROXY_CONTROL": str(control)})
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        task = asyncio.create_task(cli.run_proxy(settings, port=port, allowed_uid=os.getuid()))
        try:
            for _ in range(200):
                if control.exists():
                    break
                await asyncio.sleep(0.01)
            assert stat.S_IMODE(control.lstat().st_mode) == 0o600
            # Serving the control socket, it listens on no interface, loopback included.
            assert await refused(port)
            client = ProxyControlClient(control)
            run_id = "r_" + "b" * 26
            await asyncio.to_thread(client.allow, "10.89.0.7", run_id, ["example.org"])
            assert await asyncio.to_thread(client.reached, run_id) == []
            await asyncio.to_thread(client.listen, "127.0.0.1", run_id)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n")
            answer = await reader.readline()
            writer.close()
            assert answer.startswith(b"HTTP/1.1 403")  # 127.0.0.1 has no allow list
            await asyncio.to_thread(client.revoke_run, run_id)
            assert await refused(port)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        for leftover in folder.iterdir():
            leftover.unlink()
        folder.rmdir()
