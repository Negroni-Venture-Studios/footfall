"""The run's browser container under gVisor: its gate, its browsers, and what a page reaches.

The orchestrator connects to run-server through the gate with the run's secret path, and
opens each context as `footfall test` does: the persona's device, the guard, and the egress
proxy for every host but the app's. The app here is probe.py's page server, which can send
the browser anywhere. The proxy's own log says what reached it.

The run-server path is a secret: no assertion here prints an endpoint or a page that could
hold one, only whether it was there.
"""

import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from footfall.browsers import CONNECT_TIMEOUT_MS, LAUNCH_HEADER, connect_run_server, open_context
from footfall.personas import load_devices, load_personas
from footfall.tester.guard import Guard, GuardEvent
from footfall.tester.tools import Executor
from footfall.tester.trace import Trace
from tests.isolation.live import HELLO, HIT, LiveRun, ProxyLog, address_of, lines

pytestmark = pytest.mark.isolation

PROXY_URL: Final = "http://footfall-proxy:3128"  # as footfall test gives it
ASSETS: Final = ("example.com",)
NOT_LISTED: Final = "is not on this client's allow list"
GVISOR_RELEASE: Final = "4.4.0"  # the kernel release gVisor reports, whatever the host runs
# Every code point of Encrypted Client Hello and of the encrypted SNI before it.
HIDDEN_NAMES: Final = frozenset({0xFE08, 0xFE09, 0xFE0A, 0xFE0B, 0xFE0C, 0xFE0D, 0xFF02, 0xFFCE})
ECH: Final = 0xFE0D
PERSONA_FOR: Final = {"chromium": "grace", "webkit": "sven"}
RUN_SERVER: Final = "http://127.0.0.1:3001/json"
ENGINES: Final = ("chromium", "webkit")


@dataclass
class Browsers:
    run: LiveRun
    app: str  # the page server's origin
    endpoint: str = field(repr=False)  # holds the secret path: never printed
    address: str  # the browser container's address on the run's network
    playwright: Playwright = field(repr=False)

    async def connect(self, engine: str) -> Browser:
        return await connect_run_server(self.playwright, engine, self.endpoint)

    async def context(self, browser: Browser, engine: str) -> BrowserContext:
        """A context exactly as `footfall test` opens one for a persona on this engine."""
        devices = load_devices()
        persona = load_personas(devices)[PERSONA_FOR[engine]]
        device = devices[persona.device]
        assert device.engine == engine
        return await open_context(
            browser, persona, device, self.playwright.devices, self.app, PROXY_URL
        )


@pytest.fixture
async def browsers(run: LiveRun) -> AsyncIterator[Browsers]:
    pages, _ = await run.serve("pages", "pages", 8000)
    endpoint = await run.client.start_browsers(run.run_id, list(ASSETS))
    async with async_playwright() as playwright:
        yield Browsers(run, f"http://{pages}", endpoint, address_of(endpoint), playwright)


async def _text(page: Page) -> str:
    try:
        return await page.content()
    except PlaywrightError:
        return ""


def _hosts(event: GuardEvent) -> list[str]:
    """The hosts a guard event names, each exactly as the guard quoted it."""
    return [urlsplit("//" + name).hostname or "" for name in re.findall(r'"([^"]*)"', event.detail)]


async def _goto(page: Page, url: str) -> int | None:
    """Go to `url` and return the status of its response, or None if it failed."""
    try:
        response = await page.goto(url, timeout=30_000)
    except PlaywrightError:
        return None
    return None if response is None else response.status


# ---------------------------------------------------------------- the browsers themselves


@pytest.mark.parametrize("engine", ENGINES)
async def test_each_engine_launches_through_run_server(browsers: Browsers, engine: str) -> None:
    browser = await browsers.connect(engine)
    try:
        context = await browsers.context(browser, engine)
        page = await context.new_page()
        assert await _goto(page, browsers.app + "/") == 200
        assert await page.title() == "Probe"
    finally:
        await browser.close()


