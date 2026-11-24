"""`test_folder`: one run of a local folder, from the manifest to the results, with fakes.

The runner is a fake that records each request. Most tests start every app copy as the same
`tests/app_server.py`; the sign-in tests run the demo app itself, seeded and served on
127.0.0.1 as the runner would run it in containers. The browsers are local Chromium with name
lookups switched off, and the model is a `FakeModel` or a scripted tester.
"""

import asyncio
import contextlib
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, async_playwright

from footfall import local
from footfall.browsers import BrowserConnectError, open_context
from footfall.local import (
    LocalRunError,
    NotReadyError,
    OutputError,
    format_table,
    new_run_id,
    test_folder,
    wait_ready,
)
from footfall.manifest import ManifestError
from footfall.runner.service import RunnerError
from footfall.tester import tools
from footfall.tester.loop import Budget, run_persona
from footfall.tester.model import ModelTurn
from footfall.tester.pricing import Usage
from footfall.tester.report import Finding, PersonaReport, clean_text, strip_invisible
from footfall.tester.screen import CaptureError, capture
from footfall.tester.tools import Executor
from tests.app_server import AppServer

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo-app"
SCRIPTS = Path(__file__).parent / "fixtures" / "scripts"
FINISHES = f"fake:{SCRIPTS / 'finishes.json'}"
ENDPOINT = "ws://10.89.0.40:3000/bm90LWEtcmVhbC1wYXRoLWJ1dC1jbG9zZQ"
HOME = (
    "<!doctype html><title>Home</title><h1>Home</h1>"
    '<a href="/second" style="font-size:30px">Second</a>'
)
SECOND = "<!doctype html><title>Second</title><h1>Second page</h1>"
MANIFEST = """\
name = "Two pages"

[app]
runtime = "python3.13"
start = "python3 serve.py"
port = 8080
ready = "/healthz"
ready_timeout = 2
egress = ["api.example.com"]

[browser]
assets = ["fonts.example.com"]
"""
CHROMIUM_PERSONAS = ["grace", "kenji"]  # both on Chromium devices, so one local browser


# ---------------------------------------------------------------- fakes


class FakeRunner:
    """Answers as the runner would, and records every request."""

    def __init__(self, address: Callable[[str], str]) -> None:
        self.address = address
        self.calls: list[tuple[Any, ...]] = []
        self.envs: list[dict[str, str]] = []
        self.log_text = ""
        self.fail: dict[str, Exception] = {}
        self.workspace_files: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise self.fail[op]

    async def prepare(
        self,
        run_id: str,
        workspace: Path,
        runtime: str,
        install: str | None,
        seed: str | None,
        egress: Sequence[str],
        env: Mapping[str, str],
    ) -> None:
        self.calls.append(("prepare", run_id, workspace, runtime, install, seed, tuple(egress)))
        self.envs.append(dict(env))
        self.workspace_files = sorted(
            f"{p.relative_to(workspace)} -> {p.readlink()}"
            if p.is_symlink()
            else str(p.relative_to(workspace))
            for p in workspace.rglob("*")
            if p.is_symlink() or p.is_file()
        )
        self._maybe_fail("prepare")

    async def start_app(
        self, run_id: str, copy: str, start: str, port: int, egress: Sequence[str]
    ) -> str:
        self.calls.append(("start_app", run_id, copy, start, port, tuple(egress)))
        self._maybe_fail("start_app")
        return self.address(copy)

    async def start_browsers(self, run_id: str, assets: Sequence[str]) -> str:
        self.calls.append(("start_browsers", run_id, tuple(assets)))
        self._maybe_fail("start_browsers")
        return ENDPOINT

    async def logs(self, run_id: str, copy: str) -> str:
        self.calls.append(("logs", run_id, copy))
        return self.log_text

    async def stop(self, run_id: str) -> None:
        self.calls.append(("stop", run_id))
        self._maybe_fail("stop")

    def ops(self) -> list[str]:
        return [call[0] for call in self.calls]


