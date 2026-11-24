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
import json
import time
from collections.abc import Mapping
from typing import Any, Final, Literal
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright
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
    while True:
        try:
            return await browser_type.connect(endpoint, timeout=CONNECT_TIMEOUT_MS, headers=headers)
        except PlaywrightError:
            if time.monotonic() >= deadline:
                break
        await asyncio.sleep(CONNECT_RETRY)
    raise BrowserConnectError(f"could not connect to the run's {engine} browser") from None


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
        if persona.geolocation is not None and persona.allow_location:
            await context.grant_permissions(["geolocation"], origin=app_origin)
        if persona.text_scale != 1:
            scale = repr(float(persona.text_scale))
            await context.add_init_script(_TEXT_SCALE_JS.replace("__SCALE__", scale))
    except BaseException:
        await context.close()
        raise
    return context


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    try:
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        port = None
    return parts.scheme, (parts.hostname or ""), port


async def sign_in(
    page: Page, login: LoginSection, username: str, password: str, app_origin: str
) -> None:
    """Sign in on the app's sign-in page, as its manifest describes it.

    Opens `login.page` on `app_origin`, fills the two fields by their labels and presses the
    button by its name. Raises SignInError if any step fails, if the page is not on the app
    when a field would be filled, or if it is still the sign-in page once the app has
    answered. A redirect hop is not routed, so the sign-in page could send the browser to an
    asset host, which must never be given the account. No error carries a browser message,
    since one can quote the page or what was typed.
    """
    timeout_ms = SIGN_IN_TIMEOUT * 1000
    target = urljoin(app_origin + "/", login.page)
    step = f"open {quote(login.page, 80)}"
    failure: SignInError | None = None
    try:
        await page.goto(target, timeout=timeout_ms)
        if _origin(page.url) != _origin(app_origin):
            failure = SignInError(f"could not sign in: {quote(login.page, 80)} left the app")
        else:
            step = f"fill the field labelled {quote(login.username_label, 80)}"
            await page.get_by_label(login.username_label, exact=True).fill(
                username, timeout=timeout_ms
            )
            step = f"fill the field labelled {quote(login.password_label, 80)}"
            await page.get_by_label(login.password_label, exact=True).fill(
                password, timeout=timeout_ms
            )
            step = f"press the button {quote(login.submit, 80)}"
            button = page.get_by_role("button", name=login.submit, exact=True)
            async with page.expect_navigation(timeout=timeout_ms):
                await button.click(timeout=timeout_ms)
    except PlaywrightError:
        failure = SignInError(f"could not sign in: could not {step}")
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
