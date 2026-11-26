"""Keeping the tester's browser on the app.

The browser may load the app's own origin, the asset hosts the manifest names in
`browser.assets`, and `data:`, `blob:` and `about:blank`. Its main frame may show pages
from the app only. Everything else is refused, and what the page tried is recorded as a
`GuardEvent` for the tester's next result (AGENTS.md rule 4).

The guard works in three ways:

1. `context.route` aborts every request that breaks the rule before it leaves the browser.
2. `context.route_web_socket` closes every WebSocket that breaks it.
3. Some requests never reach a route handler: Playwright continues each hop of a redirect
   itself, and a worker's WebSocket is not routed. The guard watches for these and reports
   them. What refuses them is the egress proxy, through which the browser reaches every
   host except the app, and which lets the browser reach only the asset hosts.

Popups are closed, downloads cancelled, dialogs dismissed (a page that asks before it is
left is let go) and file pickers left unanswered, and each is recorded. Everything in an
event that came from the page is page text: it is cleaned, cut to `DETAIL_CAP` and
quoted, and a blocked address is reported by its host alone, never its path or query
(AGENTS.md rule 6).

The guard has known gaps, and the egress proxy and the container's network cover the first three:

- In Chromium, a request made as a page unloads, such as a `sendBeacon` or a `keepalive`
  fetch from a `pagehide` or `unload` handler, has no frame left; Playwright continues it
  without routing it and without a request event, so the guard never sees it. WebKit routes
  such a request, and the guard blocks it (shown by the isolation tests).
- `<link rel=preconnect>` and `<link rel=dns-prefetch>` open a connection or look a name up
  without making a request, so there is nothing to route.
- WebRTC sends UDP, which is not HTTP and is not routed.
- A service worker's requests bypass `context.route`. The guard holds only for a context
  made with `service_workers="block"`, which `footfall test` sets.
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import SplitResult, urlsplit

from playwright.async_api import (
    BrowserContext,
    Dialog,
    Download,
    FileChooser,
    Page,
    Request,
    Route,
    WebSocket,
    WebSocketRoute,
)
from playwright.async_api import Error as PlaywrightError

from footfall.tester.report import clean_text
from footfall.tester.screen import quote

__all__ = ["DETAIL_CAP", "MAX_EVENTS", "Guard", "GuardEvent", "Kind"]

Kind = Literal["blocked_request", "popup", "download", "dialog", "file_chooser"]

MAX_EVENTS: Final = 20  # distinct events kept between two drains; the rest are counted
DETAIL_CAP: Final = 200
LOG = logging.getLogger(__name__)

_DEFAULT_PORTS: Final = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_AS_HTTP: Final = {"http": "http", "https": "https", "ws": "http", "wss": "https"}
_FURTHER: Final[dict[Kind, str]] = {
    "blocked_request": "blocked requests to further hosts",
    "popup": "closed further new windows",
    "download": "cancelled further downloads",
    "dialog": "dismissed further dialogs",
    "file_chooser": "left further file pickers unanswered",
}
_DIALOGS: Final = {"alert": "an alert", "confirm": "a confirm dialog", "prompt": "a prompt"}
_CLOSE_POLICY: Final = 1008  # WebSocket close code: policy violation


@dataclass(frozen=True)
class GuardEvent:
    """Something the guard stopped or saw. `detail` is one line, safe to show the model."""

    kind: Kind
    detail: str
    count: int = 1


@dataclass(frozen=True)
class _Address:
    scheme: str  # as written: http, https, ws or wss
    origin: tuple[str, str, int]  # (http or https, host, port)


def _split(url: str) -> tuple[SplitResult, int | None] | None:
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:  # a bad port or a bad IPv6 literal
        return None


def _address(url: str) -> _Address | None:
    split = _split(url)
    if split is None:
        return None
    parts, port = split
    scheme = _AS_HTTP.get(parts.scheme)
    if scheme is None or not parts.hostname:
        return None
    if port is None:
        port = _DEFAULT_PORTS[parts.scheme]
    return _Address(parts.scheme, (scheme, parts.hostname, port))


def _where(url: str) -> str:
    """Where a URL points, for the model: its host, quoted, and never its path or query."""
    split = _split(url)
    if split is None:
        return "an address that could not be read"
    parts, port = split
    if parts.scheme in _DEFAULT_PORTS and parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
            host = f"{host}:{port}"
        return quote(host, DETAIL_CAP)
    return f"a {quote(parts.scheme + ':', 40)} address" if parts.scheme else "an unknown address"


def _parse_origin(text: str) -> tuple[str, tuple[str, str, int]]:
    problem = f"the app origin must be http or https with a host and nothing else: {text!r}"
    split = _split(text)
    if split is None:
        raise ValueError(problem)
    parts, _ = split
    address = _address(text)
    if (
        address is None
        or parts.scheme not in ("http", "https")
        or parts.path not in ("", "/")
        or "?" in text
        or "#" in text
        or "@" in parts.netloc
    ):
        raise ValueError(problem)
    return f"{parts.scheme}://{parts.netloc.lower()}", address.origin


async def _quietly(action: Awaitable[object], what: str) -> None:
    """Await a browser call whose page may already be gone."""
    try:
        await action
    except PlaywrightError as error:
        LOG.debug("could not %s: %s", what, error)


class Guard:
    """The allow rule for one browser context, and the record of what it refused."""

    def __init__(
        self,
        app_origin: str,
        assets: frozenset[str],
        *,
        redact: Callable[[str], str] = lambda text: text,
    ) -> None:
        """`redact` takes the run's secrets out of page text, a dialog's message, say, after
        it is cleaned and before it is cut, so that no cut leaves part of one."""
        self.origin, self._app = _parse_origin(app_origin)
        self._redact = redact
        self._assets = frozenset(host.lower() for host in assets)
        self._events: dict[tuple[Kind, str], int] = {}
        self._distinct = 0
        self._installed = False
        self._main: Page | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ the rule

    def allowed(self, url: str) -> bool:
        """Whether the browser may load `url` at all."""
        if url == "about:blank" or url.startswith(("data:", "blob:")):
            return True
        address = _address(url)
        if address is None:
            return False
        return address.origin == self._app or address.origin[1] in self._assets

    def on_app(self, url: str) -> bool:
        """Whether `url` is a page on the app itself."""
        address = _address(url)
        return (
            address is not None
            and address.scheme in ("http", "https")
            and address.origin == self._app
        )

    def _permitted(self, request: Request) -> bool:
        url = request.url
        if not self.allowed(url):
            return False
        if request.is_navigation_request() and _in_main_frame(request):
            return self.on_app(url)
        return True

    # ------------------------------------------------------------------ the record

    def _quote(self, text: str) -> str:
        """Page text for a detail: made one line, redacted, then cut and quoted."""
        return quote(self._redact(clean_text(text, sys.maxsize, single_line=True)), DETAIL_CAP)

    def record(self, kind: Kind, detail: str) -> None:
        """Add an event. Repeats are counted; past `MAX_EVENTS`, new ones are summed by kind."""
        key = (kind, detail)
        if key not in self._events:
            if self._distinct >= MAX_EVENTS:
                key = (kind, _FURTHER[kind])
            else:
                self._distinct += 1
        if key not in self._events:
            LOG.info("guard: %s", _loggable(*key))
        self._events[key] = self._events.get(key, 0) + 1

    def drain(self) -> list[GuardEvent]:
        """Return the events since the last drain, oldest first, and forget them."""
        events = [GuardEvent(kind, detail, count) for (kind, detail), count in self._events.items()]
        for event in events:
            if event.count > 1:
                what = _loggable(event.kind, event.detail)
                LOG.info("guard: %s, %d times in all", what, event.count)
        self._events = {}
        self._distinct = 0
        return events

    # ------------------------------------------------------------------ the browser

    async def install(self, context: BrowserContext) -> None:
        """Guard every page in `context`.

        Install before the context opens its first page: WebSockets created earlier are not
        routed, and requests made earlier were not checked. The first page the guard sees
        is the tester's page; every page after it is a popup and is closed.
        """
        if self._installed:
            raise RuntimeError("this guard is already installed on a context")
        self._installed = True
        await context.route("**/*", self._route)
        await context.route_web_socket(lambda _url: True, self._route_socket)
        context.on("request", self._watch_request)
        context.on("dialog", self._dismiss)
        context.on("page", self._watch_page)
        for page in context.pages:
            self._watch_page(page)

    async def _route(self, route: Route) -> None:
        request = route.request
        try:
            permitted = self._permitted(request)
        except PlaywrightError as error:
            LOG.debug("could not check a request, so it is refused: %s", error)
            permitted = False
        if permitted:
            await _quietly(route.fallback(), "let a request through")
            return
        navigation = request.is_navigation_request()
        where = _where(request.url)
        if navigation:
            self.record("blocked_request", f"stopped the page going to {where}")
        else:
            self.record("blocked_request", f"blocked a request to {where}")
        # An aborted navigation leaves the tester's page where it was, with no error page.
        # Anywhere else, such as a new window, an error page is shown instead: Playwright
        # then reports the window, and the guard closes it.
        reason = "aborted" if navigation and self._in_tester_frame(request) else "blockedbyclient"
        await _quietly(route.abort(reason), "abort a request")

    def _in_tester_frame(self, request: Request) -> bool:
        try:
            frame = request.frame
        except PlaywrightError:  # a window Playwright has not reported yet
            return False
        return frame.parent_frame is None and frame.page is self._main

    async def _route_socket(self, socket: WebSocketRoute) -> None:
        if self.allowed(socket.url):
            try:
                socket.connect_to_server()
            except PlaywrightError as error:
                LOG.debug("could not connect a WebSocket: %s", error)
            return
        self.record("blocked_request", f"blocked a WebSocket to {_where(socket.url)}")
        await _quietly(socket.close(code=_CLOSE_POLICY), "close a WebSocket")

    def _watch_request(self, request: Request) -> None:
        """Report a redirect hop that breaks the rule: Playwright never routes those."""
        if request.redirected_from is None:
            return
        try:
            permitted = self._permitted(request)
        except PlaywrightError:
            permitted = False
        if not permitted:
            what = "the browser" if request.is_navigation_request() else "a request"
            self.record("blocked_request", f"the app redirected {what} to {_where(request.url)}")

    def _watch_socket(self, socket: WebSocket) -> None:
        """Report a WebSocket that escaped routing, such as one a worker opened."""
        if not self.allowed(socket.url):
            self.record("blocked_request", f"the page opened a WebSocket to {_where(socket.url)}")

    def _watch_page(self, page: Page) -> None:
        page.on("download", self._cancel)
        page.on("filechooser", self._leave)
        if self._main is None or self._main is page:
            self._main = page
            page.on("websocket", self._watch_socket)
            return
        if self.on_app(page.url):
            parts = urlsplit(page.url)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            self.record("popup", f"closed a new window for {self._quote(path)}")
        else:
            self.record("popup", "closed a new window")
        task = asyncio.get_running_loop().create_task(_quietly(page.close(), "close a window"))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dismiss(self, dialog: Dialog) -> None:
        """Dismiss a dialog, but let the page go when it asks before it is left.

        Dismissing "leave this page?" would keep the tester on a page it has typed into.
        Wherever it goes next is still checked by the rule.
        """
        if dialog.type == "beforeunload":
            self.record("dialog", "the page asked before leaving")
            await _quietly(dialog.accept(), "accept a leave-page dialog")
            return
        try:
            what = _DIALOGS.get(dialog.type, "a dialog")
            message = dialog.message
            if message.strip():
                self.record("dialog", f"dismissed {what}: {self._quote(message)}")
            else:
                self.record("dialog", f"dismissed {what}")
        finally:
            await _quietly(dialog.dismiss(), "dismiss a dialog")

    async def _cancel(self, download: Download) -> None:
        try:
            name = download.suggested_filename
            if name.strip():
                self.record("download", f"cancelled a download of {self._quote(name)}")
            else:
                self.record("download", "cancelled a download")
        finally:
            await _quietly(download.cancel(), "cancel a download")

    def _leave(self, chooser: FileChooser) -> None:
        self.record("file_chooser", "left a file picker unanswered")


def _in_main_frame(request: Request) -> bool:
    try:
        frame = request.frame
    except PlaywrightError:
        # A new window's first page has a frame Playwright has not reported yet. A service
        # worker's request has none, but is not a navigation.
        return request.is_navigation_request()
    return frame.parent_frame is None


def _loggable(kind: Kind, detail: str) -> str:
    # The host of a blocked request is worth a log line; page text from a dialog is not.
    return detail if kind == "blocked_request" else kind
