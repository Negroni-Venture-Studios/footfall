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

import json
from collections.abc import Mapping
from typing import Any, Final, Literal
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright.async_api import Error as PlaywrightError

from footfall.manifest import LoginSection
from footfall.personas import Device, Persona
from footfall.tester.screen import quote

__all__ = [
    "CHROMIUM_ARGS",
    "LAUNCH_HEADER",
    "BrowserConnectError",
    "SignInError",
    "connect_run_server",
    "open_context",
    "sign_in",
]

Engine = Literal["chromium", "webkit"]

# Chromium sends a GREASE Encrypted Client Hello extension, and the egress proxy refuses every
# ECH code point, so the run's Chromium is asked to launch without it. As it stands this has
# no effect: run-server passes a client's `args` on only when it was started with `--unsafe`,
# which the runner does not do, and Chromium 158 sends the extension with this switch anyway.
CHROMIUM_ARGS: Final = ("--disable-features=EncryptedClientHello",)
# How a client of Playwright's run-server passes its launch options.
LAUNCH_HEADER: Final = "x-playwright-launch-options"
CONNECT_TIMEOUT_MS: Final = 60_000
# Read when used, so that a test can shorten it.
SIGN_IN_TIMEOUT = 15.0  # seconds for each step of signing in

# Sets the root font size, whatever the page's own styles say, as soon as there is a root and
# again once the document has loaded. `__SCALE__` is replaced with a number.
_TEXT_SCALE_JS: Final = """(() => {
  const apply = () => {
    const root = document.documentElement;
    if (root) root.style.setProperty("font-size", "calc(100% * __SCALE__)", "important");
    return Boolean(root);
  };
  if (!apply()) {
    const watch = new MutationObserver(() => { if (apply()) watch.disconnect(); });
    watch.observe(document, { childList: true });
  }
  document.addEventListener("DOMContentLoaded", apply);
})();"""


class SignInError(Exception):
    """Footfall could not sign a persona in. The message never holds the password."""


class BrowserConnectError(Exception):
    """The run's browsers could not be reached. The message never holds the endpoint."""


async def connect_run_server(playwright: Playwright, engine: str, endpoint: str) -> Browser:
    """Connect to the run's browser container, which launches a browser for `engine`,
    "chromium" or "webkit".

    The endpoint's path is the server's secret, so a failure is raised without it.
    """
    if engine not in ("chromium", "webkit"):
        raise ValueError(f"no such browser engine: {engine!r}")
    browser_type = playwright.chromium if engine == "chromium" else playwright.webkit
    options = {"args": list(CHROMIUM_ARGS)} if engine == "chromium" else {}
    try:
        return await browser_type.connect(
            endpoint, timeout=CONNECT_TIMEOUT_MS, headers={LAUNCH_HEADER: json.dumps(options)}
        )
    except PlaywrightError:
        failure = BrowserConnectError(f"could not connect to the run's {engine} browser")
    raise failure from None


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


async def sign_in(page: Page, login: LoginSection, username: str, password: str) -> None:
    """Sign in on the app's sign-in page, as its manifest describes it.

    Opens `login.page` on the page's own origin, fills the two fields by their labels and
    presses the button by its name. Raises SignInError if any step fails, or if the page is
    still the sign-in page once the app has answered. No error carries a browser message,
    since one can quote the page or what was typed.
    """
    timeout_ms = SIGN_IN_TIMEOUT * 1000
    target = urljoin(page.url, login.page)
    step = f"open {quote(login.page, 80)}"
    try:
        await page.goto(target, timeout=timeout_ms)
        step = f"fill the field labelled {quote(login.username_label, 80)}"
        await page.get_by_label(login.username_label, exact=True).fill(username, timeout=timeout_ms)
        step = f"fill the field labelled {quote(login.password_label, 80)}"
        await page.get_by_label(login.password_label, exact=True).fill(password, timeout=timeout_ms)
        step = f"press the button {quote(login.submit, 80)}"
        button = page.get_by_role("button", name=login.submit, exact=True)
        async with page.expect_navigation(timeout=timeout_ms):
            await button.click(timeout=timeout_ms)
    except PlaywrightError:
        failure = SignInError(f"could not sign in: could not {step}")
    else:
        if urlsplit(page.url).path != urlsplit(target).path:
            return
        failure = SignInError(
            f"could not sign in: still on {quote(login.page, 80)} after pressing "
            f"{quote(login.submit, 80)}"
        )
    raise failure from None