async def test_the_browser_container_runs_on_gvisors_kernel(browsers: Browsers) -> None:
    # Neither browser can read /proc through file:// under gVisor (Chromium's page comes back
    # empty and WebKit's crashes), so the kernel's release comes from what full Chromium
    # reports of the system it runs on. Both engines are run-server's children in the one
    # container, behind the one gate, so one kernel serves both.
    browser = await browsers.connect("chromium")
    try:
        page = await browser.new_page()
        await _goto(page, "chrome://version")
        text = await page.inner_text("body")
        hints = await page.evaluate(
            "navigator.userAgentData ? navigator.userAgentData"
            ".getHighEntropyValues(['platformVersion']).then(v => v.platformVersion) : ''"
        )
    finally:
        await browser.close()
    found = set(re.findall(r"Linux\s+(\d+\.\d+\.\d+)", text))
    if isinstance(hints, str) and re.fullmatch(r"\d+\.\d+\.\d+", hints):
        found.add(hints)
    host = re.match(r"\d+\.\d+\.\d+", os.uname().release)
    assert host is not None
    assert found == {GVISOR_RELEASE}, (found, hints, text[:2000])
    assert host.group(0) != GVISOR_RELEASE  # the host's own kernel is another


async def test_chromium_is_the_full_build_and_reads_the_policy(browsers: Browsers) -> None:
    browser = await browsers.connect("chromium")
    try:
        page = await browser.new_page()
        await _goto(page, "chrome://version")
        executable = (await page.locator("#executable_path").inner_text()).strip()
        assert re.fullmatch(r"/ms-playwright/chromium-\d+/chrome-linux[^/]*/chrome", executable)
        await _goto(page, "chrome://policy")
        await page.get_by_text("EncryptedClientHelloEnabled").first.wait_for(timeout=15_000)
    finally:
        await browser.close()


async def test_chromium_sends_no_encrypted_client_hello(browsers: Browsers) -> None:
    run = browsers.run
    # By address: Docker's name service does not answer inside gVisor.
    address, _ = await run.serve("hello", "hello", 8443)
    target = f"https://{address}/"
    failed: dict[str, list[str]] = {}

    async def hellos() -> list[dict[str, Any]]:
        return lines(await run.logs("hello"), HELLO)

    async def visit(browser: Browser, name: str) -> list[dict[str, Any]]:
        """The ClientHellos the probe saw when this browser went to it."""
        before = len(await hellos())
        failed[name] = []
        # The browser container's environment names the egress proxy, which Chromium uses
        # when a context gives none, and which refuses an address. So the probe and the app
        # are bypassed, as footfall test bypasses the app.
        bypass = ",".join(urlsplit(url).hostname or "" for url in (target, browsers.app))
        context = await browser.new_context(
            ignore_https_errors=True, proxy={"server": PROXY_URL, "bypass": bypass}
        )
        page = await context.new_page()
        page.on("requestfailed", lambda r: failed[name].append(f"{r.url}: {r.failure}"))
        # From the app's own page, on the run's private network like the probe.
        await _goto(page, browsers.app + "/")
        try:
            await page.goto(target, timeout=30_000)
        except PlaywrightError as error:
            failed[name].append(str(error).splitlines()[0])
        with contextlib.suppress(TimeoutError):
            await run.wait_for("hello", HELLO, count=before + 1, timeout=20)
        await asyncio.sleep(2)
        return (await hellos())[before:]

    full = await browsers.connect("chromium")  # as the orchestrator launches it
    try:
        from_full = await visit(full, "full")
    finally:
        await full.close()
    # The control: Playwright's headless shell reads no policies and sends ECH, so the
    # probe must see it there, or its silence about full Chromium proves nothing.
    shell = await browsers.playwright.chromium.connect(
        browsers.endpoint, timeout=CONNECT_TIMEOUT_MS, headers={LAUNCH_HEADER: json.dumps({})}
    )
    try:
        from_shell = await visit(shell, "shell")
    finally:
        await shell.close()
    accepted = [hit for hit in lines(await run.logs("hello"), HIT) if "accepted" in hit]
    seen = (from_full, from_shell, failed, f"{len(accepted)} connections accepted")
    assert from_full and all("extensions" in h for h in from_full), seen
    assert [h for h in from_full if HIDDEN_NAMES & set(h["extensions"])] == [], seen
    assert any(ECH in h.get("extensions", []) for h in from_shell), seen