Connect = Callable[[str, str], Awaitable[Browser]]


@pytest.fixture
async def connect() -> AsyncIterator[tuple[Connect, list[tuple[str, str]]]]:
    """A `connect_browser` that launches local Chromium, for any engine, and records its calls.

    Chromium resolves no names at all, so nothing a page asks for leaves this machine.
    """
    calls: list[tuple[str, str]] = []
    launched: list[Browser] = []
    async with async_playwright() as playwright:

        async def launch(engine: str, endpoint: str) -> Browser:
            calls.append((engine, endpoint))
            browser = await playwright.chromium.launch(
                args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"]
            )
            launched.append(browser)
            return browser

        try:
            yield launch, calls
        finally:
            for browser in launched:
                await browser.close()


def two_pages(tmp_path: Path, manifest: str = MANIFEST) -> Path:
    folder = tmp_path / "app"
    (folder / "footfall" / "briefs").mkdir(parents=True)
    (folder / "footfall.toml").write_text(manifest)
    (folder / "footfall" / "briefs" / "default.md").write_text("Look around both pages.\n")
    (folder / "serve.py").write_text("print('not run in these tests')\n")
    return folder


def serve_two_pages(app: AppServer) -> None:
    app.page("/", HOME)
    app.page("/second", SECOND)
    app.reply("/healthz", "ok", headers={"Content-Type": "text/plain"})


async def run_folder(
    folder: Path,
    runner: Any,
    connect_browser: Connect,
    tmp_path: Path,
    *,
    personas: Sequence[str] = ("grace",),
    model: str = FINISHES,
    brief: str = "default",
    all_files: bool = True,
) -> list[Any]:
    return await test_folder(
        folder,
        persona_ids=personas,
        brief_name=brief,
        focus=None,
        model_spec=model,
        out=tmp_path / "out",
        runner=runner,
        budget=Budget(max_actions=8, max_seconds=120),
        connect_browser=connect_browser,
        workspaces=tmp_path / "workspaces",
        all_files=all_files,
    )


# ---------------------------------------------------------------- small parts


def test_new_run_id_matches_the_runners_rule() -> None:
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"r_[a-z2-7]{26}", run_id) for run_id in ids)


def test_test_folder_is_not_collected_as_a_test() -> None:
    assert test_folder.__test__ is False  # type: ignore[attr-defined]


async def test_wait_ready_returns_once_the_app_answers(app_server: AppServer) -> None:
    address = f"127.0.0.1:{app_server.port}"
    app_server.reply("/healthz", status=503)

    async def recover() -> None:
        await asyncio.sleep(0.6)
        app_server.reply("/healthz", "ok")

    started = time.monotonic()
    await asyncio.gather(wait_ready(address, "/healthz", 10), recover())
    assert 0.5 < time.monotonic() - started < 5
    assert app_server.paths().count("/healthz") >= 2


async def test_wait_ready_gives_up_after_the_timeout(app_server: AppServer) -> None:
    app_server.reply("/healthz", status=500)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await wait_ready(f"127.0.0.1:{app_server.port}", "/healthz", 1)
    assert time.monotonic() - started < 3
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    with pytest.raises(TimeoutError):
        await wait_ready(f"127.0.0.1:{closed}", "/", 1)


def test_the_table_has_a_line_for_each_persona_and_a_total() -> None:
    finding = Finding("t", "minor", "/", (), "e", "a", (), "grace", "Pixel 7")
    reports = [
        PersonaReport(
            "grace", "Pixel 7", "finished", "x", 7, "yes", "", [finding] * 2, 5, 0.1234, 9.0
        ),
        PersonaReport("kenji", "Pixel 5", "failed", "y", None, None, "", [], 0, 0.0, 1.0),
    ]
    lines = format_table(reports).splitlines()
    assert re.split(r"\s{2,}", lines[0]) == ["persona", "status", "findings", "actions", "cost"]
    assert re.split(r"\s{2,}", lines[1]) == ["grace", "finished", "2", "5", "$0.12"]
    assert re.split(r"\s{2,}", lines[2]) == ["kenji", "failed", "0", "0", "$0.00"]
    assert re.split(r"\s{2,}", lines[3].strip()) == ["total", "2", "5", "$0.12"]


