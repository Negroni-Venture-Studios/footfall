"""`test_folder`: one run of a local folder, from the manifest to the results, with fakes.

The runner is a fake that records each request. Most tests start every app copy as the same
`tests/app_server.py`; the sign-in tests run the demo app itself, seeded and served on
127.0.0.1 as the runner would run it in containers. The browsers are local Chromium with name
lookups switched off, and the model is a `FakeModel` or a scripted tester.
"""

import asyncio
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from playwright.async_api import Browser, async_playwright

from footfall import local
from footfall.actions import MaskError
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
from footfall.personas import load_devices, load_personas, select
from footfall.runner.service import RunnerError
from footfall.tester import loop as tester_loop
from footfall.tester import tools
from footfall.tester.folder import Folder
from footfall.tester.loop import Budget, run_persona
from footfall.tester.model import ModelTurn
from footfall.tester.pricing import Usage
from footfall.tester.report import (
    OBSERVATIONS,
    Finding,
    PersonaReport,
    clean_text,
    strip_invisible,
)
from footfall.tester.screen import CaptureError, capture
from footfall.tester.tools import Executor
from tests.app_server import AppServer
from tests.conftest import MaskOutput

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
    untracked: bool = False,
    budget: Budget | None = None,
    out: Path | None = None,
) -> list[Any]:
    return await test_folder(
        folder,
        persona_ids=personas,
        brief_name=brief,
        focus=None,
        model_spec=model,
        out=out or tmp_path / "out",
        runner=runner,
        budget=budget or Budget(max_actions=8, max_seconds=120),
        connect_browser=connect_browser,
        workspaces=tmp_path / "workspaces",
        all_files=all_files,
        untracked=untracked,
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
    assert lines[4:] == ["", OBSERVATIONS]


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
        assert report["note"] == OBSERVATIONS
        assert report["persona"] == persona
        assert report["status"] == "finished"
        assert report["findings"][0]["title"] == "No way back from the second page"
        steps = (out / persona / "trace.jsonl").read_text().splitlines()
        assert [json.loads(step)["tool"] for step in steps] == ["go", "finish"]
        assert all(json.loads(step)["note"] == OBSERVATIONS for step in steps)
        shots = sorted(p.name for p in (out / persona / "screenshots").iterdir())
        assert shots == ["01.jpg", "02.jpg"]
    summary = json.loads((out / "summary.json").read_text())
    assert next(iter(summary.items())) == ("note", OBSERVATIONS)
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
    assert printed.splitlines()[-1] == OBSERVATIONS
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


async def forever(*arguments: Any, **options: Any) -> Any:
    """Stands in for a call to a browser container that keeps its connection and never answers."""
    await asyncio.Event().wait()


@pytest.mark.parametrize("part", ["open_context", "run_persona"])
async def test_a_persona_that_never_ends_is_stopped_at_its_hard_limit(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    part: str,
) -> None:
    serve_two_pages(app_server)
    monkeypatch.setattr(local, part, forever)
    monkeypatch.setattr(tester_loop, "MIN_TURN", 0.1)
    monkeypatch.setattr(local, "BROWSER_ALLOWANCE", 0.4)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    budget = Budget(max_seconds=0.5, grace_turns=0)  # a hard limit of 0.5 + 0.1 + 0.4 s
    started = time.monotonic()
    async with asyncio.timeout(15):  # a bound for this test, should the run keep none
        (report,) = await run_folder(
            two_pages(tmp_path), runner, connect[0], tmp_path, budget=budget
        )
    assert time.monotonic() - started < 10
    assert report.status == "failed"
    assert report.reason == (
        "the persona ran past its hard limit of 1 s, so Footfall stopped it; "
        "its actions and cost are not counted"
    )
    written = json.loads((tmp_path / "out" / "grace" / "report.json").read_text())
    assert written["status"] == "failed"
    assert runner.ops()[-1] == "stop"


async def test_a_context_that_never_closes_closes_the_connection_to_the_browsers(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_two_pages(app_server)
    real_open = open_context
    browsers: list[Browser] = []

    async def never_closes(browser: Browser, *rest: Any) -> Any:
        context = await real_open(browser, *rest)
        browsers.append(browser)
        monkeypatch.setattr(context, "close", forever)
        return context

    monkeypatch.setattr(local, "open_context", never_closes)
    monkeypatch.setattr(local, "CLOSE_TIMEOUT", 0.5)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    started = time.monotonic()
    async with asyncio.timeout(15):  # a bound for this test, should the close keep none
        (report,) = await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert time.monotonic() - started < 10
    assert report.status == "finished"
    assert (tmp_path / "out" / "grace" / "report.json").exists()
    (browser,) = browsers
    assert not browser.is_connected()
    assert runner.ops()[-1] == "stop"


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


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("summary.json", "dangling"),
        ("summary.json", "file"),
        ("grace", "dangling"),
        ("grace", "folder"),
    ],
)
async def test_a_link_where_results_go_is_refused_before_the_run(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]], name: str, kind: str
) -> None:
    # A tested branch can track a link where the results go, when they go inside its folder.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "planted-by-the-branch"
    if kind == "file":
        target.write_text("left alone")
    elif kind == "folder":
        target.mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / name).symlink_to(target)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(OutputError, match="is a link"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert runner.calls == []
    if kind == "dangling":
        assert list(elsewhere.iterdir()) == []
    elif kind == "file":
        assert target.read_text() == "left alone"
    else:
        assert list(target.iterdir()) == []


async def test_a_results_folder_that_is_a_link_is_refused_before_the_run(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    target = tmp_path / "elsewhere" / "planted-by-the-branch"
    target.mkdir(parents=True)
    (tmp_path / "out").symlink_to(target)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(OutputError, match="is a link"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert runner.calls == []
    assert list(target.iterdir()) == []


@pytest.mark.usefixtures("no_umask")
def test_results_are_made_for_their_owner_alone(tmp_path: Path) -> None:
    (grace,) = select(load_personas(load_devices()), ["grace"])
    out = tmp_path / "new" / "out"
    results = local._prepare_results(out, [grace], two_pages(tmp_path))
    try:
        local._write_json(results.out, "summary.json", {"run": "r"}, None)
    finally:
        results.close()
    made = (tmp_path / "new", out, out / "grace", out / "summary.json")
    modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in made}
    assert modes == {"new": 0o700, "out": 0o700, "grace": 0o700, "summary.json": 0o600}


async def test_a_link_above_the_results_inside_the_tested_folder_is_refused(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # The tested branch tracks a link where a folder above the results would be.
    folder = two_pages(tmp_path)
    target = tmp_path / "elsewhere" / "planted-by-the-branch"
    target.mkdir(parents=True)
    (folder / "results").symlink_to(target)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(OutputError, match="is a link"):
        await run_folder(folder, runner, connect[0], tmp_path, out=folder / "results" / "run")
    assert runner.calls == []
    assert list(target.iterdir()) == []


def test_a_link_above_the_results_outside_the_tested_folder_is_followed(tmp_path: Path) -> None:
    # The operator's own links, such as /var or /tmp on a Mac, or a home folder that is a
    # link, lie outside the folder under test.
    real = tmp_path / "real-results-parent"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    (grace,) = select(load_personas(load_devices()), ["grace"])
    out = tmp_path / "linked" / "deeper" / "out"
    local._prepare_results(out, [grace], two_pages(tmp_path)).close()
    assert (real / "deeper" / "out" / "grace").is_dir()


async def test_a_link_swapped_in_after_the_check_moves_no_result(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
) -> None:
    serve_two_pages(app_server)
    out = tmp_path / "out"
    targets = [tmp_path / "elsewhere" / name for name in ("planted-folder", "planted-out")]
    for target in targets:
        target.mkdir(parents=True)
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    real_prepare = runner.prepare

    async def prepare(*arguments: Any) -> None:
        # Once Footfall has made the results folders, links take the place of both.
        (out / "grace").rename(tmp_path / "moved-grace")
        (out / "grace").symlink_to(targets[0])
        out.rename(tmp_path / "moved-out")
        out.symlink_to(targets[1])
        await real_prepare(*arguments)

    runner.prepare = prepare  # type: ignore[method-assign,assignment]
    (report,) = await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path)
    assert report.status == "finished"
    assert [list(target.iterdir()) for target in targets] == [[], []]
    assert sorted(p.name for p in (tmp_path / "moved-out").iterdir()) == ["grace", "summary.json"]
    moved = sorted(p.name for p in (tmp_path / "moved-grace").iterdir())
    assert moved == ["report.json", "screenshots", "trace.jsonl"]


def test_a_results_file_is_never_written_through_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "planted-by-the-branch.json"
    target.parent.mkdir()
    (tmp_path / "report.json").symlink_to(target)
    folder = Folder.at(tmp_path)
    try:
        with pytest.raises(FileExistsError):
            local._write_json(folder, "report.json", {"summary": "s"}, None)
    finally:
        folder.close()
    assert not target.exists()


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


def git_output(folder: Path, *arguments: str) -> str:
    done = subprocess.run(
        [GIT, "-C", str(folder), *arguments], check=True, capture_output=True, text=True
    )
    return done.stdout


@pytest.mark.parametrize("untracked", [False, True])
async def test_only_the_files_git_tracks_are_copied(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    untracked: bool,
) -> None:
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    (folder / ".gitignore").write_text(".env\nnode_modules/\n")
    (folder / ".env").write_text("API_KEY=sk-not-a-real-key\n")  # gitleaks:allow
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "left-pad.js").write_text("module.exports = 1;\n")
    (folder / "link").symlink_to("serve.py")
    (folder / "gone.txt").write_text("tracked, then deleted\n")
    git(folder, "add", ".")
    (folder / "gone.txt").unlink()
    (folder / "notes.txt").write_text("not committed yet, and not ignored\n")
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(
        folder, runner, connect[0], tmp_path, all_files=False, untracked=untracked
    )
    assert [r.status for r in reports] == ["finished"]
    expected = [".gitignore", "footfall.toml", "footfall/briefs/default.md", "link -> serve.py"]
    expected += ["notes.txt", "serve.py"] if untracked else ["serve.py"]
    assert runner.workspace_files == expected


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
        untracked=True,  # the results are untracked and not ignored, so git lists them
    )
    assert runner.workspace_files == ["footfall.toml", "footfall/briefs/default.md", "serve.py"]