async def test_chromium_loads_an_allowed_https_asset_through_the_proxy(
    browsers: Browsers, proxy_log: ProxyLog
) -> None:
    browser = await browsers.connect("chromium")
    try:
        context = await browsers.context(browser, "chromium")
        await Guard(browsers.app, frozenset(ASSETS)).install(context)
        page = await context.new_page()
        # In a frame, not as a script: Chromium's opaque response blocking would stop an HTML
        # page loaded as a script after it arrived, and report no response for it.
        listed = lambda r: urlsplit(r.url).hostname == "example.com"  # noqa: E731
        async with page.expect_response(listed) as got:
            await _goto(page, browsers.app + "/frame?src=https://example.com/")
        response = await got.value
        assert response.status == 200
    finally:
        await browser.close()
    # The proxy refuses every ClientHello that carries ECH, so this is the end-to-end proof.
    await proxy_log.wait_for(browsers.address, "example.com", "allowed")
    refused = [d for d in proxy_log.decisions(browsers.address, "example.com") if "refused" in d]
    assert refused == []


# ---------------------------------------------------------------- the gate


async def test_the_gate_shows_an_app_container_nothing(browsers: Browsers) -> None:
    run = browsers.run
    answers = await run.probe("gate", "raw", f"{browsers.address}:3000")
    scan = await run.probe("scan", "scan", f"{browsers.address}:1-65535")
    asked = {"json", "root", "json-upgrade", "wrong-path-upgrade", "root-upgrade", "http10"}
    assert set(answers) == asked
    for name, answer in answers.items():
        closed = answer.get("bytes") == 0 or "error" in answer
        refused = answer.get("status_line") == "HTTP/1.1 404 Not Found" and not answer["body_bytes"]
        assert closed or refused, (name, answer)
    shown = json.dumps([answers, scan])
    assert "wsEndpoint" not in shown
    leaked = browsers.endpoint.rpartition("/")[2] in shown  # the secret path, never printed
    assert not leaked
    # run-server listens on loopback alone; the gate is all there is on the network.
    assert scan[f"{browsers.address}:1-65535"]["open"] == [3000], scan


async def test_the_path_connects_from_the_orchestrator(browsers: Browsers) -> None:
    for engine in ENGINES:
        browser = await browsers.connect(engine)
        try:
            assert browser.is_connected()
            assert browser.version
        finally:
            await browser.close()


@pytest.mark.parametrize("engine", ENGINES)
async def test_a_page_cannot_reach_run_server_on_loopback(
    browsers: Browsers, proxy_log: ProxyLog, engine: str
) -> None:
    browser = await browsers.connect(engine)
    try:
        context = await browsers.context(browser, engine)
        page = await context.new_page()
        await _goto(page, RUN_SERVER)
        direct = "wsEndpointPath" in await _text(page)
        await _goto(page, browsers.app + "/redirect?to=run-server")
        redirected = "wsEndpointPath" in await _text(page)
        await _goto(page, browsers.app + "/frame?src=" + RUN_SERVER)
        await asyncio.sleep(2)
        framed = False
        for frame in page.frames[1:]:
            with contextlib.suppress(PlaywrightError):
                framed = framed or "wsEndpointPath" in await frame.content()
        # The control: with no proxy, a page does reach run-server there, so the proxy,
        # through which even loopback goes, is what keeps a page from it.
        bare = await browser.new_context()
        control = await bare.new_page()
        await _goto(control, RUN_SERVER)
        reachable = "wsEndpointPath" in await _text(control)
    finally:
        await browser.close()
    assert (direct, redirected, framed) == (False, False, False)
    assert reachable
    refused = [d for d in proxy_log.decisions(browsers.address) if "not a valid host" in d]
    assert refused, proxy_log.decisions(browsers.address)


# ---------------------------------------------------------------- the guard and the proxy


