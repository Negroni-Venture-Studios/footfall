"""Browser contexts made for a persona, signing in, and connecting to the run's browsers."""

import asyncio
import json
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright._impl._driver import compute_driver_executable
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from footfall import browsers
from footfall.browsers import (
    CHROMIUM_ARGS,
    BrowserConnectError,
    SignInError,
    connect_run_server,
    open_context,
    sign_in,
)
from footfall.manifest import LoginSection
from footfall.personas import DEVICES, PERSONAS, load_devices, load_personas
from tests.app_server import AppServer

_DEVICES = load_devices(DEVICES)
_PERSONAS = load_personas(_DEVICES, PERSONAS)
# Values chosen so that none of them is a browser's or Playwright's default.
PERSONA = replace(
    _PERSONAS["grace"],
    locale="sv-SE",
    timezone="Asia/Tokyo",
    color_scheme="dark",
    reduced_motion=True,
    text_scale=1.75,
    geolocation=(-33.8688, 151.2093),
    allow_location=True,
)
PIXEL = _DEVICES["Pixel 7"]
STRICT = {"Content-Security-Policy": "default-src 'self'", "Content-Type": "text/html"}
PAGE = (
    '<!doctype html><meta name="viewport" content="width=device-width">'
    "<title>Plain</title><h1>Plain</h1>"
)
USERNAME = "tester-Wq3"
PASSWORD = "Xb7-kT2_mQ9vRz4L"  # gitleaks:allow
LOGIN = LoginSection(
    page="/login", username_label="Email address", password_label="Passphrase", submit="Log me in"
)


def login_page(action: str) -> str:
    return (
        "<!doctype html><title>Sign in</title>"
        f'<form method="get" action="{action}">'
        '<label for="u">Email address</label><input id="u" name="u">'
        '<label for="p">Passphrase</label><input id="p" name="p" type="password">'
        "<button>Log me in</button></form>"
    )


@pytest.fixture
async def playwright() -> AsyncIterator[Playwright]:
    async with async_playwright() as running:
        yield running


@pytest.fixture
async def contexts(loopback_browser: Browser) -> AsyncIterator[list[BrowserContext]]:
    """Contexts a test opens, closed however it ends."""
    made: list[BrowserContext] = []
    try:
        yield made
    finally:
        for context in made:
            await context.close()


async def persona_context(
    browser: Browser,
    playwright: Playwright,
    app: AppServer,
    made: list[BrowserContext],
    persona: Any = PERSONA,
    proxy: str = "http://127.0.0.1:9",
) -> BrowserContext:
    context = await open_context(browser, persona, PIXEL, playwright.devices, app.origin, proxy)
    made.append(context)
    return context