F01_SECRET = ".env.f01-secret"
F01_PLAIN = "f01-notes-untracked.txt"


def tracked_two_pages(tmp_path: Path) -> Path:
    """The two-page app in a repository that tracks every file of it."""
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    git(folder, "add", ".")
    return folder


async def run_to_prepare(
    folder: Path, connect: Connect, tmp_path: Path, *, untracked: bool
) -> FakeRunner:
    """Run the folder in git's mode as far as preparing the app, which stops it there, with
    no browser needed, and return the runner, which saw the workspace."""
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    runner.fail["prepare"] = RunnerError("f01-stop-marker")
    with pytest.raises(RunnerError, match="f01-stop-marker"):
        await run_folder(folder, runner, connect, tmp_path, all_files=False, untracked=untracked)
    return runner


@pytest.mark.parametrize("untracked", [False, True])
async def test_a_file_the_tested_tree_un_ignores_is_copied_only_with_untracked(
    tmp_path: Path,
    connect: tuple[Connect, list[tuple[str, str]]],
    capsys: pytest.CaptureFixture[str],
    untracked: bool,
) -> None:
    """The audit's case: the developer's own exclude file keeps a secret out of git, and the
    branch under test commits a negation, which git ranks above that file."""
    folder = tracked_two_pages(tmp_path)
    (folder / ".git" / "info").mkdir(exist_ok=True)
    (folder / ".git" / "info" / "exclude").write_text(f"{F01_SECRET}\n")
    (folder / ".gitignore").write_text(f"!{F01_SECRET}\n")
    git(folder, "add", ".gitignore")
    (folder / F01_SECRET).write_text("F01_SECRET_MARKER_3b7e\n")
    # The control: git now counts the secret as untracked and not ignored.
    listed = git_output(folder, "ls-files", "--others", "--exclude-standard")
    assert listed.splitlines() == [F01_SECRET]
    runner = await run_to_prepare(folder, connect[0], tmp_path, untracked=untracked)
    printed = capsys.readouterr().err
    assert (F01_SECRET in runner.workspace_files) is untracked
    assert (f'"{F01_SECRET}"' in printed) is untracked
    assert {".gitignore", "footfall.toml", "serve.py"} <= set(runner.workspace_files)


@pytest.mark.parametrize("untracked", [False, True])
async def test_an_untracked_file_is_copied_and_named_only_with_untracked(
    tmp_path: Path,
    connect: tuple[Connect, list[tuple[str, str]]],
    capsys: pytest.CaptureFixture[str],
    untracked: bool,
) -> None:
    folder = tracked_two_pages(tmp_path)
    (folder / F01_PLAIN).write_text("F01_PLAIN_MARKER_8c21\n")
    runner = await run_to_prepare(folder, connect[0], tmp_path, untracked=untracked)
    printed = capsys.readouterr().err
    assert (F01_PLAIN in runner.workspace_files) is untracked
    assert (f'"{F01_PLAIN}"' in printed) is untracked
    assert {"footfall.toml", "footfall/briefs/default.md", "serve.py"} <= set(
        runner.workspace_files
    )


async def test_untracked_and_all_files_cannot_both_be_asked_for(tmp_path: Path) -> None:
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(ValueError, match="untracked and all_files"):
        await test_folder(
            two_pages(tmp_path),
            persona_ids=["grace"],
            brief_name="default",
            focus=None,
            model_spec=FINISHES,
            out=tmp_path / "out",
            runner=runner,
            budget=Budget(max_actions=8, max_seconds=120),
            workspaces=tmp_path / "workspaces",
            all_files=True,
            untracked=True,
        )
    assert runner.calls == []