@pytest.mark.parametrize("engine", ENGINES)
async def test_an_asset_off_the_list_is_aborted_and_recorded(
    browsers: Browsers, proxy_log: ProxyLog, engine: str
) -> None:
    browser = await browsers.connect(engine)
    try:
        context = await browsers.context(browser, engine)
        guard = Guard(browsers.app, frozenset())  # the demo app's manifest names no assets
        await guard.install(context)
        page = await context.new_page()
        assert await _goto(page, browsers.app + "/asset?src=https://example.com/x.js") == 200
        await asyncio.sleep(2)
        events = guard.drain()
    finally:
        await browser.close()
    blocked = [e for e in events if e.kind == "blocked_request" and _hosts(e) == ["example.com"]]
    assert blocked, events
    assert proxy_log.decisions(browsers.address, "example.com") == []  # it never left


async def test_go_to_a_protocol_relative_address_is_refused(
    browsers: Browsers, proxy_log: ProxyLog, tmp_path: Path
) -> None:
    browser = await browsers.connect("chromium")
    try:
        context = await browsers.context(browser, "chromium")
        guard = Guard(browsers.app, frozenset())
        await guard.install(context)
        page = await context.new_page()
        await _goto(page, browsers.app + "/")
        executor = Executor(
            page, guard, Trace(tmp_path / "trace.jsonl"), tmp_path / "shots", touch=True
        )
        await executor.first_look()
        outcome = await executor.run("go", {"to": "//example.com/"})
        url = page.url
    finally:
        await browser.close()
    assert outcome.error
    assert url.startswith(browsers.app + "/")
    hits = [hit["path"] for hit in lines(await browsers.run.logs("pages"), HIT)]
    assert "//example.com/" not in hits
    assert proxy_log.decisions(browsers.address, "example.com") == []


@pytest.mark.parametrize("engine", ENGINES)
async def test_a_redirect_hop_off_the_list_is_refused_by_the_proxy(
    browsers: Browsers, proxy_log: ProxyLog, engine: str
) -> None:
    browser = await browsers.connect(engine)
    try:
        context = await browsers.context(browser, engine)
        await Guard(browsers.app, frozenset(ASSETS)).install(context)
        page = await context.new_page()
        # The control: a hop to a host on the list goes through.
        listed = await _goto(page, browsers.app + "/redirect?to=listed")
        landed = page.url
        unlisted = await _goto(page, browsers.app + "/redirect?to=unlisted")
    finally:
        await browser.close()
    assert listed == 200
    assert landed.startswith("https://example.com/")
    assert unlisted is None or unlisted >= 400
    await proxy_log.wait_for(browsers.address, "pypi.org", NOT_LISTED)
    await proxy_log.wait_for(browsers.address, "example.com", "allowed")


@pytest.mark.parametrize("engine", ENGINES)
async def test_an_unload_beacon_and_a_keepalive_fetch_are_refused_by_the_proxy(
    browsers: Browsers, proxy_log: ProxyLog, engine: str
) -> None:
    beacon, keepalive = "unload-beacon.example.com", "keepalive-fetch.example.com"
    browser = await browsers.connect(engine)
    try:
        context = await browsers.context(browser, engine)
        guard = Guard(browsers.app, frozenset(ASSETS))
        await guard.install(context)
        page = await context.new_page()
        leaving = f"/leaving?beacon=https://{beacon}/b&keepalive=https://{keepalive}/k"
        assert await _goto(page, browsers.app + leaving) == 200
        assert await _goto(page, browsers.app + "/") == 200  # pagehide sends both
        await proxy_log.wait_for(browsers.address, beacon, NOT_LISTED, timeout=30)
        await proxy_log.wait_for(browsers.address, keepalive, NOT_LISTED, timeout=30)
        events = guard.drain()
    finally:
        await browser.close()
    # The guard never saw them: the proxy is the wall.
    assert [e for e in events if {beacon, keepalive} & set(_hosts(e))] == []
    for host in (beacon, keepalive):
        assert [d for d in proxy_log.decisions(browsers.address, host) if "allowed" in d] == []
