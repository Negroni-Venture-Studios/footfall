"""Browser contexts made for a persona, signing in, and connecting to the run's browsers."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Coroutine
from dataclasses import replace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from footfall import browsers
from footfall.browsers import (
    CHROMIUM_LAUNCH,
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
    _PERSONAS["low-vision"],
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
    await sign_in(page, LOGIN, USERNAME, PASSWORD, app_server.origin)
    assert urlsplit(page.url).path == "/home"
    (sent,) = [hit.path for hit in app_server.hits if hit.path.startswith("/session")]
    assert parse_qs(urlsplit(sent).query) == {"u": [USERNAME], "p": [PASSWORD]}

    app_server.page("/login", login_page("/login"))  # the app sends the form back: refused
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    with pytest.raises(SignInError) as caught:
        await sign_in(page, LOGIN, USERNAME, PASSWORD, app_server.origin)
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
        await sign_in(page, LOGIN, USERNAME, PASSWORD, app_server.origin)
    assert "Email address" in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


# ---------------------------------------------------------------- the run's browsers


class _Recorder:
    """A browser type that records how it was asked to connect, and fails `failures` times."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures

    async def connect(self, endpoint: str, **options: Any) -> str:
        self.calls.append((endpoint, options))
        if len(self.calls) <= self.failures:
            raise PlaywrightError(f"WebSocket error: {endpoint} 400 Bad Request")
        return "a browser"


class _Playwright:
    def __init__(self, failures: int = 0) -> None:
        self.chromium = _Recorder(failures)
        self.webkit = _Recorder(failures)


async def test_chromium_is_launched_as_the_build_that_reads_policies() -> None:
    # Only full Chromium reads the image's policy that turns Encrypted Client Hello off; the
    # headless shell reads none, and no command-line switch turns it off in Chromium 158.
    fake = _Playwright()
    endpoint = "ws://10.89.0.12:3000/Zm9vYmFyYmF6"
    chromium: object = await connect_run_server(fake, "chromium", endpoint)  # type: ignore[arg-type]
    webkit: object = await connect_run_server(fake, "webkit", endpoint)  # type: ignore[arg-type]
    assert chromium == webkit == "a browser"
    ((url, options),) = fake.chromium.calls
    assert url == endpoint
    launch = json.loads(options["headers"]["x-playwright-launch-options"])
    assert launch == {"channel": "chromium"} == CHROMIUM_LAUNCH
    ((url, options),) = fake.webkit.calls
    assert url == endpoint
    assert json.loads(options["headers"]["x-playwright-launch-options"]) == {}


async def test_the_connection_never_exposes_the_orchestrators_network() -> None:
    # expose_network would let the run's browser container open sockets from the
    # orchestrator's side, the one thing between a hostile run-server and the orchestrator's
    # network. The options are pinned whole, so that no new one arrives unseen.
    fake = _Playwright()
    endpoint = "ws://10.89.0.12:3000/Zm9vYmFyYmF6"
    await connect_run_server(fake, "chromium", endpoint)  # type: ignore[arg-type]
    await connect_run_server(fake, "webkit", endpoint)  # type: ignore[arg-type]
    for _, options in [*fake.chromium.calls, *fake.webkit.calls]:
        assert "expose_network" not in options
        assert set(options) == {"timeout", "headers"}


async def test_a_connection_is_tried_again_until_the_browsers_answer() -> None:
    fake = _Playwright(failures=2)
    endpoint = "ws://10.89.0.12:3000/Zm9vYmFyYmF6"
    browser: object = await connect_run_server(fake, "chromium", endpoint, wait=5)  # type: ignore[arg-type]
    assert browser == "a browser"
    assert len(fake.chromium.calls) == 3


async def test_a_failed_connection_never_shows_the_endpoint() -> None:
    endpoint = "ws://10.89.0.12:3000/c2VjcmV0LXBhdGgtdG9rZW4"
    fake = _Playwright(failures=1000)
    with pytest.raises(BrowserConnectError) as caught:
        await connect_run_server(fake, "chromium", endpoint, wait=0.5)  # type: ignore[arg-type]
    assert len(fake.chromium.calls) > 1
    assert "c2VjcmV0" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