async def test_untracked_names_are_printed_cleaned(
    tmp_path: Path,
    connect: tuple[Connect, list[tuple[str, str]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = tracked_two_pages(tmp_path)
    name = "f01-\x1b]0;title\x07name\nfootfall test: forged ##[add-mask]x"
    (folder / name).write_text("F01_ODD_NAME_MARKER\n")
    runner = await run_to_prepare(folder, connect[0], tmp_path, untracked=True)
    printed = capsys.readouterr().err
    assert name in runner.workspace_files
    assert "\x1b" not in printed
    assert "\x07" not in printed
    assert "##[" not in printed
    assert "f01-]0;titlename footfall test: forged ## [add-mask]x" in printed
    assert not [line for line in printed.splitlines() if line.startswith("footfall test: forged")]


@pytest.mark.parametrize(
    ("ignored", "label"),
    [("footfall/briefs/default.md", "default.md"), ("footfall.toml", "footfall.toml")],
)
async def test_a_manifest_or_brief_git_does_not_list_is_refused(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]], ignored: str, label: str
) -> None:
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    (folder / ".gitignore").write_text(f"{ignored}\n")
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(ManifestError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False, untracked=True)
    assert caught.value.problems == [
        f"{label}: is not among the files the run copies, which git tracks or, with "
        "--untracked, does not ignore"
    ]
    assert runner.calls == []
    assert not (tmp_path / "workspaces").exists()
    assert not (tmp_path / "out").exists()


async def test_an_untracked_brief_is_refused_by_default(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    git(folder, "add", "footfall.toml", "serve.py")
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(ManifestError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False)
    assert caught.value.problems == [
        "default.md: is not among the files the run copies, which git tracks or, with "
        "--untracked, does not ignore"
    ]
    assert runner.calls == []


@pytest.mark.parametrize("mode", ["tracked", "untracked", "all files"])
async def test_a_listed_brief_or_one_copied_with_all_files_goes_ahead(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]], mode: str
) -> None:
    """The positive control: the run gets as far as preparing the app, which stops it here,
    with no browser needed. An untracked brief is copied with --untracked, and an ignored
    one with --all-files, so each is read."""
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    if mode == "all files":
        (folder / ".gitignore").write_text("footfall/briefs/default.md\n")
    git(folder, "add", "footfall.toml", "serve.py")
    if mode == "tracked":
        git(folder, "add", "footfall")
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    runner.fail["prepare"] = RunnerError("f07-stop-marker")
    with pytest.raises(RunnerError, match="f07-stop-marker"):
        await run_folder(
            folder,
            runner,
            connect[0],
            tmp_path,
            all_files=mode == "all files",
            untracked=mode == "untracked",
        )
    assert runner.ops() == ["prepare", "stop"]
    assert "footfall/briefs/default.md" in runner.workspace_files


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

    passwords: list[str] = []

    class SeedFails(FakeRunner):
        async def prepare(self, run_id: str, workspace: Path, *rest: Any) -> None:
            passwords.append(rest[-1]["FOOTFALL_PASSWORD"])
            raise RunnerError("seed failed with exit code 1", split_password(passwords[0]))

    runner = SeedFails(lambda copy: "127.0.0.1:9")
    with pytest.raises(RunnerError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path)
    tail = caught.value.log_tail
    assert_scrubbed(tail, passwords[0])
    assert "seeding with [password]" in tail
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

    def write(folder: Folder, name: str, data: object, password: str | None) -> None:
        runner.calls.append(("written", folder.path.name, name))
        if folder.path.name == "kenji":
            raise OSError(28, "No space left on device")
        real_write(folder, name, data, password)

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


# ---------------------------------------------------------------- copying safely


async def test_a_link_in_place_of_a_tracked_folder_is_never_followed(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # keys/ was committed, then replaced by an ignored link to a folder outside. git still
    # lists keys/id_key from its index; the copy must not read it through the link.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_key").write_text("PRIVATE KEY THAT MUST STAY OUT\n")
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    (folder / "keys").mkdir()
    (folder / "keys" / "id_key").write_text("a key that was committed\n")
    git(folder, "add", "footfall.toml", "footfall", "keys/id_key")
    shutil.rmtree(folder / "keys")
    (folder / "keys").symlink_to(outside)
    (folder / ".gitignore").write_text("keys\n")
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError, match="keys"):
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False)
    assert "prepare" not in runner.ops()
    assert list((tmp_path / "workspaces").iterdir()) == []


async def test_a_crafted_index_cannot_reach_through_a_link(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # The index names keys/id_key without the file ever being in the folder, and keys is a
    # link that is not ignored, so it is listed and copied before keys/id_key.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_key").write_text("PRIVATE KEY THAT MUST STAY OUT\n")
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    blob = (
        subprocess.run(
            [GIT, "-C", str(folder), "hash-object", "-w", "--stdin"],
            input=b"anything\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git(folder, "update-index", "--add", "--cacheinfo", f"100644,{blob},keys/id_key")
    (folder / "keys").symlink_to(outside)
    copied: list[Path] = []
    real_prepare = FakeRunner.prepare

    class Recording(FakeRunner):
        async def prepare(self, run_id: str, workspace: Path, *rest: Any) -> None:
            copied.extend(workspace.rglob("*"))
            await real_prepare(self, run_id, workspace, *rest)

    runner = Recording(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError, match="keys"):
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False, untracked=True)
    assert copied == []
    assert "prepare" not in runner.ops()


def test_the_copy_never_writes_through_a_link_in_the_workspace(tmp_path: Path) -> None:
    # A link already in the workspace, where a folder should be, is never written through.
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keys").symlink_to(outside)
    folder = tmp_path / "source"
    (folder / "keys").mkdir(parents=True)
    (folder / "keys" / "x").write_text("data\n")
    source = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    target = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(LocalRunError, match="keys"):
            local._copy_name(source, target, PurePosixPath("keys/x"))
    finally:
        os.close(source)
        os.close(target)
    assert list(outside.iterdir()) == []


async def test_a_nested_repository_is_refused_by_name(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    nested = folder / "vendor" / "widget"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "widget.js").write_text("export default 1;\n")
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False, untracked=True)
    assert "vendor/widget" in str(caught.value)
    assert "--all-files" in str(caught.value)
    assert "prepare" not in runner.ops()


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired(["git"], 60), FileNotFoundError(2, "No such file", "git")],
)
async def test_git_that_hangs_or_cannot_start_is_reported(
    tmp_path: Path,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def run(*arguments: Any, **options: Any) -> Any:
        raise failure

    monkeypatch.setattr(subprocess, "run", run)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError, match="--all-files"):
        await run_folder(two_pages(tmp_path), runner, connect[0], tmp_path, all_files=False)
    assert runner.calls == []


# ---------------------------------------------------------------- a hostile repository


def hostile_repository(folder: Path, marker: Path) -> None:
    """A repository whose listing would fetch a missing .gitignore lazily, through a promisor
    remote whose upload-pack command is a shell command of the repository's choosing."""
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "index.html").write_text("hi\n")
    git(folder, "add", "index.html")
    missing = "1234567890123456789012345678901234567890"
    git(folder, "update-index", "--add", "--cacheinfo", f"100644,{missing},.gitignore")
    git(folder, "update-index", "--skip-worktree", ".gitignore")
    git(folder, "config", "core.repositoryformatversion", "1")
    git(folder, "config", "extensions.partialClone", "origin")
    git(folder, "config", "remote.origin.url", str(folder))
    git(folder, "config", "remote.origin.promisor", "true")
    git(folder, "config", "remote.origin.uploadpack", f"touch {marker}; git-upload-pack")
    (folder / "notes.txt").write_text("untracked\n")