async def test_open_context_applies_persona(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    app_server.reply("/", PAGE, headers=STRICT)
    app_server.reply("/sw.js", "", headers={"Content-Type": "text/javascript"})
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    seen = await page.evaluate(
        """async () => ({
            width: window.innerWidth,
            language: navigator.language,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            dark: matchMedia("(prefers-color-scheme: dark)").matches,
            reduced: matchMedia("(prefers-reduced-motion: reduce)").matches,
            touch: navigator.maxTouchPoints > 0,
            agent: navigator.userAgent,
            rootFont: getComputedStyle(document.documentElement).fontSize,
            headingFont: getComputedStyle(document.querySelector("h1")).fontSize,
            styles: document.querySelectorAll("style").length,
            workers: await navigator.serviceWorker.register("/sw.js").then(
                () => navigator.serviceWorker.getRegistrations()).then(r => r.length),
        })"""
    )
    descriptor = playwright.devices["Pixel 7"]
    assert seen == {
        "width": descriptor["viewport"]["width"],
        "language": "sv-SE",
        "timezone": "Asia/Tokyo",
        "dark": True,
        "reduced": True,
        "touch": True,
        "agent": descriptor["user_agent"],
        "rootFont": "28px",  # 16px times 1.75, despite a policy that refuses inline styles
        "headingFont": "56px",
        "styles": 0,
        "workers": 0,
    }


async def test_the_text_scale_holds_after_the_page_loads(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    # A page that sets its own root style as it is parsed does not undo the scale.
    app_server.reply(
        "/",
        '<!doctype html><html style="color: red"><title>Own</title><h1>Own</h1>',
        headers={"Content-Type": "text/html"},
    )
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    font = await page.evaluate("getComputedStyle(document.documentElement).fontSize")
    assert font == "28px"


async def test_a_text_scale_of_one_leaves_the_page_alone(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    app_server.reply("/", PAGE, headers=STRICT)
    plain = replace(PERSONA, text_scale=1.0)
    context = await persona_context(loopback_browser, playwright, app_server, contexts, plain)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    assert await page.evaluate("document.documentElement.getAttribute('style')") is None


async def test_motion_and_scheme_follow_the_persona(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    app_server.reply("/", PAGE, headers=STRICT)
    light = replace(PERSONA, color_scheme="light", reduced_motion=False)
    context = await persona_context(loopback_browser, playwright, app_server, contexts, light)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    assert await page.evaluate("matchMedia('(prefers-color-scheme: light)').matches")
    assert not await page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")


LOCATE = """() => new Promise(resolve => navigator.geolocation.getCurrentPosition(
    p => resolve([p.coords.latitude, p.coords.longitude]),
    e => resolve("refused: " + e.code),
    {timeout: 3000}))"""
PERMISSION = "async () => (await navigator.permissions.query({name: 'geolocation'})).state"


async def test_location_permission_only_when_allowed(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    app_server.reply("/", PAGE, headers=STRICT)
    allowed = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await allowed.new_page()
    await page.goto(app_server.origin + "/")
    assert await page.evaluate(PERMISSION) == "granted"
    assert await page.evaluate(LOCATE) == [-33.8688, 151.2093]

    refuses = replace(PERSONA, allow_location=False)
    denied = await persona_context(loopback_browser, playwright, app_server, contexts, refuses)
    page = await denied.new_page()
    await page.goto(app_server.origin + "/")
    assert await page.evaluate(PERMISSION) != "granted"
    assert await page.evaluate(LOCATE) == "refused: 1"  # PERMISSION_DENIED


async def test_location_is_granted_to_the_app_alone(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    other_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    other_server.reply("/", PAGE, headers=STRICT)
    context = await persona_context(
        loopback_browser, playwright, app_server, contexts, proxy=other_server.origin
    )
    page = await context.new_page()
    await page.goto(f"http://evil.test:{other_server.port}/")
    assert await page.evaluate(PERMISSION) != "granted"


async def test_hosts_other_than_the_app_go_through_the_proxy(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    other_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    app_server.page("/", PAGE)
    with AppServer() as proxy:
        # A proxy is sent the whole address; this server answers by its path alone.
        proxy.reply("/font.css", "body{}", headers={"Content-Type": "text/css"})
        context = await persona_context(
            loopback_browser, playwright, app_server, contexts, proxy=proxy.origin
        )
        page = await context.new_page()
        await page.goto(app_server.origin + "/")
        sent = await page.evaluate(
            "fetch('http://assets.test/font.css', {mode: 'no-cors'}).then(() => 'sent', () => 'no')"
        )
        assert sent == "sent"
        assert proxy.paths() == ["http://assets.test/font.css"]
    assert app_server.paths() == ["/"]  # the app itself is reached directly
    assert other_server.paths() == []  # and the other host only through the proxy


async def test_sign_in_success_and_failure(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    app_server.page("/", PAGE)
    app_server.page("/login", login_page("/session"))
    app_server.redirect("/session", "/home")
    app_server.page("/home", "<!doctype html><title>Home</title><h1>Signed in</h1>")
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    await sign_in(page, LOGIN, USERNAME, PASSWORD)
    assert urlsplit(page.url).path == "/home"
    (sent,) = [hit.path for hit in app_server.hits if hit.path.startswith("/session")]
    assert parse_qs(urlsplit(sent).query) == {"u": [USERNAME], "p": [PASSWORD]}

    app_server.page("/login", login_page("/login"))  # the app sends the form back: refused
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    with pytest.raises(SignInError) as caught:
        await sign_in(page, LOGIN, USERNAME, PASSWORD)
    assert 'still on "/login"' in str(caught.value)
    assert PASSWORD not in str(caught.value)


async def test_sign_in_without_the_fields_fails_without_the_password(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browsers, "SIGN_IN_TIMEOUT", 0.5)
    app_server.page("/", PAGE)
    app_server.page("/login", f"<!doctype html><title>{PASSWORD}</title><p>No form here")
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    with pytest.raises(SignInError) as caught:
        await sign_in(page, LOGIN, USERNAME, PASSWORD)
    assert "Email address" in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


# ---------------------------------------------------------------- the run's browsers


class _Recorder:
    """A browser type that records how it was asked to connect."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def connect(self, endpoint: str, **options: Any) -> str:
        self.calls.append((endpoint, options))
        if self.fail:
            raise PlaywrightError(f"WebSocket error: {endpoint} 400 Bad Request")
        return "a browser"


class _Playwright:
    def __init__(self, fail: bool = False) -> None:
        self.chromium = _Recorder(fail)
        self.webkit = _Recorder(fail)


async def test_chromium_is_asked_to_launch_without_encrypted_client_hello() -> None:
    fake = _Playwright()
    endpoint = "ws://10.89.0.12:3000/Zm9vYmFyYmF6"
    chromium: object = await connect_run_server(fake, "chromium", endpoint)  # type: ignore[arg-type]
    webkit: object = await connect_run_server(fake, "webkit", endpoint)  # type: ignore[arg-type]
    assert chromium == webkit == "a browser"
    ((url, options),) = fake.chromium.calls
    assert url == endpoint
    launch = json.loads(options["headers"]["x-playwright-launch-options"])
    assert launch == {"args": ["--disable-features=EncryptedClientHello"]}
    assert list(CHROMIUM_ARGS) == launch["args"]
    ((url, options),) = fake.webkit.calls
    assert url == endpoint
    assert json.loads(options["headers"]["x-playwright-launch-options"]) == {}


async def test_a_failed_connection_never_shows_the_endpoint() -> None:
    endpoint = "ws://10.89.0.12:3000/c2VjcmV0LXBhdGgtdG9rZW4"
    with pytest.raises(BrowserConnectError) as caught:
        await connect_run_server(_Playwright(fail=True), "chromium", endpoint)  # type: ignore[arg-type]
    assert "c2VjcmV0" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def run_server() -> Iterator[str]:
    """Playwright's own run-server on 127.0.0.1, on a secret path, as the runner starts it."""
    node, cli = compute_driver_executable()
    port = free_port()
    path = "/c2VjcmV0LXBhdGgtZm9yLXRoaXMtcnVu"
    command = [str(node), str(cli), "run-server", "--port", str(port), "--host", "127.0.0.1"]
    process = subprocess.Popen(
        [*command, "--path", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().decode().strip().endswith(path)
        yield f"ws://127.0.0.1:{port}{path}"
    finally:
        process.terminate()
        process.communicate(timeout=10)


async def test_only_the_secret_path_reaches_the_browsers(
    run_server: str, playwright: Playwright, app_server: AppServer
) -> None:
    app_server.page("/", PAGE)
    root = run_server.rsplit("/", 1)[0] + "/"
    with pytest.raises(BrowserConnectError):
        await connect_run_server(playwright, "chromium", root)
    browser = await connect_run_server(playwright, "chromium", run_server)
    try:
        page = await browser.new_page()
        await page.goto(app_server.origin + "/")
        assert await page.title() == "Plain"
    finally:
        await asyncio.wait_for(browser.close(), 30)
