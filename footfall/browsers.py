"""The tester's browsers: connecting to the run's, a context for each persona, and signing in.

Every setting a context has comes from the persona and its device, never from the app or the
page. Service workers are blocked, because their requests would bypass the guard. Requests to
any host but the app go to the egress proxy, which lets the browser reach only the run's asset
hosts. A persona's location is granted to the app's own origin, and only when the persona
allows it.

Browsers have no setting for text size, so a persona's text scale is applied to the page's
root font size. It is set through the CSSOM, because a page's content security policy can
refuse an added `<style>` element but cannot refuse a script that Playwright runs.
"""

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any, Final, Literal
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Browser, BrowserContext, ElementHandle, Locator, Page, Playwright
from playwright.async_api import Error as PlaywrightError

from footfall.manifest import LoginSection
from footfall.personas import Device, Persona
from footfall.tester.screen import quote

__all__ = [
    "CHROMIUM_LAUNCH",
    "LAUNCH_HEADER",
    "BrowserConnectError",
    "SignInError",
    "connect_run_server",
    "open_context",
    "potentially_trustworthy",
    "sign_in",
]

Engine = Literal["chromium", "webkit"]

# Chromium sends a GREASE Encrypted Client Hello extension, and the egress proxy refuses every
# ECH code point. In Chromium 158 no command-line switch turns it off; a managed policy, which
# the browser image installs, does. Playwright's headless shell reads no policies, so the run's
# Chromium is launched as full Chromium in its new headless mode. run-server passes `channel`
# on from any client, unlike `args`.
CHROMIUM_LAUNCH: Final = {"channel": "chromium"}
# How a client of Playwright's run-server passes its launch options.
LAUNCH_HEADER: Final = "x-playwright-launch-options"
CONNECT_TIMEOUT_MS: Final = 60_000
CONNECT_WAIT: Final = 60.0  # seconds to keep trying while the browser container starts
CONNECT_RETRY: Final = 0.5
# Read when used, so that a test can shorten it.
SIGN_IN_TIMEOUT = 15.0  # seconds for each step of signing in

# Scales the page's own root font size. Until the document has loaded, the root is set to the
# scale times the browser's default; once it has, and again once its stylesheets have, the
# page's own size is measured, with Footfall's taken off and the page's own inline size put
# back, and the root is set to that times the scale. `__SCALE__` is replaced with a number.
_TEXT_SCALE_JS: Final = """(() => {
  const scale = __SCALE__;
  let own = null;
  const root = () => document.documentElement;
  const remember = (element) => {
    if (own === null) {
      own = [element.style.getPropertyValue("font-size"),
             element.style.getPropertyPriority("font-size")];
    }
  };
  const set = (element, value) => element.style.setProperty("font-size", value, "important");
  const early = () => {
    const element = root();
    if (!element) return false;
    remember(element);
    set(element, "calc(100% * " + scale + ")");
    return true;
  };
  const fit = () => {
    const element = root();
    if (!element) return;
    remember(element);
    if (own[0]) element.style.setProperty("font-size", own[0], own[1]);
    else element.style.removeProperty("font-size");
    const size = parseFloat(getComputedStyle(element).fontSize);
    const base = Number.isFinite(size) && size > 0 ? size + "px" : "100%";
    set(element, "calc(" + base + " * " + scale + ")");
  };
  if (!early()) {
    const watch = new MutationObserver(() => { if (early()) watch.disconnect(); });
    watch.observe(document, { childList: true });
  }
  document.addEventListener("DOMContentLoaded", fit);
  window.addEventListener("load", fit);
})();"""


class SignInError(Exception):
    """Footfall could not sign a persona in. The message never holds the password."""


class BrowserConnectError(Exception):
    """The run's browsers could not be reached. The message never holds the endpoint."""