def test_a_hostile_repository_makes_git_run_its_command_without_footfalls_settings(
    tmp_path: Path,
) -> None:
    """The positive control: without the settings Footfall gives git, listing the hostile
    repository runs its command, so the test that Footfall's listing does not can fail."""
    marker = tmp_path / "control-marker"
    folder = tmp_path / "hostile"
    hostile_repository(folder, marker)
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    subprocess.run(
        [GIT, "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=folder,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert marker.exists()


def test_a_repository_cannot_make_git_run_a_command(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    folder = tmp_path / "hostile"
    hostile_repository(folder, marker)
    try:
        listing = local._git_files(folder, untracked=True)  # the listing that reads ignores
    except LocalRunError as error:
        # Some versions of git refuse the folder rather than read it without the object.
        assert "git cannot read the files" in str(error)
    else:
        assert PurePosixPath("notes.txt") in listing.names  # git ran, and read the folder
    assert not marker.exists()


def test_git_runs_with_a_bare_environment_and_no_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")  # gitleaks:allow
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    monkeypatch.setenv("SOMETHING_ELSE", "not for git")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    real_run = subprocess.run

    def run(command: list[str], **options: Any) -> Any:
        calls.append((command, options))
        return real_run(command, **options)

    monkeypatch.setattr(subprocess, "run", run)
    listing = local._git_files(folder, untracked=True)
    assert PurePosixPath("footfall.toml") in listing.names
    # ls-files twice, then rev-parse for the git folder, the common one and a superproject,
    # each run the same safe way.
    assert len(calls) == 5
    assert "--show-superproject-working-tree" in calls[-1][0]
    allowed = {"PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL"}
    for command, options in calls:
        assert not any("safe.directory" in part for part in command)
        assert options["stdin"] is subprocess.DEVNULL
        assert options["start_new_session"] is True  # no terminal of the developer's to read
        env = options["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        assert env["GIT_ALLOW_PROTOCOL"] == ""
        assert env["LANG"] == "en_GB.UTF-8"
        assert set(env) - allowed == {
            "GIT_TERMINAL_PROMPT",
            "GIT_OPTIONAL_LOCKS",
            "GIT_NO_LAZY_FETCH",
            "GIT_ALLOW_PROTOCOL",
        }


def craft_index(path: Path, names: Sequence[str]) -> None:
    """Write a git index by hand, naming paths git itself would never add."""
    empty = bytes.fromhex("e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
    entries = b""
    for name in sorted(n.encode() for n in names):
        entry = struct.pack(">10I", 0, 0, 0, 0, 0, 0, 0o100644, 0, 0, 0) + empty
        entry += struct.pack(">H", min(len(name), 0xFFF)) + name
        entry += b"\0" * (8 - len(entry) % 8)
        entries += entry
    body = b"DIRC" + struct.pack(">II", 2, len(names)) + entries
    path.write_bytes(body + hashlib.sha1(body, usedforsecurity=False).digest())


@pytest.mark.parametrize("spelling", [".git/config", ".GIT/config", ".g\u200cit/config"])
async def test_a_crafted_index_cannot_copy_the_git_folder(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]], spelling: str
) -> None:
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    git(folder, "config", "remote.origin.url", "https://token-not-real@example.com/app.git")
    names = [spelling, "footfall.toml", "footfall/briefs/default.md"]
    craft_index(folder / ".git" / "index", names)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError, match="git folder"):
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False)
    assert "prepare" not in runner.ops()


def test_the_git_folder_is_refused_whatever_it_is_called(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    (folder / "meta").mkdir(parents=True)
    (folder / "meta" / "config").write_text("url = https://token-not-real@example.com\n")
    info = (folder / "meta").stat()
    with pytest.raises(LocalRunError, match="git folder"):
        local._copy_files(
            folder,
            tmp_path / "workspace",
            [PurePosixPath("meta/config")],
            tmp_path / "out",
            git_dirs=frozenset({(info.st_dev, info.st_ino)}),
        )
    assert not (tmp_path / "workspace" / "meta").exists()


async def test_names_in_errors_are_quoted(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    nested = folder / "evil\x1b[2Jname"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "x.js").write_text("1\n")
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError) as caught:
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False, untracked=True)
    message = str(caught.value)
    assert "\x1b" not in message
    assert '"evil[2Jname"' in message
    assert "tracked file now replaced by a folder" in message