# ---------------------------------------------------------------- whole runs


async def test_test_folder_writes_results(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    serve_two_pages(app_server)
    launch, launches = connect
    folder = two_pages(tmp_path)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(folder, runner, launch, tmp_path, personas=CHROMIUM_PERSONAS)

    assert [(r.persona, r.status) for r in reports] == [
        ("grace", "finished"),
        ("kenji", "finished"),
    ]
    out = tmp_path / "out"
    for persona in CHROMIUM_PERSONAS:
        report = json.loads((out / persona / "report.json").read_text())
        assert report["persona"] == persona
        assert report["status"] == "finished"
        assert report["findings"][0]["title"] == "No way back from the second page"
        steps = (out / persona / "trace.jsonl").read_text().splitlines()
        assert [json.loads(step)["tool"] for step in steps] == ["go", "finish"]
        shots = sorted(p.name for p in (out / persona / "screenshots").iterdir())
        assert shots == ["01.jpg", "02.jpg"]
    summary = json.loads((out / "summary.json").read_text())
    assert re.fullmatch(r"r_[a-z2-7]{26}", summary["run_id"])
    assert summary["app"] == "Two pages"
    assert summary["brief"] == "default"
    assert [p["persona"] for p in summary["personas"]] == CHROMIUM_PERSONAS
    assert summary["personas"][0]["findings"] == 1
    assert summary["cost_usd"] == 0.0

    run_id = summary["run_id"]
    workspace = tmp_path / "workspaces" / run_id
    assert runner.calls[0] == (
        "prepare",
        run_id,
        workspace,
        "python3.13",
        None,
        None,
        ("api.example.com",),
    )
    assert runner.envs == [{}]  # no [login], so no account and no password
    assert runner.workspace_files == ["footfall.toml", "footfall/briefs/default.md", "serve.py"]
    starts = [call for call in runner.calls if call[0] == "start_app"]
    assert starts == [
        ("start_app", run_id, persona, "python3 serve.py", 8080, ("api.example.com",))
        for persona in CHROMIUM_PERSONAS
    ]
    assert ("start_browsers", run_id, ("fonts.example.com",)) in runner.calls
    assert runner.ops()[-1] == "stop"
    assert runner.ops().count("stop") == 1
    assert launches == [("chromium", ENDPOINT)]
    assert not workspace.exists()
    assert app_server.paths().count("/healthz") >= 2

    printed = capsys.readouterr().out
    assert re.split(r"\s{2,}", printed.splitlines()[0]) == [
        "persona",
        "status",
        "findings",
        "actions",
        "cost",
    ]
    assert re.split(r"\s{2,}", printed.splitlines()[1]) == ["grace", "finished", "1", "1", "$0.00"]
    assert ENDPOINT not in printed


async def test_contexts_use_the_proxy_container_and_the_device(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_two_pages(app_server)
    opened: list[tuple[str, str, str]] = []
    executors: list[bool] = []
    real_open = open_context
    real_executor = Executor

    async def recording_open(browser: Browser, persona: Any, device: Any, *rest: Any) -> Any:
        descriptors, origin, proxy = rest
        opened.append((persona.id, origin, proxy))
        return await real_open(browser, persona, device, descriptors, origin, proxy)

    def recording_executor(*args: Any, **options: Any) -> Any:
        executors.append(options["touch"])
        return real_executor(*args, **options)

    monkeypatch.setattr(local, "open_context", recording_open)
    monkeypatch.setattr(local, "Executor", recording_executor)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await test_folder(
        two_pages(tmp_path),
        persona_ids=["olga", "kenji"],  # a laptop without touch, and a phone with it
        brief_name="default",
        focus=None,
        model_spec=FINISHES,
        out=tmp_path / "out",
        runner=runner,
        budget=Budget(max_actions=8, max_seconds=120),
        connect_browser=connect[0],
        workspaces=tmp_path / "workspaces",
        proxy_container="proxy-for-tests",
        all_files=True,
    )
    assert [r.status for r in reports] == ["finished", "finished"]
    origin = f"http://127.0.0.1:{app_server.port}"
    assert sorted(opened) == [
        ("kenji", origin, "http://proxy-for-tests:3128"),
        ("olga", origin, "http://proxy-for-tests:3128"),
    ]
    assert sorted(executors) == [False, True]


async def test_unready_app_fails_with_logs_and_cleans_up(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    runner = FakeRunner(lambda copy: f"127.0.0.1:{closed}")
    runner.log_text = "Traceback (most recent call last):\n\x1b[31mOSError: port in use\x1b[0m\n"
    started = time.monotonic()
    with pytest.raises(NotReadyError) as caught:
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert time.monotonic() - started < 10
    assert "grace" in str(caught.value)
    assert "/healthz" in str(caught.value)
    assert "OSError: port in use" in caught.value.log_tail
    assert "\x1b" not in caught.value.log_tail  # no terminal escapes reach the operator
    assert ("logs", runner.calls[0][1], "grace") in runner.calls
    assert runner.ops()[-1] == "stop"
    assert "start_browsers" not in runner.ops()
    assert connect[1] == []
    assert list((tmp_path / "workspaces").iterdir()) == []
    # Nothing was written for the persona, so its empty folder goes, and a run can follow.
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize("op", ["prepare", "start_app", "start_browsers"])
async def test_stop_is_called_when_a_step_fails(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    op: str,
) -> None:
    serve_two_pages(app_server)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    runner.fail[op] = RunnerError(f"{op} went wrong", "the last lines")
    with pytest.raises(RunnerError, match=f"{op} went wrong"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert runner.ops()[-1] == "stop"
    assert list((tmp_path / "workspaces").iterdir()) == []


async def test_stop_is_called_when_the_browsers_cannot_be_reached(
    tmp_path: Path, app_server: AppServer
) -> None:
    serve_two_pages(app_server)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")

    async def unreachable(engine: str, endpoint: str) -> Browser:
        raise BrowserConnectError("could not connect to the run's chromium browser")

    with pytest.raises(BrowserConnectError):
        await run_folder(two_pages(tmp_path), runner, unreachable, tmp_path)
    assert runner.ops()[-1] == "stop"


async def test_a_failed_stop_is_reported(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    serve_two_pages(app_server)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    runner.fail["stop"] = RunnerError("could not remove footfall-r_x-app-grace")
    with pytest.raises(RunnerError, match="could not remove"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert (tmp_path / "out" / "grace" / "report.json").exists()


async def test_a_persona_that_fails_leaves_the_others_running(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    serve_two_pages(app_server)
    real = run_persona

    async def kenji_breaks(**options: Any) -> Any:
        if options["persona"].id == "kenji":
            raise RuntimeError("something inside the persona broke")
        return await real(**options)

    monkeypatch.setattr(local, "run_persona", kenji_breaks)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(
        two_pages(tmp_path), runner, connect[0], tmp_path, personas=CHROMIUM_PERSONAS
    )
    assert [(r.persona, r.status, r.reason) for r in reports] == [
        ("grace", "finished", "the tester called finish"),
        ("kenji", "failed", "RuntimeError"),
    ]
    failed = json.loads((tmp_path / "out" / "kenji" / "report.json").read_text())
    assert failed["status"] == "failed"
    assert "broke" not in json.dumps(failed)
    assert runner.ops()[-1] == "stop"
    printed = capsys.readouterr().out
    assert re.split(r"\s{2,}", printed.splitlines()[2]) == ["kenji", "failed", "0", "0", "$0.00"]


async def test_a_page_that_stops_answering_ends_only_its_persona(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_two_pages(app_server)
    closed: list[str] = []
    real_open = open_context

    async def tracking_open(browser: Browser, persona: Any, *rest: Any) -> Any:
        context = await real_open(browser, persona, *rest)
        context.on("close", lambda _: closed.append(persona.id))
        if persona.id == "kenji":
            # Every capture of this persona's page hangs, as a hostile page's would.
            await context.add_init_script("window.__hang = true;")
        return context

    async def hanging_capture(page: Any, *, timeout: float) -> Any:
        if await page.evaluate("Boolean(window.__hang)"):
            raise CaptureError("the page did not answer")
        return await capture(page, timeout=timeout)

    monkeypatch.setattr(tools, "capture", hanging_capture)
    monkeypatch.setattr(local, "open_context", tracking_open)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(
        two_pages(tmp_path), runner, connect[0], tmp_path, personas=CHROMIUM_PERSONAS
    )
    assert [(r.persona, r.status) for r in reports] == [("grace", "finished"), ("kenji", "failed")]
    assert reports[1].reason == "the page stopped answering"
    assert sorted(closed) == CHROMIUM_PERSONAS


async def test_existing_results_are_never_overwritten(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    (tmp_path / "out" / "grace").mkdir(parents=True)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(OutputError, match="already"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert runner.calls == []


async def test_a_bad_manifest_starts_nothing(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    folder = two_pages(tmp_path, MANIFEST.replace('runtime = "python3.13"', 'runtime = "cobol"'))
    with pytest.raises(ManifestError):
        await run_folder(folder, runner, connect[0], tmp_path)
    assert runner.calls == []
    assert not (tmp_path / "workspaces").exists()


async def test_the_results_folder_is_left_out_of_the_copy(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    (folder / "results" / "old").mkdir(parents=True)
    (folder / "results" / "old" / "report.json").write_text("{}")
    (folder / "link").symlink_to("/etc/hosts")
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    await test_folder(
        folder,
        persona_ids=["grace"],
        brief_name="default",
        focus=None,
        model_spec=FINISHES,
        out=folder / "results",
        runner=runner,
        budget=Budget(max_actions=8, max_seconds=120),
        connect_browser=connect[0],
        workspaces=tmp_path / "workspaces",
        all_files=True,
    )
    # A link is copied as a link, never as the host file it points at.
    assert runner.workspace_files == [
        "footfall.toml",
        "footfall/briefs/default.md",
        "link -> /etc/hosts",
        "serve.py",
    ]


# ---------------------------------------------------------------- signing in


class LocalRunner(FakeRunner):
    """Runs the app's seed and copies as local processes on 127.0.0.1, as containers would."""

    def __init__(self, base: Path) -> None:
        super().__init__(lambda copy: "")
        self.base = base
        self.processes: list[subprocess.Popen[bytes]] = []
        self.ports: dict[str, int] = {}

    async def prepare(
        self,
        run_id: str,
        workspace: Path,
        runtime: str,
        install: str | None,
        seed: str | None,
        egress: Sequence[str],
        env: Mapping[str, str],
    ) -> None:
        await super().prepare(run_id, workspace, runtime, install, seed, egress, env)
        prepared = self.base / "prepared"
        shutil.copytree(workspace, prepared)
        done = subprocess.run(
            [sys.executable, "seed.py"],
            cwd=prepared,
            env={"PATH": "/usr/bin:/bin", **env},
            capture_output=True,
            check=False,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr

    async def start_app(
        self, run_id: str, copy: str, start: str, port: int, egress: Sequence[str]
    ) -> str:
        self.calls.append(("start_app", run_id, copy, start, port, tuple(egress)))
        folder = self.base / "copies" / copy
        shutil.copytree(self.base / "prepared", folder)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            free = sock.getsockname()[1]
        self.processes.append(
            subprocess.Popen(
                [sys.executable, "serve.py"],
                cwd=folder,
                env={"PATH": "/usr/bin:/bin", "PORT": str(free)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        return f"127.0.0.1:{free}"

    def close(self) -> None:
        for process in self.processes:
            process.terminate()
            process.wait(timeout=10)


@pytest.fixture
def demo_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "tiny-tasks"
    shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("*.sqlite3*", "__pycache__"))
    brief = folder / "footfall" / "briefs" / "signed-out.md"
    brief.write_text('+++\nstart = "signed-out"\n+++\n\nSign in and look at your list.\n')
    return folder


@pytest.fixture
def local_runner(tmp_path: Path) -> Iterator[LocalRunner]:
    runner = LocalRunner(tmp_path / "local-runner")
    try:
        yield runner
    finally:
        runner.close()


ACCOUNT = re.compile(r"the username (\S+) and the password (\S+)\.")


def latest_screen(messages: list[dict[str, Any]]) -> str:
    content = messages[-1]["content"]
    first = content[0]
    return str(first["content"][0]["text"] if first["type"] == "tool_result" else first["text"])


class SigningInTester:
    """A scripted tester that signs in with the account it was given, then reports the password.

    It reads the username and password from the first message, as a model would, and finds
    the fields on each screen by their names.
    """

    def __init__(self) -> None:
        self.turns = 0
        self.first: list[dict[str, Any]] = []
        self.systems: list[str] = []
        self.screens: list[str] = []

    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        self.turns += 1
        self.systems.append(system)
        self.first = messages[0]["content"]
        screen = latest_screen(messages)
        self.screens.append(screen)
        text = " ".join(b.get("text", "") for b in self.first if b["type"] == "text")
        found = ACCOUNT.search(text)
        assert found is not None
        username, password = found.groups()

        def element(name: str) -> int:
            match = re.search(rf'\[(\d+)\] \w+ "{name}"', screen)
            assert match is not None, screen
            return int(match.group(1))

        def tap(name: str) -> tuple[str, dict[str, Any]]:
            return "tap", {"element": element(name), "x": None, "y": None}

        steps: list[Callable[[], tuple[str, dict[str, Any]]]] = [
            lambda: ("go", {"to": "/login"}),
            lambda: tap("Username"),
            lambda: ("type", {"text": username}),
            lambda: tap("Password"),
            lambda: ("type", {"text": password}),
            lambda: tap("Sign in"),
            lambda: (
                "finish",
                {
                    "summary": f"Signed in with {password} and saw the list.",
                    "score": 7,
                    "goal_reached": "yes",
                    "findings": [
                        {
                            "title": f"The password {password} is easy to mistype",
                            "severity": "minor",
                            "page": "/login",
                            "steps": [f"Type {password}"],
                            "expected": "A way to show the password.",
                            "actual": f"Only dots for {password}.",
                            "screenshots": ["02"],
                        }
                    ],
                },
            ),
        ]
        name, data = steps[self.turns - 1]()
        block = {"type": "tool_use", "id": f"toolu_{self.turns:03d}", "name": name, "input": data}
        return ModelTurn([block], "tool_use", Usage(0, 0, 0, 0), "fake")


def everything_written(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())


async def test_the_password_never_reaches_the_results_even_when_typed(
    tmp_path: Path,
    demo_folder: Path,
    local_runner: LocalRunner,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    tester = SigningInTester()
    monkeypatch.setattr(local, "model_from_spec", lambda spec: tester)
    reports = await run_folder(
        demo_folder, local_runner, connect[0], tmp_path, personas=["grace"], brief="signed-out"
    )
    ((env,),) = [local_runner.envs]
    username, password = env["FOOTFALL_USERNAME"], env["FOOTFALL_PASSWORD"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", password)
    assert username != password

    (report,) = reports
    assert report.status == "finished", report.reason
    assert "Tiny Tasks" in tester.screens[-1] or "Sign out" in tester.screens[-1]
    first_text = json.dumps(tester.first)
    assert password in first_text  # the tester is given it, in its first message
    assert password not in tester.systems[0]  # and never in the system prompt

    out = everything_written(tmp_path / "out")
    assert password.encode() not in out
    assert b"[password]" in (tmp_path / "out" / "grace" / "report.json").read_bytes()
    assert b"[password]" in (tmp_path / "out" / "grace" / "trace.jsonl").read_bytes()
    printed = capsys.readouterr()
    assert password not in printed.out + printed.err
    assert password not in caplog.text
    # The password went to the seed step's environment, and to no other request.
    requests = json.dumps([[str(part) for part in call] for call in local_runner.calls])
    assert password not in requests


async def test_footfall_signs_the_persona_in_when_the_brief_starts_signed_in(
    tmp_path: Path,
    demo_folder: Path,
    local_runner: LocalRunner,
    connect: tuple[Connect, list[tuple[str, str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = tmp_path / "look.json"
    script.write_text(
        json.dumps(
            [
                [
                    {
                        "tool": "finish",
                        "input": {
                            "summary": "Looked.",
                            "score": 5,
                            "goal_reached": "partly",
                            "findings": [],
                        },
                    }
                ]
            ]
        )
    )
    reports = await test_folder(
        demo_folder,
        persona_ids=["grace"],
        brief_name="default",
        focus=None,
        model_spec=f"fake:{script}",
        out=tmp_path / "out",
        runner=local_runner,
        budget=Budget(max_actions=8, max_seconds=120),
        connect_browser=connect[0],
        workspaces=tmp_path / "workspaces",
        all_files=True,
    )
    (report,) = reports
    assert report.status == "finished", report.reason
    password = local_runner.envs[0]["FOOTFALL_PASSWORD"]
    trace = (tmp_path / "out" / "grace" / "trace.jsonl").read_text()
    assert json.loads(trace.splitlines()[0])["path_before"] == "/"  # signed in, on the list
    assert password.encode() not in everything_written(tmp_path / "out")
    assert password not in capsys.readouterr().out


# ---------------------------------------------------------------- which files are copied


GIT = shutil.which("git") or "git"


def git(folder: Path, *arguments: str) -> None:
    subprocess.run([GIT, "-C", str(folder), *arguments], check=True, capture_output=True)


async def test_only_the_files_git_would_commit_are_copied(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    (folder / ".gitignore").write_text(".env\nnode_modules/\n")
    (folder / ".env").write_text("API_KEY=sk-not-a-real-key\n")  # gitleaks:allow
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "left-pad.js").write_text("module.exports = 1;\n")
    (folder / "notes.txt").write_text("not committed yet, and not ignored\n")
    (folder / "link").symlink_to("serve.py")
    (folder / "gone.txt").write_text("tracked, then deleted\n")
    git(folder, "add", "footfall.toml", "gone.txt")
    (folder / "gone.txt").unlink()
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(folder, runner, connect[0], tmp_path, all_files=False)
    assert [r.status for r in reports] == ["finished"]
    assert runner.workspace_files == [
        ".gitignore",
        "footfall.toml",
        "footfall/briefs/default.md",
        "link -> serve.py",
        "notes.txt",
        "serve.py",
    ]


async def test_results_inside_a_repository_are_not_copied(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    (folder / "results" / "old").mkdir(parents=True)
    (folder / "results" / "old" / "report.json").write_text("{}")
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    await test_folder(
        folder,
        persona_ids=["grace"],
        brief_name="default",
        focus=None,
        model_spec=FINISHES,
        out=folder / "results",
        runner=runner,
        budget=Budget(max_actions=8, max_seconds=120),
        connect_browser=connect[0],
        workspaces=tmp_path / "workspaces",
    )
    assert runner.workspace_files == ["footfall.toml", "footfall/briefs/default.md", "serve.py"]


async def test_a_folder_outside_git_is_refused_without_all_files(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError, match="--all-files"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path, all_files=False)
    assert runner.calls == []
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------- log tails and failures

LOGIN_MANIFEST = (
    MANIFEST
    + """
[login]
page = "/login"
username_label = "Username"
password_label = "Password"
submit = "Sign in"

[test]
briefs = "footfall/briefs"
"""
)


def split_password(password: str) -> str:
    """A log that holds the password whole, cut by a bell, and cut by a zero-width space."""
    half = len(password) // 2
    return (
        f"seeding with {password}\n"
        f"{password[:half]}\x07{password[half:]}\n"
        f"{password[:3]}\u200b{password[3:]}\n"
    )


def assert_scrubbed(tail: str, password: str) -> None:
    assert "[password]" in tail
    assert password not in strip_invisible(clean_text(tail, 10**9))


async def test_a_runner_log_tail_never_rebuilds_the_password(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    folder = two_pages(tmp_path, LOGIN_MANIFEST)
    (folder / "footfall" / "briefs" / "default.md").write_text(
        '+++\nstart = "signed-out"\n+++\nLook around.\n'
    )

    class SeedFails(FakeRunner):
        async def prepare(self, run_id: str, workspace: Path, *rest: Any) -> None:
            env = rest[-1]
            raise RunnerError(
                "seed failed with exit code 1", split_password(env["FOOTFALL_PASSWORD"])
            )

    runner = SeedFails(lambda copy: "127.0.0.1:9")
    with pytest.raises(RunnerError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path)
    tail = caught.value.log_tail
    password = re.search(r"seeding with \[password\]", tail)
    assert password is not None
    assert "\x07" not in tail
    assert "\u200b" not in tail


async def test_an_app_log_tail_never_rebuilds_the_password(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    folder = two_pages(tmp_path, LOGIN_MANIFEST)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    runner = FakeRunner(lambda copy: f"127.0.0.1:{closed}")
    seen: list[str] = []
    real_prepare = runner.prepare

    async def prepare(*arguments: Any) -> None:
        await real_prepare(*arguments)
        seen.append(arguments[-1]["FOOTFALL_PASSWORD"])
        runner.log_text = split_password(seen[0])

    runner.prepare = prepare  # type: ignore[method-assign,assignment]
    with pytest.raises(NotReadyError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path)
    assert_scrubbed(caught.value.log_tail, seen[0])


async def test_wait_ready_survives_an_endless_line() -> None:
    async def flood(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 " + b"x" * 70_000)
        with contextlib.suppress(ConnectionError):
            await writer.drain()
            await reader.read()
        writer.close()

    server = await asyncio.start_server(flood, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(TimeoutError):
            await wait_ready(f"127.0.0.1:{port}", "/healthz", 1)
    finally:
        server.close()


async def test_a_report_that_cannot_be_written_stops_nothing(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_two_pages(app_server)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    real_write = local._write_json

    def write(path: Path, data: object, password: str | None) -> None:
        runner.calls.append(("written", path.parent.name, path.name))
        if path.parent.name == "kenji":
            raise OSError(28, "No space left on device")
        real_write(path, data, password)

    monkeypatch.setattr(local, "_write_json", write)
    reports = await run_folder(
        two_pages(tmp_path), runner, connect[0], tmp_path, personas=CHROMIUM_PERSONAS
    )
    assert [(r.persona, r.status) for r in reports] == [
        ("grace", "finished"),
        ("kenji", "finished"),
    ]
    ops = [call[:2] for call in runner.calls]
    assert ops.index(("stop", ops[-1][1])) == len(ops) - 1
    assert ("written", "grace") in ops and ("written", "kenji") in ops


async def test_every_persona_ends_before_a_failure_is_raised(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_two_pages(app_server)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    real_persona = local._Run.persona

    async def persona(self: Any, who: Any, *rest: Any) -> Any:
        if who.id == "kenji":
            raise RuntimeError("kenji's part broke at once")
        report = await real_persona(self, who, *rest)
        runner.calls.append(("ended", who.id))
        return report

    monkeypatch.setattr(local._Run, "persona", persona)
    with pytest.raises(RuntimeError, match="broke at once"):
        await run_folder(
            two_pages(tmp_path), runner, connect[0], tmp_path, personas=CHROMIUM_PERSONAS
        )
    ops = runner.ops()
    assert ops[-2:] == ["ended", "stop"]
    grace = json.loads((tmp_path / "out" / "grace" / "report.json").read_text())
    assert grace["status"] == "finished"  # not cut short when its browser was closed