async def test_sign_in_fills_nothing_on_a_page_off_the_app(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    other_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    # A redirect hop is not routed, so the sign-in page can send the browser to another host,
    # here one of the assets, which must never be given the account.
    app_server.page("/", PAGE)
    app_server.redirect("/login", f"http://assets.test:{other_server.port}/login")
    other_server.page("/login", login_page("/session"))
    context = await persona_context(
        loopback_browser, playwright, app_server, contexts, proxy=other_server.origin
    )
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    with pytest.raises(SignInError) as caught:
        await sign_in(page, LOGIN, USERNAME, PASSWORD, app_server.origin)
    assert "left the app" in str(caught.value)
    assert [hit.path for hit in other_server.hits] == [
        f"http://assets.test:{other_server.port}/login"
    ]
    assert await page.locator("#u").input_value() == ""


async def test_the_text_scale_follows_the_pages_own_root_size(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    # A site that sets its root to 62.5%, for rem units of 10px, grows by the persona's scale,
    # not to 1.75 times the browser's 16px.
    app_server.reply(
        "/",
        '<!doctype html><meta name="viewport" content="width=device-width">'
        '<link rel="stylesheet" href="/site.css"><title>Rem</title><h1>Rem</h1>',
        headers=STRICT,
    )
    app_server.reply("/site.css", "html { font-size: 62.5% }", headers={"Content-Type": "text/css"})
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    sizes = await page.evaluate(
        """() => [getComputedStyle(document.documentElement).fontSize,
                  getComputedStyle(document.querySelector("h1")).fontSize,
                  document.querySelectorAll("style").length]"""
    )
    assert sizes == ["17.5px", "35px", 0]


async def test_sign_in_cannot_be_outrun_by_a_page_that_moves_on(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    other_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    # The sign-in page passes the origin check, then sends the browser elsewhere a moment
    # later, through a redirect hop, to a page with the same form: nothing is filled there.
    app_server.page("/", PAGE)
    app_server.page(
        "/login",
        "<!doctype html><title>Sign in</title><p>Loading"
        '<script>setTimeout(() => { location.href = "/bounce"; }, 200)</script>',
    )
    app_server.redirect("/bounce", f"{other_server.origin}/login")
    other_server.page("/login", login_page("/session"))
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    with pytest.raises(SignInError) as caught:
        await sign_in(page, LOGIN, USERNAME, PASSWORD, app_server.origin)
    assert "left the app" in str(caught.value)
    assert not any(hit.path.startswith("/session") for hit in other_server.hits)
    assert await page.locator("#u").input_value() == ""
    assert PASSWORD not in str(caught.value)


MISMATCH = (
    "BrowserType.connect: WebSocket error: ws://10.89.0.12:3000/c2VjcmV0LXBhdGgtdG9rZW4 "
    "428 Precondition Required\n"
    "╔══════════════════════════════════════════╗\n"
    "║ Playwright version mismatch:             ║\n"
    "║   - server version: v1.65                ║\n"
    "║   - client version: v1.66                ║\n"
    "╚══════════════════════════════════════════╝"
)


class _Mismatched:
    def __init__(self) -> None:
        self.calls = 0

    async def connect(self, endpoint: str, **options: Any) -> str:
        self.calls += 1
        raise PlaywrightError(MISMATCH)


async def test_a_version_mismatch_fails_at_once_and_names_both_versions() -> None:
    fake = _Playwright()
    mismatched = _Mismatched()
    fake.chromium = mismatched  # type: ignore[assignment]
    started = time.monotonic()
    with pytest.raises(BrowserConnectError) as caught:
        await connect_run_server(
            fake,  # type: ignore[arg-type]
            "chromium",
            "ws://10.89.0.12:3000/c2VjcmV0LXBhdGgtdG9rZW4",
            wait=30,
        )
    assert time.monotonic() - started < 1
    assert mismatched.calls == 1
    message = str(caught.value)
    assert "v1.65" in message and "v1.66" in message
    assert "version" in message
    assert "c2VjcmV0" not in message


async def test_sign_in_believes_the_browser_not_the_page_about_where_it_is(
    loopback_browser: Browser,
    playwright: Playwright,
    app_server: AppServer,
    other_server: AppServer,
    contexts: list[BrowserContext],
) -> None:
    # The page Footfall is sent to claims, through the DOM, to be the app.
    app_server.page("/", PAGE)
    app_server.page(
        "/login",
        "<!doctype html><title>Sign in</title><p>Loading"
        f'<script>setTimeout(() => {{ location.href = "{other_server.origin}/login"; }}, 200)'
        "</script>",
    )
    # The real document in every respect but its location, so that the element still has a
    # frame, while page JavaScript asking for its document's origin is told the app's.
    spoof = (
        "<script>(() => {"
        "const real = Object.getOwnPropertyDescriptor(Node.prototype, 'ownerDocument').get;"
        "Object.defineProperty(Node.prototype, 'ownerDocument', {get() {"
        "const doc = real.call(this); if (!doc) return doc;"
        "return new Proxy(doc, {get(target, key) {"
        f"if (key === 'location') return {{origin: '{app_server.origin}'}};"
        "const value = Reflect.get(target, key);"
        "return typeof value === 'function' ? value.bind(target) : value; }}); }});"
        "})();</script>"
    )
    other_server.page("/login", spoof + login_page("/session"))
    context = await persona_context(loopback_browser, playwright, app_server, contexts)
    page = await context.new_page()
    await page.goto(app_server.origin + "/")
    with pytest.raises(SignInError, match="left the app"):
        await sign_in(page, LOGIN, USERNAME, PASSWORD, app_server.origin)
    assert not any(hit.path.startswith("/session") for hit in other_server.hits)
    assert await page.locator("#u").input_value() == ""


async def test_a_handle_is_let_go_when_its_frame_cannot_be_read() -> None:
    disposed: list[bool] = []

    class Handle:
        async def owner_frame(self) -> None:
            raise PlaywrightError("Target closed")

        async def dispose(self) -> None:
            disposed.append(True)

    class Locator:
        async def element_handle(self, timeout: float) -> Handle:
            return Handle()

    class Page:
        main_frame = object()

    with pytest.raises(PlaywrightError):
        await browsers._on_app(Page(), Locator(), "http://10.89.0.11:8080", 1000)  # type: ignore[arg-type]
    assert disposed == [True]


APP = "http://10.89.0.11:8080"


class _Frame:
    """A main frame whose address can be set to change after it has been read `reads` times."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.moves_after: int | None = None

    @property
    def url(self) -> str:
        if self.moves_after is not None:
            if self.moves_after == 0:
                self._url, self.moves_after = "http://10.89.0.99:8080/login", None
            else:
                self.moves_after -= 1
        return self._url


class _Handle:
    def __init__(self, page: "_FakePage", role: str) -> None:
        self.page = page
        self.role = role

    async def owner_frame(self) -> _Frame:
        frame = self.page.main_frame
        if self.role == "button":
            # The page moves on once the last handle has been taken and checked: the check
            # reads the address once more, and then it is elsewhere.
            frame.moves_after = 1
        return frame

    async def fill(self, value: str, timeout: float) -> None:
        self.page.acted.append(("fill", self.role))

    async def click(self, timeout: float) -> None:
        self.page.acted.append(("click", self.role))

    async def dispose(self) -> None:
        pass


class _Locator:
    def __init__(self, page: "_FakePage", role: str) -> None:
        self.page = page
        self.role = role

    async def element_handle(self, timeout: float) -> _Handle:
        return _Handle(self.page, self.role)


class _FakePage:
    def __init__(self) -> None:
        self.main_frame = _Frame(APP + "/")
        self.acted: list[tuple[str, str]] = []

    @property
    def url(self) -> str:
        return self.main_frame.url

    async def goto(self, url: str, timeout: float) -> None:
        self.main_frame = _Frame(url)

    def get_by_label(self, label: str, exact: bool) -> _Locator:
        return _Locator(self, label)

    def get_by_role(self, role: str, name: str, exact: bool) -> _Locator:
        return _Locator(self, role)


def test_still_on_app_refuses_a_main_frame_off_the_app() -> None:
    page = _FakePage()
    browsers._still_on_app(page, APP)  # type: ignore[arg-type]
    page.main_frame.moves_after = 0
    with pytest.raises(browsers._LeftTheApp):
        browsers._still_on_app(page, APP)  # type: ignore[arg-type]


async def test_sign_in_fills_nothing_once_the_page_has_moved_on() -> None:
    # Every handle was taken while the page was on the app; then the main frame left it.
    page = _FakePage()
    with pytest.raises(SignInError, match="left the app"):
        await sign_in(page, LOGIN, USERNAME, PASSWORD, APP)  # type: ignore[arg-type]
    assert page.acted == []


@pytest.mark.parametrize(
    ("origin", "secure"),
    [
        ("http://127.0.0.1:8000", True),
        ("http://localhost:8000", True),
        ("http://app.localhost:8000", True),
        ("http://[::1]:8000", True),
        ("http://127.8.9.10:8000", True),  # all of 127.0.0.0/8 is loopback, as Chromium has it
        ("http://127.0.0.1.example.com", False),
        ("http://128.0.0.1:8000", False),
        ("https://172.18.0.3:8443", True),
        # How the app is reached in the real stack: Chromium refuses to grant a permission
        # there ("Permission can't be granted in current context"), and the page could not
        # use the location anyway (found in the isolation workflow).
        ("http://172.18.0.3:8080", False),
        ("http://example.com", False),
    ],
)
def test_location_is_granted_only_to_an_origin_that_could_use_it(origin: str, secure: bool) -> None:
    assert browsers.potentially_trustworthy(origin) is secure


class _RecordingContext:
    """Stands in for a browser context: records what open_context asks of it."""

    def __init__(self) -> None:
        self.granted: list[tuple[list[str], str]] = []
        self.scripts: list[str] = []

    async def grant_permissions(self, permissions: list[str], origin: str) -> None:
        self.granted.append((permissions, origin))

    async def add_init_script(self, script: str) -> None:
        self.scripts.append(script)

    async def close(self) -> None:
        return None


class _RecordingBrowser:
    def __init__(self) -> None:
        self.context = _RecordingContext()
        self.options: dict[str, Any] = {}

    async def new_context(self, **options: Any) -> _RecordingContext:
        self.options = options
        return self.context


@pytest.mark.parametrize(
    ("origin", "granted"),
    [("http://172.18.0.3:8080", False), ("http://127.0.0.1:8080", True)],
)
async def test_open_context_grants_the_location_only_where_it_can_be_used(
    origin: str, granted: bool
) -> None:
    # The app in the real stack is plain HTTP at a container's address, where Chromium
    # refuses to grant the permission; the context still opens, with the location set.
    browser = _RecordingBrowser()
    descriptors = {PIXEL.playwright: {"viewport": {"width": 412, "height": 839}}}
    context = await open_context(
        cast(Browser, browser), PERSONA, PIXEL, descriptors, origin, "http://proxy:3128"
    )
    assert context is cast(BrowserContext, browser.context)
    assert browser.options["geolocation"] == {"latitude": -33.8688, "longitude": 151.2093}
    expected = [(["geolocation"], origin)] if granted else []
    assert browser.context.granted == expected


# ---------------------------------------------------------------- clean-up past the hard limit


async def never() -> None:
    """A browser container that has stopped answering: the call never returns."""
    await asyncio.Event().wait()


async def ends_within(seconds: float, work: Coroutine[Any, Any, Any]) -> BaseException | None:
    """Run `work` under a hard limit of 0.2 s, as a persona's, and say what it raised; fail
    unless it has ended, clean-up included, within `seconds`."""

    async def limited() -> None:
        async with asyncio.timeout(0.2):
            await work

    task = asyncio.create_task(limited())
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.cancel()
        pytest.fail(f"the clean-up was still waiting {seconds:g} s after the hard limit")
    return task.exception()


@pytest.fixture
def short_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browsers, "CLOSE_TIMEOUT", 0.2)


async def test_a_context_that_never_closes_does_not_hold_open_contexts_clean_up(
    short_close: None,
) -> None:
    """Part B, Minor 2: once the hard limit has cancelled open_context, its clean-up must
    not wait without end on a context whose grant and close never return."""
    browser = _RecordingBrowser()
    browser.context.grant_permissions = lambda *a, **k: never()  # type: ignore[method-assign]
    browser.context.close = never  # type: ignore[method-assign]
    descriptors: dict[str, Any] = {PIXEL.playwright: {}}
    opening = open_context(
        cast(Browser, browser), PERSONA, PIXEL, descriptors, "http://127.0.0.1:8080", "http://p:1"
    )
    assert isinstance(await ends_within(3, opening), TimeoutError)


async def test_a_handle_that_never_lets_go_does_not_hold_on_apps_clean_up(
    short_close: None,
) -> None:
    class Handle:
        async def owner_frame(self) -> None:
            await never()

        dispose = staticmethod(never)

    class Locator:
        async def element_handle(self, timeout: float) -> Handle:
            return Handle()

    class Page:
        main_frame = object()

    taking = browsers._on_app(Page(), Locator(), APP, 1000)  # type: ignore[arg-type]
    assert isinstance(await ends_within(3, taking), TimeoutError)


class _Stuck(_Handle):
    """A handle on the app whose fill and dispose never return."""

    async def owner_frame(self) -> _Frame:
        return self.page.main_frame

    async def fill(self, value: str, timeout: float) -> None:
        await never()

    async def dispose(self) -> None:
        await never()


class _StuckPage(_FakePage):
    def get_by_label(self, label: str, exact: bool) -> _Locator:
        return cast(_Locator, _StuckLocator(self, label))

    def get_by_role(self, role: str, name: str, exact: bool) -> _Locator:
        return cast(_Locator, _StuckLocator(self, role))


class _StuckLocator(_Locator):
    async def element_handle(self, timeout: float) -> _Handle:
        return _Stuck(self.page, self.role)


async def test_handles_that_never_let_go_do_not_hold_sign_ins_clean_up(short_close: None) -> None:
    signing = sign_in(_StuckPage(), LOGIN, USERNAME, PASSWORD, APP)  # type: ignore[arg-type]
    assert isinstance(await ends_within(3, signing), TimeoutError)