async def connect_run_server(
    playwright: Playwright, engine: str, endpoint: str, *, wait: float = CONNECT_WAIT
) -> Browser:
    """Connect to the run's browser container, which launches a browser for `engine`,
    "chromium" or "webkit".

    The container may still be starting, so a failed connection is tried again for up to
    `wait` seconds. The endpoint's path is the server's secret, so a failure is raised
    without it.
    """
    if engine not in ("chromium", "webkit"):
        raise ValueError(f"no such browser engine: {engine!r}")
    browser_type = playwright.chromium if engine == "chromium" else playwright.webkit
    options = CHROMIUM_LAUNCH if engine == "chromium" else {}
    headers = {LAUNCH_HEADER: json.dumps(options)}
    deadline = time.monotonic() + wait
    mismatch: str | None = None
    while True:
        try:
            return await browser_type.connect(endpoint, timeout=CONNECT_TIMEOUT_MS, headers=headers)
        except PlaywrightError as error:
            mismatch = _version_mismatch(str(error))
            if mismatch is not None or time.monotonic() >= deadline:
                break
        await asyncio.sleep(CONNECT_RETRY)
    if mismatch is not None:
        raise BrowserConnectError(mismatch) from None
    raise BrowserConnectError(f"could not connect to the run's {engine} browser") from None


def _version_mismatch(message: str) -> str | None:
    """What to say when run-server refused a client of another Playwright release, which no
    retry mends; None for any other failure. Only the versions are taken from the message,
    which also holds the endpoint."""
    if not re.search(r"\b428\b", message) or "version mismatch" not in message:
        return None
    server = re.search(r"server version: (v\d+(?:\.\d+)*)", message)
    client = re.search(r"client version: (v\d+(?:\.\d+)*)", message)
    if server is None or client is None:
        return "the run's browsers run another Playwright version than Footfall's"
    return (
        f"the run's browsers run Playwright {server.group(1)}, and Footfall runs "
        f"{client.group(1)}; the browser image and uv.lock must name the same version"
    )


async def open_context(
    browser: Browser,
    persona: Persona,
    device: Device,
    descriptors: Mapping[str, Any],
    app_origin: str,
    proxy_url: str,
) -> BrowserContext:
    """A browser context set up as the persona's device and settings.

    `descriptors` are Playwright's device descriptors, and `app_origin` is the app's
    `http://host:port`. Install the guard before the context opens its first page.
    """
    descriptor = {
        key: value
        for key, value in descriptors[device.playwright].items()
        if key != "default_browser_type"
    }
    host = urlsplit(app_origin).hostname or ""
    options: dict[str, Any] = {
        **descriptor,
        "locale": persona.locale,
        "timezone_id": persona.timezone,
        "color_scheme": persona.color_scheme,
        "reduced_motion": "reduce" if persona.reduced_motion else "no-preference",
        "service_workers": "block",
        "proxy": {"server": proxy_url, "bypass": host},
    }
    if persona.geolocation is not None:
        latitude, longitude = persona.geolocation
        options["geolocation"] = {"latitude": latitude, "longitude": longitude}
    context = await browser.new_context(**options)
    try:
        if (
            persona.geolocation is not None
            and persona.allow_location
            and potentially_trustworthy(app_origin)
        ):
            await context.grant_permissions(["geolocation"], origin=app_origin)
        if persona.text_scale != 1:
            scale = repr(float(persona.text_scale))
            await context.add_init_script(_TEXT_SCALE_JS.replace("__SCALE__", scale))
    except BaseException:
        await context.close()
        raise
    return context


def potentially_trustworthy(origin: str) -> bool:
    """Whether a page at `origin` is a secure context, where a browser lets it ask for the
    location. Chromium refuses to grant the permission anywhere else, and the page could not
    use it there, so the app reached at a container's plain-HTTP address is not granted it."""
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return True
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    try:
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        port = None
    return parts.scheme, (parts.hostname or ""), port


class _LeftTheApp(Exception):
    """An element Footfall would fill or press is in a document that is not the app's."""


