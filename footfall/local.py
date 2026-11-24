"""One run of a local folder: what `footfall test` does, from the manifest to the results.

The orchestrator holds the Anthropic key and drives the browsers; the runner, over its socket,
holds Docker. Nothing here starts a container or opens the Docker socket (AGENTS.md rule 1).
A run copies the folder into the workspaces root and asks the runner to prepare it, to start
one copy of the app for each persona and one browser container, and to stop it all at the end,
whatever happened in between.

When the manifest has a `[login]` section, the run makes a throwaway username and password.
The password reaches a container only through the seed step's environment (AGENTS.md rule 3).
Footfall signs each persona in before it starts when the brief starts signed in. Either way
the tester is given the account in its first message, and every executor redacts the
password, so it is written to no trace, report or summary, printed nowhere and logged nowhere.

Each persona runs in its own browser context, and a persona that fails, whatever the reason,
ends alone: its context is closed and the others carry on. Each one's report is written as it
ends, and the run's summary once all have.
"""

import asyncio
import base64
import contextlib
import functools
import json
import logging
import secrets
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Protocol, TextIO
from urllib.parse import quote as url_quote

from playwright.async_api import Browser, async_playwright
from playwright.async_api import Error as PlaywrightError

from footfall.browsers import Engine, SignInError, connect_run_server, open_context, sign_in
from footfall.manifest import Brief, Manifest, load_brief, load_manifest
from footfall.personas import Device, Persona, PersonaError, load_devices, load_personas, select
from footfall.tester.guard import Guard
from footfall.tester.loop import Budget, run_persona
from footfall.tester.model import model_from_spec
from footfall.tester.report import PersonaReport, clean_text
from footfall.tester.tools import REDACTED, Executor
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

type ConnectBrowser = Callable[[str, str], Awaitable[Browser]]


class Runner(Protocol):
    """What a run needs from the runner; `RunnerClient` is the real one."""

    async def prepare(
        self,
        run_id: str,
        workspace: Path,
        runtime: str,
        install: str | None,
        seed: str | None,
        egress: Sequence[str],
        env: Mapping[str, str],
    ) -> None: ...

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
    """The results folder already holds results that this run would overwrite."""


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
    except (OSError, TimeoutError):
        return False
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
    """The run's results as a table: one line for each persona, then the total."""
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
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def _failed(persona: Persona, device: Device, reason: str, started: float) -> PersonaReport:
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
        cost_usd=0.0,
        seconds=time.monotonic() - started,
    )


def _write_json(path: Path, data: object, password: str | None) -> None:
    """Write `data` as JSON, with the password taken out once more in case anything held it."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if password:
        text = text.replace(password, REDACTED)
    path.write_text(text, encoding="utf-8")


def _copy_folder(folder: Path, workspace: Path, out: Path) -> None:
    """Copy the folder to test into its workspace, links as links, leaving the results out."""
    results = out.resolve()

    def ignore(where: str, names: list[str]) -> set[str]:
        return {name for name in names if (Path(where) / name).resolve() == results}

    shutil.copytree(folder, workspace, symlinks=True, ignore=ignore)


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
        budget: Budget,
        proxy_url: str,
        account: tuple[str, str] | None,
    ) -> None:
        self.run_id = run_id
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

    async def persona(self, persona: Persona, browser: Browser, address: str) -> PersonaReport:
        """Set up and run one persona, and write its report. Never raises but to be cancelled."""
        device = self.devices[persona.device]
        started = time.monotonic()
        folder = self.out / persona.id
        app_origin = f"http://{address}"
        context = None
        try:
            context = await open_context(
                browser, persona, device, self.descriptors, app_origin, self.proxy_url
            )
            guard = Guard(app_origin, frozenset(self.manifest.browser.assets))
            await guard.install(context)
            page = await context.new_page()
            await page.goto(app_origin + "/", timeout=GOTO_TIMEOUT_MS)
            login = self.manifest.login
            if self.brief.start == "signed-in" and login is not None and self.account:
                await sign_in(page, login, *self.account)
            executor = Executor(
                page,
                guard,
                Trace(folder / "trace.jsonl"),
                folder / "screenshots",
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
            )
        except SignInError as error:
            report = _failed(persona, device, str(error), started)
        except Exception as error:  # noqa: BLE001  # any failure ends this persona, not the run
            # The class alone: a message may quote the page, or what was typed into it.
            report = _failed(persona, device, type(error).__name__, started)
        finally:
            if context is not None:
                with contextlib.suppress(PlaywrightError):
                    await context.close()
        _write_json(folder / "report.json", report.to_json(), self.password)
        LOG.info("persona %s: %s", persona.id, report.status)
        return report

    def summary(self, reports: Sequence[PersonaReport]) -> dict[str, Any]:
        return {
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


def _clean_tail(text: str, password: str | None) -> str:
    """An app's log tail, safe to print: the password out and no terminal control characters."""
    if password:
        text = text.replace(password, REDACTED)
    return clean_text(text, LOG_TAIL_CAP)


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