def test_a_folder_that_cannot_be_opened_is_not_called_a_link(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    (folder / "locked").mkdir(parents=True)
    (folder / "locked" / "x").write_text("data\n")
    (folder / "locked").chmod(0)
    try:
        with pytest.raises(LocalRunError) as caught:
            local._copy_files(
                folder, tmp_path / "workspace", [PurePosixPath("locked/x")], tmp_path / "out"
            )
    finally:
        (folder / "locked").chmod(0o755)
    assert 'cannot open "locked" (Permission denied)' in str(caught.value)
    assert "link" not in str(caught.value)


def test_the_workspace_must_be_new(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    folder.mkdir()
    (tmp_path / "workspace").mkdir()
    with pytest.raises(FileExistsError):
        local._copy_files(folder, tmp_path / "workspace", [], tmp_path / "out")


# ---------------------------------------------------------------- git folders in other places

TOKEN_URL = "https://token-not-real@example.com/app.git"


def app_files(folder: Path) -> None:
    """The two-page app's own files, written into an existing folder."""
    (folder / "footfall" / "briefs").mkdir(parents=True)
    (folder / "footfall.toml").write_text(MANIFEST)
    (folder / "footfall" / "briefs" / "default.md").write_text("Look around both pages.\n")
    (folder / "serve.py").write_text("print('not run in these tests')\n")


def moved_git_folder(folder: Path, name: str) -> Path:
    """A repository whose git folder is `name`, reached through a .git file."""
    git(folder, "init", "-q")
    moved = folder / name
    (folder / ".git").rename(moved)
    (folder / ".git").write_text(f"gitdir: {name}\n")
    git(folder, "config", "remote.origin.url", TOKEN_URL)
    return moved


async def refused_without_the_token(
    folder: Path, tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    copied: list[bytes] = []
    real_prepare = FakeRunner.prepare

    class Recording(FakeRunner):
        async def prepare(self, run_id: str, workspace: Path, *rest: Any) -> None:
            copied.extend(p.read_bytes() for p in workspace.rglob("*") if p.is_file())
            await real_prepare(self, run_id, workspace, *rest)

    runner = Recording(lambda copy: "127.0.0.1:9")
    with pytest.raises(LocalRunError, match="git folder"):
        await run_folder(folder, runner, connect[0], tmp_path, all_files=False, untracked=True)
    assert "prepare" not in runner.ops()
    assert not any(b"token-not-real" in data for data in copied)


async def test_a_git_folder_reached_through_a_git_file_is_never_copied(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    folder = two_pages(tmp_path)
    moved_git_folder(folder, "meta")
    await refused_without_the_token(folder, tmp_path, connect)


async def test_a_git_folder_whose_name_ends_in_a_space_is_never_copied(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # git keeps the space in "gitdir: meta ", and a decoy called meta stands beside it.
    folder = two_pages(tmp_path)
    moved = moved_git_folder(folder, "meta ")
    (folder / "meta").mkdir()
    craft_index(moved / "index", ["footfall.toml", "meta /config"])
    await refused_without_the_token(folder, tmp_path, connect)


async def test_a_linked_worktrees_common_folder_is_never_copied(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # The git folder is meta/worktrees/w, but the configuration is in meta, its common folder.
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    (folder / ".git").rename(folder / "meta")
    worktree = folder / "meta" / "worktrees" / "w"
    worktree.mkdir(parents=True)
    (worktree / "HEAD").write_text("ref: refs/heads/main\n")
    (worktree / "commondir").write_text("../..\n")
    (worktree / "gitdir").write_text(f"{folder / '.git'}\n")
    (folder / ".git").write_text("gitdir: meta/worktrees/w\n")
    (folder / ".gitignore").write_text("/meta/worktrees/\n")
    git(folder, "config", "remote.origin.url", TOKEN_URL)
    await refused_without_the_token(folder, tmp_path, connect)


async def test_a_folder_that_is_itself_the_git_folder_is_never_copied(
    tmp_path: Path, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # Tested from inside the git folder, whose core.worktree is its parent: its config and
    # HEAD are listed at the top, where no folder above them is opened.
    top = tmp_path / "top"
    top.mkdir()
    git(top, "init", "-q")
    folder = top / "app"
    (top / ".git").rename(folder)
    subprocess.run([GIT, "--git-dir", str(folder), "config", "core.worktree", ".."], check=True)
    subprocess.run(
        [GIT, "--git-dir", str(folder), "config", "remote.origin.url", TOKEN_URL], check=True
    )
    app_files(folder)
    await refused_without_the_token(folder, tmp_path, connect)


async def test_all_files_never_copies_the_git_folder(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    git(folder, "config", "remote.origin.url", TOKEN_URL)
    other = tmp_path / "other"
    other.mkdir()
    moved_git_folder(other, "meta")
    (folder / "vendored").symlink_to(other)  # a link is copied as a link, never followed
    nested = folder / "lib"
    nested.mkdir()
    moved_git_folder(nested, "meta")  # a moved git folder, which only git can name
    runner = FakeRunner(lambda copy: f"127.0.0.1:{app_server.port}")
    await run_folder(folder, runner, connect[0], tmp_path, all_files=True)
    assert not any(".git" in name.split("/") for name in runner.workspace_files)
    assert "lib/.git" not in runner.workspace_files
    assert not any(name.startswith(".git") for name in runner.workspace_files)


# ---------------------------------------------------------------- folders shaped like git folders


class Copying(FakeRunner):
    """Records every file the workspace holds when the runner is asked to prepare it."""

    def __init__(self, address: Callable[[str], str]) -> None:
        super().__init__(address)
        self.copied: list[bytes] = []

    async def prepare(self, run_id: str, workspace: Path, *rest: Any) -> None:
        self.copied.extend(p.read_bytes() for p in workspace.rglob("*") if p.is_file())
        await super().prepare(run_id, workspace, *rest)


@pytest.mark.parametrize("git_says", ["what it can", "nothing", "nothing it can read"])
async def test_all_files_leaves_out_every_folder_shaped_like_a_git_folder(
    tmp_path: Path,
    app_server: AppServer,
    connect: tuple[Connect, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    git_says: str,
) -> None:
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    outer = moved_git_folder(folder, "outer-meta")  # the repository's own, behind a .git file
    nested = folder / "lib"
    nested.mkdir()
    moved_git_folder(nested, "meta")  # a nested repository's, which the outer git never names
    if git_says == "nothing":
        monkeypatch.setattr(local, "_known_git_folders", lambda folder: frozenset())
    if git_says == "nothing it can read":
        with (outer / "config").open("a") as config:
            config.write("[core]\n\trepositoryformatversion = 99\n")
    runner = Copying(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(folder, runner, connect[0], tmp_path, all_files=True)
    assert [r.status for r in reports] == ["finished"]
    assert not any(b"token-not-real" in data for data in runner.copied)
    files = runner.workspace_files
    assert not any(name.startswith(("lib/meta", "outer-meta")) for name in files), files
    assert "serve.py" in files


def outer_with_a_nested_repository(tmp_path: Path) -> Path:
    """An outer repository with a nested one whose git folder sits beside it, outside the
    nested folder: sub/.git names ../submeta, sub/ is ignored, and submeta/ is not."""
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    sub = folder / "sub"
    sub.mkdir()
    git(sub, "init", "-q")
    (sub / ".git").rename(folder / "submeta")
    (sub / ".git").write_text("gitdir: ../submeta\n")
    git(sub, "config", "remote.origin.url", TOKEN_URL)
    (folder / ".gitignore").write_text("sub/\n")
    return folder


async def test_an_untracked_git_folder_beside_its_worktree_is_left_out(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    serve_two_pages(app_server)
    folder = outer_with_a_nested_repository(tmp_path)
    runner = Copying(lambda copy: f"127.0.0.1:{app_server.port}")
    reports = await run_folder(
        folder, runner, connect[0], tmp_path, all_files=False, untracked=True
    )
    assert [r.status for r in reports] == ["finished"]
    assert not any(b"token-not-real" in data for data in runner.copied)
    assert not any(name.startswith("submeta") for name in runner.workspace_files)
    assert "serve.py" in runner.workspace_files


async def test_a_tracked_git_fixture_is_still_copied(
    tmp_path: Path, app_server: AppServer, connect: tuple[Connect, list[tuple[str, str]]]
) -> None:
    # A repository may commit a git folder as a test fixture; any clone would hold it.
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    git(folder, "init", "-q")
    fixture = folder / "fixtures" / "repo"
    (fixture / "objects").mkdir(parents=True)
    (fixture / "HEAD").write_text("ref: refs/heads/main\n")
    (fixture / "config").write_text("[core]\n\tbare = true\n")
    (fixture / "objects" / "README").write_text("empty\n")
    git(folder, "add", "fixtures")
    (fixture / "untracked-note").write_text("not committed\n")
    runner = Copying(lambda copy: f"127.0.0.1:{app_server.port}")
    await run_folder(folder, runner, connect[0], tmp_path, all_files=False, untracked=True)
    files = runner.workspace_files
    assert "fixtures/repo/HEAD" in files
    assert "fixtures/repo/config" in files
    assert "fixtures/repo/objects/README" in files
    assert "fixtures/repo/untracked-note" not in files  # untracked, inside a git-shaped folder


# ---------------------------------------------------------------- GitHub Actions


async def test_in_github_actions_the_password_is_masked_before_it_is_used(
    tmp_path: Path, mask_output: MaskOutput
) -> None:
    folder = two_pages(tmp_path, LOGIN_MANIFEST)
    printed: list[str] = []

    class SeedFails(FakeRunner):
        async def prepare(self, run_id: str, workspace: Path, *rest: Any) -> None:
            # The first use of the password: by now the mask must be in the log already.
            printed.append(mask_output.read())
            self.envs.append(dict(rest[-1]))
            raise RunnerError("seed failed with exit code 1")

    async def unreachable(engine: str, endpoint: str) -> Browser:
        raise BrowserConnectError("never reached")

    runner = SeedFails(lambda copy: "127.0.0.1:9")
    with pytest.raises(RunnerError):
        await run_folder(folder, runner, unreachable, tmp_path)
    password = runner.envs[0]["FOOTFALL_PASSWORD"]
    assert f"::add-mask::{password}\n" in printed[0]


async def test_outside_github_actions_nothing_is_masked(
    tmp_path: Path, mask_output: MaskOutput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    folder = two_pages(tmp_path, LOGIN_MANIFEST)
    runner = FakeRunner(lambda copy: "127.0.0.1:9")
    runner.fail["prepare"] = RunnerError("seed failed with exit code 1")

    async def unreachable(engine: str, endpoint: str) -> Browser:
        raise BrowserConnectError("never reached")

    with pytest.raises(RunnerError):
        await run_folder(folder, runner, unreachable, tmp_path)
    assert mask_output.read() == ""


# ---------------------------------------------------------------- git folders, cleanup pass


@pytest.mark.parametrize(
    ("head", "objects", "commondir"),
    [
        ("file", "folder", None),
        ("link", "folder", None),  # git -c core.preferSymlinkRefs=true makes HEAD a link
        ("file", "link", None),
        ("link", "link", None),
        (None, None, "file"),
        (None, None, "link"),
    ],
)
def test_a_git_folder_is_known_by_its_shape_whether_its_parts_are_links(
    tmp_path: Path, head: str | None, objects: str | None, commondir: str | None
) -> None:
    meta = tmp_path / "meta"
    (meta / "refs" / "heads").mkdir(parents=True)
    (meta / "refs" / "heads" / "main").write_text("0" * 40 + "\n")
    (meta / "store").mkdir()
    (meta / "common.txt").write_text("../other\n")
    for name, kind, target in (
        ("HEAD", head, "refs/heads/main"),
        ("objects", objects, "store"),
        ("commondir", commondir, "common.txt"),
    ):
        if kind == "link":
            (meta / name).symlink_to(target)
        elif kind == "file":
            (meta / name).write_text("ref: refs/heads/main\n")
        elif kind == "folder":
            (meta / name).mkdir()
    assert local._shaped_like_a_git_folder(lambda name: local._kind(str(meta / name)))


def test_an_ordinary_folder_is_not_shaped_like_a_git_folder(tmp_path: Path) -> None:
    # Any link counts as `objects`, even a dangling one, so the folder is not git-shaped only
    # because it has no HEAD: HEAD.md is not HEAD.
    (tmp_path / "objects").symlink_to("nowhere")
    (tmp_path / "HEAD.md").write_text("a heading\n")
    assert not local._shaped_like_a_git_folder(lambda name: local._kind(str(tmp_path / name)))


def test_a_head_beside_a_dangling_objects_link_is_shaped_like_a_git_folder(tmp_path: Path) -> None:
    (tmp_path / "objects").symlink_to("nowhere")
    (tmp_path / "HEAD").write_text("ref: refs/heads/main\n")
    assert local._shaped_like_a_git_folder(lambda name: local._kind(str(tmp_path / name)))


def shaped_folder(folder: Path, *names: str) -> Path:
    """A folder in the app shaped like a git folder, with the files named in it."""
    folder.mkdir(parents=True)
    (folder / "HEAD").write_text("ref: refs/heads/main\n")
    (folder / "objects").mkdir()
    for name in names:
        (folder / name).write_text(f"{name}\n")
    return folder


def test_all_files_warns_once_for_each_folder_shaped_like_a_git_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    shaped_folder(folder / "vendor" / "meta", "a", "b", "c")
    shaped_folder(folder / "lib" / "cache", "d")
    with caplog.at_level("WARNING", logger="footfall.local"):
        local._copy_folder(folder, tmp_path / "workspace", tmp_path / "out")
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2, warnings
    assert any("vendor/meta" in w for w in warnings) and any("lib/cache" in w for w in warnings)
    assert all("shaped like a git folder" in w for w in warnings)
    assert not (tmp_path / "workspace" / "vendor" / "meta").exists()


def test_git_only_copy_warns_once_for_each_folder_it_leaves_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    shaped_folder(folder / "vendor" / "meta", "a", "b", "c")
    names = [PurePosixPath("serve.py"), *(PurePosixPath(f"vendor/meta/{n}") for n in "abc")]
    with caplog.at_level("WARNING", logger="footfall.local"):
        local._copy_files(
            folder,
            tmp_path / "workspace",
            names,
            tmp_path / "out",
            untracked=frozenset(names[1:]),
        )
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "vendor/meta" in warnings[0] and "shaped like a git folder" in warnings[0]
    assert (tmp_path / "workspace" / "serve.py").exists()
    assert not (tmp_path / "workspace" / "vendor").exists()


@pytest.mark.parametrize("tracked", [True, False])
@pytest.mark.parametrize("config", ["config", "config.worktree"])
def test_a_hard_link_to_the_git_configuration_is_refused(
    tmp_path: Path, config: str, tracked: bool
) -> None:
    """A hostile archive, once extracted, can leave a hard link to .git/config in the
    worktree, under any name, and an index that tracks it: it is the same file, token and
    all, so it is never copied."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    git(folder, "config", "remote.origin.url", TOKEN_URL)
    if config == "config.worktree":
        (folder / ".git" / "config.worktree").write_text(f"[remote]\n\turl = {TOKEN_URL}\n")
    os.link(folder / ".git" / config, folder / "settings.ini")
    if tracked:
        git(folder, "add", "settings.ini")
    listing = local._git_files(folder, untracked=not tracked)
    assert PurePosixPath("settings.ini") in listing.names
    with pytest.raises(LocalRunError, match="git configuration"):
        local._copy_files(
            folder,
            tmp_path / "git-only",
            listing.names,
            tmp_path / "out",
            git_dirs=listing.git_dirs,
            untracked=listing.untracked,
        )
    with pytest.raises(LocalRunError, match="git configuration"):
        local._copy_folder(
            folder, tmp_path / "all-files", tmp_path / "out", local._known_git_folders(folder)
        )


def test_a_copy_of_the_configuration_that_is_not_the_same_file_is_copied(tmp_path: Path) -> None:
    """Only the file itself is refused; a copy of its text is the app's own business."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    shutil.copy(folder / ".git" / "config", folder / "settings.ini")
    listing = local._git_files(folder, untracked=True)
    local._copy_files(
        folder,
        tmp_path / "workspace",
        listing.names,
        tmp_path / "out",
        git_dirs=listing.git_dirs,
        untracked=listing.untracked,
    )
    assert (tmp_path / "workspace" / "settings.ini").exists()


@pytest.mark.parametrize(
    "inside", ["modules/sub/config", "modules/a/b/config", "worktrees/other/config.worktree"]
)
@pytest.mark.parametrize("all_files", [False, True])
def test_a_hard_link_to_any_file_of_the_git_folder_is_refused(
    tmp_path: Path, inside: str, all_files: bool
) -> None:
    """A submodule's configuration, or another worktree's, holds its remote, token and all."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    target = folder / ".git" / inside
    target.parent.mkdir(parents=True)
    target.write_text(f"[remote]\n\turl = {TOKEN_URL}\n")
    os.link(target, folder / "notes.cfg")
    with pytest.raises(LocalRunError, match="git configuration"):
        if all_files:
            local._copy_folder(
                folder,
                tmp_path / "copy",
                tmp_path / "out",
                local._known_git_folders(folder),
                local._known_linked(folder),
            )
        else:
            listing = local._git_files(folder, untracked=True)
            local._copy_files(
                folder,
                tmp_path / "copy",
                listing.names,
                tmp_path / "out",
                git_dirs=listing.git_dirs,
                untracked=listing.untracked,
                linked=listing.linked,
            )


def test_files_linked_to_each_other_in_the_worktree_are_copied(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    os.link(folder / "serve.py", folder / "serve-again.py")
    listing = local._git_files(folder, untracked=True)
    local._copy_files(
        folder,
        tmp_path / "copy",
        listing.names,
        tmp_path / "out",
        git_dirs=listing.git_dirs,
        untracked=listing.untracked,
        linked=listing.linked,
    )
    assert (tmp_path / "copy" / "serve-again.py").read_text() == "print('not run in these tests')\n"


async def test_a_mask_that_cannot_be_written_stops_the_run_before_the_browsers_are_used(
    tmp_path: Path, app_server: AppServer
) -> None:
    """The browsers' path must not be used unmasked: a MaskError when it arrives stops the
    run, and the run is still stopped."""
    serve_two_pages(app_server)
    folder = two_pages(tmp_path)
    connected: list[str] = []

    class MaskFails(FakeRunner):
        async def start_browsers(self, run_id: str, assets: Sequence[str]) -> str:
            await super().start_browsers(run_id, assets)
            raise MaskError("the mask for a secret Footfall made could not be written")

    async def never(engine: str, endpoint: str) -> Browser:
        connected.append(endpoint)
        raise AssertionError("the browsers were used")

    runner = MaskFails(lambda copy: f"127.0.0.1:{app_server.port}")
    with pytest.raises(MaskError):
        await run_folder(folder, runner, never, tmp_path)
    assert connected == []
    assert runner.ops()[-1] == "stop"


def commit_all(folder: Path) -> None:
    git(folder, "add", "-A")
    git(
        folder,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "x",
    )


def walked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every folder the linked-file walk starts from."""
    tops: list[str] = []
    real_walk = os.walk

    def recording(top: Any, *args: Any, **kwargs: Any) -> Any:
        tops.append(str(top))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(os, "walk", recording)
    return tops


def test_the_git_folder_is_walked_once_for_linked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository's own git folder is its common folder too; walk it once."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    os.link(folder / "serve.py", folder / "serve-again.py")
    tops = walked(monkeypatch)
    listing = local._git_files(folder)
    assert not listing.linked.holds((folder / "serve-again.py").lstat())
    assert tops == [str((folder / ".git").resolve())]


def test_a_worktrees_git_folder_inside_the_common_one_is_not_walked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    app_files(main)
    git(main, "init", "-q")
    commit_all(main)
    git(main, "worktree", "add", "-q", str(tmp_path / "other"))
    other = tmp_path / "other"
    os.link(other / "serve.py", other / "serve-again.py")
    tops = walked(monkeypatch)
    listing = local._git_files(other)
    assert not listing.linked.holds((other / "serve-again.py").lstat())
    assert tops == [str((main / ".git").resolve())]


def test_a_git_folder_that_cannot_be_read_refuses_the_copy_of_a_linked_file(
    tmp_path: Path,
) -> None:
    """The walk fails closed: a folder it cannot read might hold the file's other name."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    locked = folder / ".git" / "modules"
    locked.mkdir()
    os.link(folder / "serve.py", folder / "serve-again.py")
    listing = local._git_files(folder, untracked=True)
    locked.chmod(0)
    try:
        with pytest.raises(LocalRunError, match="could not be read"):
            local._copy_files(
                folder,
                tmp_path / "copy",
                listing.names,
                tmp_path / "out",
                git_dirs=listing.git_dirs,
                untracked=listing.untracked,
                linked=listing.linked,
            )
    finally:
        locked.chmod(0o755)


def test_a_file_with_one_name_needs_no_walk_however_unreadable_the_git_folder(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    locked = folder / ".git" / "modules"
    locked.mkdir()
    listing = local._git_files(folder, untracked=True)
    locked.chmod(0)
    try:
        local._copy_files(
            folder,
            tmp_path / "copy",
            listing.names,
            tmp_path / "out",
            git_dirs=listing.git_dirs,
            untracked=listing.untracked,
            linked=listing.linked,
        )
    finally:
        locked.chmod(0o755)
    assert (tmp_path / "copy" / "serve.py").exists()


def test_a_link_to_the_superprojects_git_configuration_is_refused(tmp_path: Path) -> None:
    """Testing a submodule's folder, a hard link to the superproject's configuration, whose
    remote can hold a token, is refused like one to the submodule's own."""
    source = tmp_path / "library"
    app_files(source)
    git(source, "init", "-q")
    commit_all(source)
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q")
    (outer / "README.md").write_text("outer\n")
    commit_all(outer)
    git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "lib")
    git(outer, "config", "remote.origin.url", TOKEN_URL)
    folder = outer / "lib"
    os.link(outer / ".git" / "config", folder / "notes.cfg")
    listing = local._git_files(folder, untracked=True)
    with pytest.raises(LocalRunError, match="git configuration"):
        local._copy_files(
            folder,
            tmp_path / "copy",
            listing.names,
            tmp_path / "out",
            git_dirs=listing.git_dirs,
            untracked=listing.untracked,
            linked=listing.linked,
        )


def test_a_failed_superproject_lookup_refuses_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --all-files, git reads the repository but cannot say whether it is a submodule:
    the copy is refused, rather than made with no hard-link check at all."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    git(folder, "config", "remote.origin.url", TOKEN_URL)
    os.link(folder / ".git" / "config", folder / "settings.ini")

    def fails(git: str, folder: Path, **options: Any) -> list[Path]:
        raise LocalRunError("git cannot read the files in the folder (it failed)")

    monkeypatch.setattr(local, "_superproject_git_folders", fails)
    with pytest.raises(LocalRunError, match="it failed"):
        local._known_linked(folder)


@pytest.mark.parametrize("untracked", [False, True])
def test_a_link_to_the_config_of_the_repository_around_a_nested_one_is_refused(
    tmp_path: Path, untracked: bool
) -> None:
    """Testing a nested repository that is not a submodule, git names no superproject, so the
    repository around it is found as git would find it, and a hard link to its configuration
    is refused, as --all-files refuses it. Untracked, the link is copied only on request."""
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q")
    git(outer, "config", "remote.origin.url", TOKEN_URL)
    folder = outer / "nested"
    app_files(folder)
    git(folder, "init", "-q")
    git(folder, "add", ".")
    os.link(outer / ".git" / "config", folder / "notes.cfg")
    # The control: git names no superproject here.
    assert git_output(folder, "rev-parse", "--show-superproject-working-tree") == ""
    listing = local._git_files(folder, untracked=untracked)
    assert (PurePosixPath("notes.cfg") in listing.names) is untracked
    with contextlib.ExitStack() as refused:
        if untracked:
            refused.enter_context(pytest.raises(LocalRunError, match="git configuration"))
        local._copy_files(
            folder,
            tmp_path / "git-only",
            listing.names,
            tmp_path / "out",
            git_dirs=listing.git_dirs,
            untracked=listing.untracked,
            linked=listing.linked,
        )
    if not untracked:
        assert (tmp_path / "git-only" / "serve.py").exists()
        assert not (tmp_path / "git-only" / "notes.cfg").exists()
    with pytest.raises(LocalRunError, match="git configuration"):
        local._copy_folder(
            folder,
            tmp_path / "all-files",
            tmp_path / "out",
            local._known_git_folders(folder),
            local._known_linked(folder),
        )


def test_git_mode_refuses_a_linked_file_when_a_git_entry_above_cannot_be_followed(
    tmp_path: Path,
) -> None:
    """git reads the repository, but a `.git` above it is a link discovery will not follow,
    so the folders around it are unknown: a file with another name is refused, no other."""
    above = tmp_path / "above"
    above.mkdir()
    (above / ".git").symlink_to(shaped_folder(tmp_path / "elsewhere"))
    folder = above / "app"
    app_files(folder)
    git(folder, "init", "-q")
    git(folder, "add", ".")
    listing = local._git_files(folder)
    assert not listing.linked.holds((folder / "serve.py").lstat())
    info = linked_file(folder)
    with pytest.raises(LocalRunError, match="is a link or another kind"):
        listing.linked.holds(info)


def a_chain_of_superprojects(depth: int) -> Callable[..., bytes]:
    """git's answers for a folder /levels/0 inside `depth` superprojects, /levels/1 and on."""

    def run_git(
        git: str, folder: Path, arguments: Sequence[str], *rest: Any, **options: Any
    ) -> bytes:
        if "--show-superproject-working-tree" in arguments:
            level = int(folder.name)
            return f"/levels/{level + 1}\n".encode() if level < depth else b""
        return f"{folder}/.git\n".encode()

    return run_git


@pytest.mark.parametrize("extra", [0, 1])
def test_superprojects_are_followed_up_to_the_limit_and_no_further(
    monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    depth = local._MOST_SUPERPROJECTS + extra
    monkeypatch.setattr(local, "_run_git", a_chain_of_superprojects(depth))
    if extra:
        with pytest.raises(LocalRunError, match=f"more than {depth - 1} levels deep"):
            local._superproject_git_folders("git", Path("/levels/0"))
        return
    found = local._superproject_git_folders("git", Path("/levels/0"))
    assert len(found) == 2 * depth  # each superproject's git folder and its common one
    assert found[-1] == Path(f"/levels/{depth}/.git")


@pytest.mark.parametrize("failure", ["hangs", "fails"])
def test_a_failed_superproject_lookup_under_all_files_gives_advice_that_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """--all-files still asks git whether the folder is in a submodule; when that fails, the
    advice cannot be to pass --all-files, which was passed."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    real_run = subprocess.run

    def run(command: list[str], **options: Any) -> Any:
        if "--show-superproject-working-tree" in command:
            if failure == "hangs":
                raise subprocess.TimeoutExpired(command, 60)
            return subprocess.CompletedProcess(command, 1, b"", b"fatal: f08-marker\n")
        return real_run(command, **options)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(LocalRunError) as caught:
        local._known_linked(folder)
    message = str(caught.value)
    assert "--all-files" in message  # named, as what still asks git
    assert "pass --all-files" not in message
    assert "readable to git" in message
    if failure == "fails":
        assert "f08-marker" in message


def test_a_folder_outside_git_is_copied_with_all_files(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    os.link(folder / "serve.py", folder / "serve-again.py")
    linked = local._known_linked(folder)
    local._copy_folder(folder, tmp_path / "copy", tmp_path / "out", frozenset(), linked)
    assert (tmp_path / "copy" / "serve-again.py").exists()


def test_a_repository_git_cannot_read_still_refuses_a_link_to_its_config(tmp_path: Path) -> None:
    """--all-files is the way to test a repository git cannot read, such as one another user
    owns; its git folder is then found as git would find it, so the check still holds."""
    folder = tmp_path / "source"
    app_files(folder)
    git(folder, "init", "-q")
    with (folder / ".git" / "config").open("a") as config:
        config.write(f"[core]\n\trepositoryformatversion = 99\n[remote]\n\turl = {TOKEN_URL}\n")
    os.link(folder / ".git" / "config", folder / "settings.ini")
    linked = local._known_linked(folder)
    with pytest.raises(LocalRunError, match="git configuration"):
        local._copy_folder(folder, tmp_path / "copy", tmp_path / "out", frozenset(), linked)


def test_a_moved_git_folder_git_cannot_read_is_found_through_its_git_file(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    moved = moved_git_folder(folder, "meta")
    with (moved / "config").open("a") as config:
        config.write("[core]\n\trepositoryformatversion = 99\n")
    os.link(moved / "config", folder / "settings.ini")
    linked = local._known_linked(folder)
    assert linked.holds((folder / "settings.ini").lstat())


# ---------------------------------------------------------------- discovery, fix round 3


def linked_file(folder: Path) -> os.stat_result:
    """A file in the folder with a second name, so that the hard-link check must run."""
    os.link(folder / "serve.py", folder / "serve-again.py")
    return (folder / "serve-again.py").lstat()


def refused_for(folder: Path, match: str) -> None:
    """The --all-files check, once a file with another name needs it, refuses the copy."""
    info = linked_file(folder)
    linked = local._known_linked(folder)
    with pytest.raises(LocalRunError, match=match) as caught:
        linked.holds(info)
    assert "--all-files" not in str(caught.value)  # advice that would not help


def test_a_commondir_that_is_a_fifo_is_refused_without_waiting(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    gitdir = folder / "g"
    shaped_folder(gitdir)
    (folder / ".git").write_text("gitdir: g\n")
    os.mkfifo(gitdir / "commondir")
    done: list[BaseException | None] = []

    def look() -> None:
        try:
            refused_for(folder, "not a plain file")
            done.append(None)
        except BaseException as error:  # noqa: BLE001  # reported below
            done.append(error)

    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():  # free the reader that waits on the FIFO, then fail
        with contextlib.suppress(OSError):
            os.close(os.open(gitdir / "commondir", os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("discovery waited on a FIFO")
    assert done == [None], done


@pytest.mark.parametrize(
    ("make", "match"),
    [
        ("commondir-link", "not a plain file"),
        ("git-file-nul", "NUL"),
        ("gitdir-root", "not shaped like a git folder"),
        ("gitdir-elsewhere", "not shaped like a git folder"),
        ("no-gitdir-line", "cannot tell where"),
        # Not "link" alone: the advice's "the hard links" holds that whatever the reason.
        ("git-link", "is a link or another kind"),
        ("unreadable", "cannot read"),
        ("huge-line", "not shaped like a git folder"),
    ],
)
def test_a_git_entry_footfall_cannot_follow_refuses_a_linked_file(
    tmp_path: Path, make: str, match: str
) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    git_file = folder / ".git"
    if make == "commondir-link":
        shaped_folder(folder / "g")
        git_file.write_text("gitdir: g\n")
        (folder / "g" / "commondir").symlink_to("/etc/hosts")
    elif make == "git-file-nul":
        git_file.write_bytes(b"gitdir: g\0h\n")
    elif make == "gitdir-root":
        git_file.write_text("gitdir: /\n")
    elif make == "gitdir-elsewhere":
        (folder / "plain").mkdir()
        git_file.write_text("gitdir: plain\n")
    elif make == "no-gitdir-line":
        git_file.write_text("not what git writes\n")
    elif make == "git-link":
        shaped_folder(folder / "real")
        git_file.symlink_to("real")
    elif make == "unreadable":
        git_file.write_text("gitdir: g\n")
        git_file.chmod(0)
    elif make == "huge-line":
        git_file.write_text("gitdir: " + "g" * 100_000)
    try:
        refused_for(folder, match)
    finally:
        if git_file.exists() and not git_file.is_symlink():
            git_file.chmod(0o644)


@pytest.mark.parametrize("call", ["fstat", "read"])
def test_a_git_file_that_fails_as_it_is_read_cannot_be_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """An error once the file is open, from the disk say, is a reason not to follow it,
    reported as one, and the file is closed all the same."""
    folder = tmp_path / "source"
    app_files(folder)
    path = folder / ".git"
    path.write_text("gitdir: g\n")
    opened: list[int] = []
    real_open, real_call = os.open, getattr(os, call)

    def recording(name: Any, flags: int, *rest: Any, **options: Any) -> int:
        fd = real_open(name, flags, *rest, **options)
        if str(name) == str(path):
            opened.append(fd)
        return fd

    def failing(fd: int, *rest: Any) -> Any:
        if fd in opened:
            raise OSError(errno.EIO, "Input/output error")
        return real_call(fd, *rest)

    monkeypatch.setattr(os, "open", recording)
    monkeypatch.setattr(os, call, failing)
    with pytest.raises(local._Undiscoverable, match=r"cannot read .*\(Input/output error\)"):
        local._read_small(path)
    monkeypatch.undo()
    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])


def test_a_git_folder_not_shaped_like_one_is_named_once(tmp_path: Path) -> None:
    folder = tmp_path / "source"
    app_files(folder)
    (folder / ".git").mkdir()  # no HEAD, no objects
    info = linked_file(folder)
    with pytest.raises(LocalRunError) as caught:
        local._known_linked(folder).holds(info)
    message = str(caught.value)
    assert '" is not shaped like a git folder' in message
    assert " names " not in message


def test_only_the_start_of_a_git_file_is_read(tmp_path: Path) -> None:
    """A .git file is read only as far as `_SMALL_FILE`, so a huge one, sparse or not, costs
    nothing. Its line names one folder in its first bytes and another in the whole line, so
    which one is found says how much was read."""
    folder = tmp_path / "source"
    app_files(folder)
    first = shaped_folder(folder / "first")
    shaped_folder(first / "whole")
    # "first/./././...": however much of the dots is read, it names first; only the whole
    # line, ending in "whole", names the folder inside it.
    dots = "./" * local._SMALL_FILE
    (folder / ".git").write_text(f"gitdir: first/{dots}whole\n")
    found = local._discovered_git_folders(folder)
    assert found[0] == first, found[0]


def test_a_file_with_one_name_is_copied_however_odd_the_git_entry(tmp_path: Path) -> None:
    """The check runs only for a file with another name, so nothing else is held back."""
    folder = tmp_path / "source"
    app_files(folder)
    (folder / ".git").write_text("gitdir: /\n")
    local._copy_folder(
        folder, tmp_path / "copy", tmp_path / "out", frozenset(), local._known_linked(folder)
    )
    assert (tmp_path / "copy" / "serve.py").exists()


def test_a_linked_worktrees_common_folder_is_found_without_git(tmp_path: Path) -> None:
    """The commondir a worktree's git folder names is walked too, when git cannot help."""
    main = tmp_path / "main"
    app_files(main)
    git(main, "init", "-q")
    commit_all(main)
    git(main, "worktree", "add", "-q", str(tmp_path / "other"))
    found = local._discovered_git_folders(tmp_path / "other")
    assert (main / ".git").resolve() in {path.resolve() for path in found}


def test_an_implicit_bare_repository_is_a_git_folder(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    git(tmp_path, "init", "-q", "--bare", "bare")
    folder = bare / "app"
    app_files(folder)
    assert bare.resolve() in {path.resolve() for path in local._discovered_git_folders(folder)}


def test_a_nested_submodule_refuses_links_to_every_superprojects_config(tmp_path: Path) -> None:
    """outer contains lib, which contains inner: testing inner, a hard link to outer's
    configuration is refused, as one to lib's is."""
    for name in ("inner", "lib", "outer"):
        app_files(tmp_path / name)
        git(tmp_path / name, "init", "-q")
        commit_all(tmp_path / name)
    allow = ("-c", "protocol.file.allow=always")
    git(tmp_path / "lib", *allow, "submodule", "add", "-q", str(tmp_path / "inner"), "inner")
    commit_all(tmp_path / "lib")
    git(tmp_path / "outer", *allow, "submodule", "add", "-q", str(tmp_path / "lib"), "lib")
    git(tmp_path / "outer", *allow, "submodule", "update", "--init", "--recursive", "-q")
    folder = tmp_path / "outer" / "lib" / "inner"
    os.link(tmp_path / "outer" / ".git" / "config", folder / "notes.cfg")
    info = (folder / "notes.cfg").lstat()
    assert local._git_files(folder).linked.holds(info)
    assert local._known_linked(folder).holds(info)