async def _on_app(
    page: Page, locator: Locator, app_origin: str, timeout_ms: float
) -> ElementHandle:
    """The element `locator` finds, bound to the document it is in, which must be the app's
    page in the main frame. Filling or pressing a handle acts on that document alone, and
    raises once the page has moved on to another.

    Where the element is comes from the browser, through Playwright's record of the frame's
    address, never from page JavaScript, which a page can redefine to name the app.
    """
    handle = await locator.element_handle(timeout=timeout_ms)
    try:
        frame = await handle.owner_frame()
        if frame is not page.main_frame or _origin(frame.url) != _origin(app_origin):
            raise _LeftTheApp
    except BaseException:
        with contextlib.suppress(PlaywrightError):
            await handle.dispose()
        raise
    return handle


def _still_on_app(page: Page, app_origin: str) -> None:
    if _origin(page.main_frame.url) != _origin(app_origin):
        raise _LeftTheApp


async def sign_in(
    page: Page, login: LoginSection, username: str, password: str, app_origin: str
) -> None:
    """Sign in on the app's sign-in page, as its manifest describes it.

    Opens `login.page` on `app_origin`, finds the two fields by their labels and the button
    by its name, fills the fields and presses the button. A redirect hop is not routed, and
    a page can send the browser elsewhere at any moment, so a page that has left the app
    must never be given the account. Each element is therefore taken as a handle on its own
    document, which must be in the main frame while that frame is on the app, as the browser
    reports it; the page is checked again before each field is filled and before the button
    is pressed, and a handle cannot fill or press anything once its page has moved on.
    Raises SignInError if any step fails, if any element is not on the app, if pressing the
    button leaves the app, or if the page is still the sign-in page once the app has
    answered. No error carries a browser message, since one can quote the page or what was
    typed.
    """
    timeout_ms = SIGN_IN_TIMEOUT * 1000
    target = urljoin(app_origin + "/", login.page)
    step = f"open {quote(login.page, 80)}"
    failure: SignInError | None = None
    handles: list[ElementHandle] = []
    try:
        await page.goto(target, timeout=timeout_ms)
        if _origin(page.url) != _origin(app_origin):
            raise _LeftTheApp
        step = f"find the field labelled {quote(login.username_label, 80)}"
        user = page.get_by_label(login.username_label, exact=True)
        handles.append(await _on_app(page, user, app_origin, timeout_ms))
        step = f"find the field labelled {quote(login.password_label, 80)}"
        secret = page.get_by_label(login.password_label, exact=True)
        handles.append(await _on_app(page, secret, app_origin, timeout_ms))
        step = f"find the button {quote(login.submit, 80)}"
        button = page.get_by_role("button", name=login.submit, exact=True)
        handles.append(await _on_app(page, button, app_origin, timeout_ms))
        step = f"fill the field labelled {quote(login.username_label, 80)}"
        _still_on_app(page, app_origin)
        await handles[0].fill(username, timeout=timeout_ms)
        step = f"fill the field labelled {quote(login.password_label, 80)}"
        _still_on_app(page, app_origin)
        await handles[1].fill(password, timeout=timeout_ms)
        step = f"press the button {quote(login.submit, 80)}"
        _still_on_app(page, app_origin)
        async with page.expect_navigation(timeout=timeout_ms):
            await handles[2].click(timeout=timeout_ms)
    except _LeftTheApp:
        failure = SignInError(f"could not sign in: {quote(login.page, 80)} left the app")
    except PlaywrightError:
        failure = SignInError(f"could not sign in: could not {step}")
    finally:
        for handle in handles:
            with contextlib.suppress(PlaywrightError):
                await handle.dispose()
    if failure is None:
        if _origin(page.url) != _origin(app_origin):
            failure = SignInError(
                f"could not sign in: pressing {quote(login.submit, 80)} left the app"
            )
        elif urlsplit(page.url).path == urlsplit(target).path:
            failure = SignInError(
                f"could not sign in: still on {quote(login.page, 80)} after pressing "
                f"{quote(login.submit, 80)}"
            )
    if failure is not None:
        raise failure from None