def _prepare_results(out: Path, personas: Sequence[Persona]) -> list[Path]:
    """Make a results folder for each persona, refusing to overwrite what an earlier run left
    there, and return them."""
    taken = [out / p.id for p in personas if (out / p.id).exists()]
    if (out / "summary.json").exists():
        taken.append(out / "summary.json")
    if taken:
        raise OutputError(
            f"{taken[0]} already exists; choose another results folder with --out, "
            "or move the old results away"
        )
    out.mkdir(parents=True, exist_ok=True)
    folders = [out / persona.id for persona in personas]
    for folder in folders:
        folder.mkdir()
    return folders


def _tidy_results(folders: Sequence[Path]) -> None:
    """Remove the persona folders a failed run left empty, so that they block no later run."""
    for folder in folders:
        with contextlib.suppress(OSError):
            folder.rmdir()  # only an empty folder


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
    connect_browser: ConnectBrowser | None = None,
    workspaces: Path = DEFAULT_WORKSPACES,
    proxy_container: str = DEFAULT_PROXY_CONTAINER,
    stdout: TextIO | None = None,
) -> list[PersonaReport]:
    """Test a local folder with the named personas (the manifest's, when none are named).

    `connect_browser` takes an engine and the run's Playwright endpoint and returns a
    browser; by default it connects to the run's browser container. Writes the results into
    `out` and prints a table of them. The runner is asked to stop the run however this ends.
    Raises ManifestError, PersonaError, ModelSpecError, OutputError, NotReadyError,
    RunnerError or BrowserConnectError when the run cannot go ahead.
    """
    manifest = load_manifest(folder)
    brief = load_brief(folder, manifest, brief_name)
    devices = load_devices()
    personas = select(load_personas(devices), list(persona_ids) or list(manifest.test.personas))
    model_from_spec(model_spec)  # a spec that names nothing runnable stops the run here

    run_id = new_run_id()
    account = None
    if manifest.login is not None:
        account = ("tester-" + secrets.token_urlsafe(6), secrets.token_urlsafe(24))
    workspace = workspaces / run_id
    stream = stdout or sys.stdout
    async with contextlib.AsyncExitStack() as stack:
        playwright = await stack.enter_async_context(async_playwright())
        connect = connect_browser or functools.partial(connect_run_server, playwright)
        missing = [d.playwright for d in devices.values() if d.playwright not in playwright.devices]
        if missing:
            raise PersonaError(f"Playwright has no device called {missing[0]!r}")
        folders = _prepare_results(out, personas)
        run = _Run(
            run_id=run_id,
            manifest=manifest,
            brief=brief,
            devices=devices,
            descriptors=playwright.devices,
            focus=focus,
            model_spec=model_spec,
            out=out,
            budget=budget,
            proxy_url=f"http://{proxy_container}:{PROXY_PORT}",
            account=account,
        )
        try:
            reports = await _go(run, runner, connect, personas, folder, workspace)
            _write_json(out / "summary.json", run.summary(reports), run.password)
            print(format_table(reports), file=stream)
        except BaseException:
            await _finish(runner, run_id, workspace, quiet=True)
            _tidy_results(folders)
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
) -> list[PersonaReport]:
    """Prepare the app, start its copies and the browsers, and run every persona."""
    manifest = run.manifest
    workspace.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_copy_folder, folder, workspace, run.out)
    env: dict[str, str] = {}
    if run.account is not None:
        env = {"FOOTFALL_USERNAME": run.account[0], "FOOTFALL_PASSWORD": run.account[1]}
    app = manifest.app
    LOG.info("preparing run %s", run.run_id)
    await runner.prepare(run.run_id, workspace, app.runtime, app.install, app.seed, app.egress, env)
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
        return list(
            await asyncio.gather(
                *(
                    run.persona(p, browsers[run.devices[p.device].engine], addresses[p.id])
                    for p in personas
                )
            )
        )
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
