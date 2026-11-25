"""The tester's six tools: their definitions for the model, and the executor that runs them.

Every tool input comes from the model and is checked here before the browser does
anything, even though strict tool use already holds the model to the schemas: strict mode
cannot express a number's range or a string's length. No input can name a URL, a file or
anything outside the page. `go` takes a path, and `validate_path` builds the address from
the app's origin and that path alone (AGENTS.md rule 4).

Each action returns the screen it led to: the guard's events since the last action, a
line saying what was done, any notes, the screenshot id and the described screen. Every
call is appended to the trace with the screenshots before and after it, and with no more of
its input than the tool takes: each string cleaned, redacted and cut as the report's text is,
each list cut, and any field the tool does not have named without its value.
"""

import asyncio
import contextlib
import json
import logging
import re
import sys
import unicodedata
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from footfall.tester.guard import Guard, GuardEvent
from footfall.tester.report import (
    CAPS,
    ELLIPSIS,
    MAX_SCREENSHOTS,
    PAGE_CAP,
    ToolError,
    clean_text,
    map_report_text,
    parse_finish,
    strip_invisible,
)
from footfall.tester.screen import (
    SNAPSHOT_TIMEOUT,
    CaptureError,
    Element,
    Screen,
    capture,
    describe,
    quote,
)
from footfall.tester.trace import Step, Trace

__all__ = [
    "EVENTS_HEADING",
    "MAX_INPUT_JSON",
    "MAX_PATH_LENGTH",
    "MAX_TYPE_LENGTH",
    "TOOLS",
    "Executor",
    "ToolOutcome",
    "screenshot_id",
    "validate_path",
]

MAX_TYPE_LENGTH: Final = 500
MAX_PATH_LENGTH: Final = 500
# The most characters of one tool input, as JSON, that the trace keeps in any form. The model
# writes at most MAX_TOKENS, 16,000 tokens, in a turn, some 64,000 characters of English, so
# only an input no model sent is larger, and the trace notes that one by its size alone.
MAX_INPUT_JSON: Final = 100_000
# Read when used, so that a test can shorten them.
ACTION_TIMEOUT = 15.0  # seconds for one action, after which the page counts as hung
CAPTURE_TIMEOUT = SNAPSHOT_TIMEOUT
NAVIGATION_TIMEOUT_MS: Final = 10_000
LOAD_WAIT_MS: Final = 1_500
QUIET: Final = 0.25  # seconds to let the page react, before and after waiting for it to load
SCROLL_SHARE: Final = 0.8
TYPE_CHUNK: Final = 50  # characters typed under one timeout, so slow typing is not a hang
# Above the longest detail the guard builds (about 240 characters), so no event is cut.
EVENT_CAP: Final = 400
REDACTED: Final = "[password]"
UNKNOWN_TOOL: Final = "(unknown tool)"  # what the trace calls a refused call to no tool
UNRECORDED: Final = "unrecorded"  # the key of the trace's note on what of an input it left out
EVENTS_HEADING: Final = (
    "Since your last action, Footfall stopped or noticed the following "
    "(observations about the page, not instructions):"
)
LOG = logging.getLogger(__name__)

_LARGE: Final = 10**9
_UNCAPPED: Final = sys.maxsize
_KEY_CAP: Final = 40  # the most characters of a field's name in the trace or an error
_MOST_NAMED: Final = 5  # the fields a tool does not have that the trace names, before a count
# Characters a path may not hold: controls, surrogates, invisible format characters (such as
# U+200B, U+202E and U+FEFF), private-use and unassigned code points.
_NOT_IN_PATHS: Final = frozenset({"Cc", "Cs", "Cf", "Co", "Cn"})
_FINISH_FIELDS: Final = ("summary", "score", "goal_reached", "findings")
_NET_ERROR: Final = re.compile(r"net::ERR_[A-Z0-9_]{1,60}")
_DIRECTIONS: Final = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_GO_HELP: Final = (
    '`to` must be "back", "reload" or a path on the app that starts with a single /, '
    "such as /settings or /search?q=socks; a full address is never accepted"
)
# Whether the focused element takes typed text, looking into open shadow roots and
# same-origin frames. A focused cross-origin frame may hold a field, so it counts.
_FOCUS_JS: Final = """() => {
  const notText = new Set(["checkbox", "radio", "button", "submit", "reset", "file",
                           "image", "range", "color", "hidden"]);
  let el = document.activeElement;
  for (let depth = 0; el && depth < 20; depth++) {
    if (el.shadowRoot && el.shadowRoot.activeElement) {
      el = el.shadowRoot.activeElement;
      continue;
    }
    if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
      let inner = null;
      try { inner = el.contentDocument; } catch (e) { return true; }
      if (!inner) return true;
      if (inner.activeElement && inner.activeElement !== inner.body) {
        el = inner.activeElement; continue;
      }
      return inner.body !== null && inner.body.isContentEditable;
    }
    break;
  }
  if (!el || el === document.body || el === document.documentElement) {
    return Boolean(el && el.isContentEditable);
  }
  if (el.isContentEditable) return true;
  if (el.disabled || el.readOnly) return false;
  if (el.tagName === "TEXTAREA") return true;
  if (el.tagName === "INPUT") return !notText.has(String(el.type).toLowerCase());
  return false;
}"""


# ------------------------------------------------------------------ the definitions


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": _object(properties),
        "strict": True,
    }


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _whole_or_null(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


_FINDING: Final = _object(
    {
        "title": _text(f"A short title, at most {CAPS['title']} characters."),
        "severity": {
            "type": "string",
            "enum": ["blocker", "major", "minor", "cosmetic"],
            "description": "How much the problem gets in the person's way.",
        },
        "page": _text("The path of the page where it happens, such as /checkout."),
        "steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"The steps to reproduce it, at most {CAPS['steps']}.",
        },
        "expected": _text("What you expected to happen."),
        "actual": _text("What happened instead."),
        "screenshots": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The ids of the screenshots that show it, such as 03.",
        },
    }
)

TOOLS: Final[list[dict[str, Any]]] = [
    _tool(
        "look",
        "Take a fresh look at the screen. Returns a screenshot id, the page's path and "
        "title, a numbered list of the visible controls and text with each one's centre in "
        "CSS pixels, and accessibility warnings. Every other action already returns the "
        "screen it led to, so use this only when you need a fresh view, for example after "
        "waiting for something to change.",
        {},
    ),
    _tool(
        "tap",
        "Tap an element from the latest screen by its number, or tap a point by its x and y "
        "in CSS pixels from the top left of the screen. Give either `element`, or both `x` "
        "and `y`, and set the rest to null. Element numbers belong to the latest screen "
        "only: after any action, use the numbers on the new screen.",
        {
            "element": _whole_or_null("The element's number on the latest screen, or null."),
            "x": _whole_or_null("The point's distance from the left edge, or null."),
            "y": _whole_or_null("The point's distance from the top edge, or null."),
        },
    ),
    _tool(
        "type",
        "Type text into the field that has focus, as a person would on this device. Tap "
        f"the field first. At most {MAX_TYPE_LENGTH} characters. A newline presses Enter "
        "and a tab moves to the next field.",
        {"text": _text(f"The text to type, at most {MAX_TYPE_LENGTH} characters.")},
    ),
    _tool(
        "scroll",
        "Scroll the page by most of a screen in one direction.",
        {
            "direction": {
                "type": "string",
                "enum": list(_DIRECTIONS),
                "description": "Which way the page should move into view.",
            }
        },
    ),
    _tool(
        "go",
        "Go to a page on the app by its path, go back, or reload the page. You can visit "
        "only the app, so a full address is refused.",
        {
            "to": _text(
                '"back", "reload", or a path on the app that starts with /, such as '
                f"/settings or /search?q=socks, at most {MAX_PATH_LENGTH} characters."
            )
        },
    ),
    _tool(
        "finish",
        "Write your report and end the test. Give a summary, a score, whether you reached "
        "your goal, and your findings. Each finding has a short title, a severity, the "
        "page's path, the steps to reproduce it, what you expected, what happened, and the "
        f"ids of the screenshots that show it. Give at most {CAPS['findings']} findings.",
        {
            "summary": _text(
                f"What you did and what you found, at most {CAPS['summary']} characters."
            ),
            "score": {
                "type": "integer",
                "description": "A whole number from 0 to 10 for how well the app served you.",
            },
            "goal_reached": {
                "type": "string",
                "enum": ["yes", "partly", "no"],
                "description": "Whether you reached your goal.",
            },
            "findings": {
                "type": "array",
                "items": _FINDING,
                "description": "The problems you found, most severe first.",
            },
        },
    ),
]


_TOOL_NAMES: Final = frozenset(tool["name"] for tool in TOOLS)


# ------------------------------------------------------------------ checking inputs


def validate_path(to: str, app_origin: str) -> str:
    """Return the address of the path `to` on the app, or raise `ToolError`.

    A path starts with a single "/" and holds no whitespace, backslash or control
    character, any of which a browser could read as the start of another host, and no
    invisible character that would make the path read differently from what it is.
    """
    if not isinstance(to, str):
        raise ToolError("`to` must be a string")
    if len(to) > MAX_PATH_LENGTH:
        raise ToolError(f"a path can be at most {MAX_PATH_LENGTH} characters")
    if not to.startswith("/") or to.startswith("//"):
        raise ToolError(_GO_HELP)
    for ch in to:
        if ch == "\\" or ch.isspace() or unicodedata.category(ch) in _NOT_IN_PATHS:
            raise ToolError(
                "a path cannot hold spaces, backslashes, or control or invisible characters"
            )
    return app_origin.rstrip("/") + to


def screenshot_id(number: int) -> str:
    """Screenshot ids are two digits, zero-padded, and widen past 99."""
    return f"{number:02d}"


def _show(number: int) -> str:
    return str(number) if -_LARGE < number < _LARGE else "a very large number"


def _check_fields(
    data: Mapping[str, Any], names: tuple[str, ...], *, nullable: tuple[str, ...] = ()
) -> None:
    for key in data:
        if key not in names:
            raise ToolError(
                f"unexpected field `{clean_text(str(key), _KEY_CAP, single_line=True)}`"
            )
    for key in names:
        if key not in data and key not in nullable:
            raise ToolError(f"missing field `{key}`")


def _whole(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"`{name}` must be a whole number or null")
    return value


def _typeable(ch: str) -> bool:
    return ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cs")


@dataclass(frozen=True)
class _Text:
    """A string the trace keeps at most `cap` characters of."""

    cap: int
    single_line: bool = False


@dataclass(frozen=True)
class _List:
    """A list the trace keeps at most `most` items of, each of the shape `item`."""

    item: "_Shape"
    most: int


@dataclass(frozen=True)
class _Fields:
    """An object the trace keeps these fields of, and names the others of."""

    fields: Mapping[str, "_Shape"]


type _Shape = _Text | _List | _Fields

# A number or a choice, such as a severity: a string there is the model's mistake, and the
# trace keeps as much of it as of a field's name.
_VALUE: Final = _Text(_KEY_CAP, single_line=True)
_NO_FIELDS: Final = _Fields({})
# What the trace keeps of each tool's input: what the tool takes, and for `finish` no more
# than the report keeps, with each field on one line where the report has it so.
_SHAPES: Final[Mapping[str, _Fields]] = {
    "look": _NO_FIELDS,
    "tap": _Fields({"element": _VALUE, "x": _VALUE, "y": _VALUE}),
    "type": _Fields({"text": _Text(MAX_TYPE_LENGTH)}),
    "scroll": _Fields({"direction": _VALUE}),
    "go": _Fields({"to": _Text(MAX_PATH_LENGTH)}),
    "finish": _Fields(
        {
            "summary": _Text(CAPS["summary"]),
            "score": _VALUE,
            "goal_reached": _VALUE,
            "findings": _List(
                _Fields(
                    {
                        "title": _Text(CAPS["title"], single_line=True),
                        "severity": _VALUE,
                        "page": _Text(PAGE_CAP, single_line=True),
                        "steps": _List(_Text(CAPS["step"], single_line=True), CAPS["steps"]),
                        "expected": _Text(CAPS["expected"]),
                        "actual": _Text(CAPS["actual"]),
                        "screenshots": _List(_VALUE, MAX_SCREENSHOTS),
                    }
                ),
                CAPS["findings"],
            ),
        }
    ),
}


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def _event_text(event: GuardEvent) -> str:
    # The guard builds safe details, all shorter than EVENT_CAP; cleaning again keeps a
    # hand-built event on one line.
    text = clean_text(event.detail, EVENT_CAP, single_line=True)
    return f"{text} ({event.count} times)" if event.count > 1 else text


def _event_lines(events: list[GuardEvent]) -> list[str]:
    if not events:
        return []
    return [EVENTS_HEADING, *(f"- {_event_text(e)}" for e in events)]


def _check_finish(data: Mapping[str, Any]) -> None:
    _check_fields(data, _FINISH_FIELDS)
    findings = data["findings"]
    if not isinstance(findings, list):
        return  # parse_finish says what is wrong
    allowed = _FINDING["properties"]
    for index, item in enumerate(findings, start=1):
        if isinstance(item, Mapping):
            for key in item:
                if key not in allowed:
                    name = clean_text(str(key), _KEY_CAP, single_line=True)
                    raise ToolError(f"finding {index}: unexpected field `{name}`")


def _failure(error: BaseException) -> str:
    """What the trace says about an exception that ended a step."""
    if isinstance(error, CaptureError):
        return str(error)
    message = clean_text(str(error), 300, single_line=True)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _element_at(screen: Screen, x: int, y: int) -> Element | None:
    """The smallest listed element whose box holds the point, if any."""
    best: Element | None = None
    for e in screen.elements:
        (cx, cy), (w, h) = e.centre, e.size
        inside = abs(x - cx) <= w / 2 and abs(y - cy) <= h / 2
        if inside and (best is None or w * h < best.size[0] * best.size[1]):
            best = e
    return best


# ------------------------------------------------------------------ the executor


@dataclass(frozen=True)
class ToolOutcome:
    """What one tool call led to. `text` and the screen's JPEG go back to the model."""

    text: str
    screen: Screen | None
    finish: dict[str, Any] | None
    error: bool


@dataclass
class _Action:
    summary: str = ""
    failures: list[str] = field(default_factory=list)
    finish: dict[str, Any] | None = None
    role: str | None = None
    name: str | None = None
    x: int | None = None
    y: int | None = None


class Executor:
    """Runs the model's tool calls on one page and records each in the trace.

    `touch` says whether taps use the touchscreen, as the device does. Each string in
    `redact`, such as the throwaway password the tester types to sign in, is replaced by
    "[password]" in the trace, the free text of the report from `finish` and any
    `CaptureError`, wherever it appears whole once characters that show nothing are taken
    out. The match is exact, so the same text split across two calls, or in another case,
    or with other characters between its own, is not found. The log holds no error's
    message at all, only its class.
    """

    def __init__(
        self,
        page: Page,
        guard: Guard,
        trace: Trace,
        screenshots: Path,
        *,
        touch: bool,
        redact: frozenset[str] = frozenset(),
    ) -> None:
        self._page = page
        self._guard = guard
        self._trace = trace
        self._dir = screenshots
        self._touch = touch
        # Stripped as the text they are looked for in is, so that a secret holding a
        # character that shows nothing still matches; one made only of such characters
        # would match nothing and goes. Longest first, so that a secret holding another is
        # replaced whole.
        visible = {strip_invisible(secret) for secret in redact}
        self._secrets = tuple(sorted((x for x in visible if x), key=len, reverse=True))
        self._screen: Screen | None = None
        self._shot: str | None = None
        self._shots: list[str] = []
        self._steps = 0
        self._attempt = _Action()
        self._home = guard.origin + "/"
        self._home_path = "/"

    @property
    def secrets(self) -> tuple[str, ...]:
        """The strings this executor redacts, without characters that show nothing and
        longest first, so that others can redact the same.
        """
        return self._secrets

    @property
    def screenshot_ids(self) -> set[str]:
        """The ids of every screenshot taken so far, which findings may cite."""
        return set(self._shots)

    async def first_look(self) -> ToolOutcome:
        """The screen before the first action. It is screenshot 01 and is not traced.

        A `CaptureError` is raised as `run` raises it, redacted and with nothing chained.
        """
        try:
            notes = await self._settle_on_app()
            events = self._guard.drain()
            screen, shot, again = await self._look()
        except CaptureError as error:
            hidden = self._hide(error)
        else:
            lines = notes + again
            return ToolOutcome(self._compose(events, lines, shot, screen), screen, None, False)
        raise hidden from None

    async def run(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        """Run one tool call. A mistake in the input comes back as an error outcome.

        Anything else that goes wrong is raised after the step is traced. `CaptureError`,
        raised when the page hangs or cannot be read, means the caller must end the persona
        and close the browser context. Its message may quote the page, so it is raised again
        with every secret redacted and with no exception chained to it.
        """
        if self._screen is None or self._shot is None:
            raise RuntimeError("call first_look before run")
        self._steps += 1
        self._attempt = _Action()
        draft = Step(
            number=self._steps,
            tool=clean_text(str(name), _KEY_CAP, single_line=True),
            input=self._record(name, tool_input),
            target_role=None,
            target_name=None,
            x=None,
            y=None,
            path_before=self._screen.path,
            path_after=self._screen.path,
            screenshot_before=self._shot,
            screenshot_after=None,
            error=None,
        )
        events: list[GuardEvent] = []
        traced = False
        try:
            try:
                if not isinstance(tool_input, Mapping):
                    raise ToolError("the tool input must be an object")
                action = await self._perform(name, tool_input)
            except ToolError as error:
                events = self._guard.drain()
                message = str(error)
                traced = True
                self._append(draft, events, error=message)
                return ToolOutcome("\n".join([*_event_lines(events), message]), None, None, True)
            if action.finish is not None:
                traced = True
                self._append(draft, self._guard.drain())
                report = map_report_text(action.finish, self._redact)
                return ToolOutcome("Your report was received.", None, report, False)
            notes = await self._settle_on_app()
            events = self._guard.drain()
            screen, shot, again = await self._look()
            notes += again
            traced = True
            self._append(
                draft,
                events,
                error=" ".join(action.failures) or None,
                action=action,
                path_after=screen.path,
                screenshot_after=shot,
            )
            lines = [action.summary, *action.failures, *notes]
            return ToolOutcome(self._compose(events, lines, shot, screen), screen, None, False)
        except BaseException as error:
            # Whatever ended the step, the trace says so, unless writing it is what failed.
            if not traced:
                failure = _failure(error)
                self._append(
                    draft, events + self._guard.drain(), error=failure, action=self._attempt
                )
            if not isinstance(error, CaptureError):
                raise
            hidden = self._hide(error)
        # Raised here, outside the handler, so that neither __cause__ nor __context__ holds
        # the original error and whatever the page put in it.
        raise hidden from None

    def refuse(self, name: object, reason: str) -> None:
        """Trace a tool call the loop would not run, and the reason.

        The step names the tool only when it is one of the six. Any other name is the
        model's own text, and could hold the password whole or in part, so it is traced as
        `UNKNOWN_TOOL`. Nothing of the input is kept, and nothing happens in the browser.
        """
        if self._screen is None or self._shot is None:
            raise RuntimeError("call first_look before refuse")
        self._steps += 1
        path = self._scrub(self._screen.path)
        self._trace.append(
            Step(
                number=self._steps,
                tool=name if isinstance(name, str) and name in _TOOL_NAMES else UNKNOWN_TOOL,
                input={},
                target_role=None,
                target_name=None,
                x=None,
                y=None,
                path_before=path,
                path_after=path,
                screenshot_before=self._shot,
                screenshot_after=None,
                error=reason,
            )
        )

    # ------------------------------------------------------------------ the tools

    async def _perform(self, name: str, data: Mapping[str, Any]) -> _Action:
        if name == "look":
            _check_fields(data, ())
            return _Action()
        if name == "tap":
            return await self._tap(data)
        if name == "type":
            return await self._type(data)
        if name == "scroll":
            return await self._scroll(data)
        if name == "go":
            return await self._go(data)
        if name == "finish":
            _check_finish(data)
            # Checked as the loop will read it, so that free text made only of characters
            # that show nothing is refused here, where the model can try again.
            visible = map_report_text(data, strip_invisible) if self._secrets else data
            parse_finish(visible, source="", device="", screenshots=self.screenshot_ids)
            return _Action(finish=dict(data))
        # The name is the model's own text, so none of it is echoed into the trace.
        raise ToolError("there is no tool by that name")

    async def _tap(self, data: Mapping[str, Any]) -> _Action:
        names = ("element", "x", "y")
        _check_fields(data, names, nullable=names)
        element, x, y = (_whole(data.get(n), n) for n in names)
        if element is not None and x is None and y is None:
            target = self._element(element)
            x, y = target.centre
            action = _Action(f"Tapped element {element}.", role=target.role, name=target.name)
        elif element is None and x is not None and y is not None:
            width, height = self._viewport()
            if not (0 <= x < width and 0 <= y < height):
                raise ToolError(
                    f"the point ({_show(x)}, {_show(y)}) is outside the screen, which is "
                    f"{width} by {height} CSS pixels"
                )
            action = _Action(f"Tapped the point ({x}, {y}).")
            hit = _element_at(self._current(), x, y)
            if hit is not None:
                action.role, action.name = hit.role, hit.name
        else:
            raise ToolError(
                "give either an element number or both x and y, and set the rest to null"
            )
        action.x, action.y = x, y
        self._attempt = action
        if self._touch:
            action.failures = await self._act(self._page.touchscreen.tap(x, y))
        else:
            action.failures = await self._act(self._page.mouse.click(x, y))
        return action

    async def _type(self, data: Mapping[str, Any]) -> _Action:
        _check_fields(data, ("text",))
        text = data["text"]
        if not isinstance(text, str):
            raise ToolError("`text` must be a string")
        if not text:
            raise ToolError("`text` must not be empty")
        if len(text) > MAX_TYPE_LENGTH:
            raise ToolError(
                f"`text` can be at most {MAX_TYPE_LENGTH} characters, and this has {len(text)}"
            )
        if not all(_typeable(ch) for ch in text):
            raise ToolError("`text` holds control characters; only newlines and tabs can be typed")
        if not await self._focused_field():
            raise ToolError("nothing that takes text has focus; tap a field first")
        noun = "character" if len(text) == 1 else "characters"
        action = _Action(f"Typed {len(text)} {noun}.")
        self._attempt = action
        # Each chunk has its own timeout: a page that handles every keystroke slowly is not
        # hung, but one that stops answering is.
        for start in range(0, len(text), TYPE_CHUNK):
            chunk = text[start : start + TYPE_CHUNK]
            action.failures = await self._act(self._page.keyboard.type(chunk))
            if action.failures:
                break
        return action

    async def _scroll(self, data: Mapping[str, Any]) -> _Action:
        _check_fields(data, ("direction",))
        direction = data["direction"]
        if not isinstance(direction, str) or direction not in _DIRECTIONS:
            raise ToolError("`direction` must be one of up, down, left or right")
        width, height = self._viewport()
        sx, sy = _DIRECTIONS[direction]
        dx, dy = sx * width * SCROLL_SHARE, sy * height * SCROLL_SHARE
        action = self._attempt = _Action(f"Scrolled {direction}.")
        action.failures = await self._act(self._wheel(width / 2, height / 2, dx, dy))
        return action

    async def _wheel(self, x: float, y: float, dx: float, dy: float) -> None:
        await self._page.mouse.move(x, y)
        try:
            await self._page.mouse.wheel(dx, dy)
        except PlaywrightError as error:  # some mobile emulations have no wheel
            self._debug("the mouse wheel failed, so the page is scrolled directly", error)
            await self._page.evaluate("([x, y]) => window.scrollBy(x, y)", [dx, dy])

    async def _go(self, data: Mapping[str, Any]) -> _Action:
        _check_fields(data, ("to",))
        to = data["to"]
        page = self._page
        if to == "back":
            action = self._attempt = _Action("Went back.")
            go = page.go_back(wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
        elif to == "reload":
            action = self._attempt = _Action("Reloaded the page.")
            go = page.reload(wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
        else:
            url = validate_path(to, self._guard.origin)
            action = self._attempt = _Action(f"Went to {quote(to, PAGE_CAP)}.")
            go = page.goto(url, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
        action.failures = await self._act(go)
        return action

    # ------------------------------------------------------------------ the page

    def _current(self) -> Screen:
        if self._screen is None:
            raise RuntimeError("call first_look before run")
        return self._screen

    def _element(self, number: int) -> Element:
        for e in self._current().elements:
            if e.number == number:
                return e
        shown = f"element {number}" if -_LARGE < number < _LARGE else "that element"
        raise ToolError(f"{shown} is not on the current screen; look again")

    def _viewport(self) -> tuple[int, int]:
        size = self._page.viewport_size
        if size:
            return size["width"], size["height"]
        screen = self._current()
        return screen.width, screen.height

    async def _act(self, action: Awaitable[object]) -> list[str]:
        """Do one thing in the browser, within `ACTION_TIMEOUT`.

        A browser error becomes a line for the result; a page that does not answer at all
        is hung, which ends the persona.
        """
        try:
            async with asyncio.timeout(ACTION_TIMEOUT):
                await action
        except TimeoutError:
            raise CaptureError(
                f"the page did not answer within {ACTION_TIMEOUT:g} s during the action"
            ) from None
        except PlaywrightError as error:
            return [self._browser_error(error)]
        return []

    def _browser_error(self, error: PlaywrightError) -> str:
        """Say what went wrong in the browser without passing on any text from the page."""
        self._debug("the browser reported an error", error)
        if isinstance(error, PlaywrightTimeoutError):
            return "The page did not finish loading in time."
        code = _NET_ERROR.search(str(error))
        if code:
            return f"The page could not be loaded: {code.group(0)}."
        return "The browser could not complete the action."

    async def _focused_field(self) -> bool:
        try:
            async with asyncio.timeout(ACTION_TIMEOUT):
                answer = await self._page.evaluate(_FOCUS_JS)
        except TimeoutError:
            raise CaptureError(
                f"the page did not answer within {ACTION_TIMEOUT:g} s while its focus was read"
            ) from None
        except PlaywrightError as error:
            self._debug("could not read which element has focus", error)
            return False
        return answer is True

    async def _settle(self) -> None:
        """Let the page react, within about two seconds; never wait for the network to idle."""
        await asyncio.sleep(QUIET)
        with contextlib.suppress(PlaywrightError):
            await self._page.wait_for_load_state("domcontentloaded", timeout=LOAD_WAIT_MS)
        await asyncio.sleep(QUIET)

    async def _settle_on_app(self) -> list[str]:
        """Settle, and bring the browser back if it has left the app."""
        await self._settle()
        if self._guard.on_app(self._page.url):
            return []
        notes = [f"The browser left the app, so Footfall brought it back to {self._home_path}."]
        goto = self._page.goto(self._home, wait_until="commit", timeout=NAVIGATION_TIMEOUT_MS)
        notes += await self._act(goto)
        await self._settle()
        return notes

    async def _look(self) -> tuple[Screen, str, list[str]]:
        """Capture the screen, once more if the page moved on while it was being read, or
        left the app, and raise `CaptureError` if it left the app again.

        Checking that the browser is on the app and reading the screen are not one step: a
        page can be sent to another host between them, by a redirect hop the guard never
        routes, and what was read is then that host's page, which the model must never be
        shown. So the address is checked again after the read, and a screen read off the
        app is thrown away unsaved. Returns the screen, its screenshot id, and any notes
        from bringing the browser back to the app before a second read.
        """
        screen, notes = await self._read()
        if not self._guard.on_app(self._page.url):
            LOG.debug("the page left the app while it was read, so it is read again")
            notes += await self._settle_on_app()
            screen, again = await self._read()
            notes += again
            if not self._guard.on_app(self._page.url):
                raise CaptureError("the page left the app each time its screen was read")
        shot = self._save(screen.jpeg)
        self._screen, self._shot = screen, shot
        self._home, self._home_path = self._page.url, screen.path
        return screen, shot, notes

    async def _read(self) -> tuple[Screen, list[str]]:
        """Capture the screen, once more if the page moved on while it was being read.

        Returns the screen and any notes from bringing the browser back to the app before
        the second read.
        """
        before = self._page.url
        notes: list[str] = []
        try:
            screen = await capture(self._page, timeout=CAPTURE_TIMEOUT)
        except CaptureError as error:
            # A navigation destroys the page's context mid-read, or changes the address while
            # the page is read. Either is read once more, even after a timeout, and a second
            # failure ends the persona. A timeout or an answer of the wrong shape at the same
            # address is the page's fault, and ends it at once.
            cause = error.__cause__
            moved = isinstance(cause, PlaywrightError) and not isinstance(
                cause, PlaywrightTimeoutError
            )
            if not (moved or self._page.url != before):
                raise
            self._debug("the page moved while it was read, so it is read again", error)
            # Where the navigation ended may be off the app; the model only ever sees the app.
            notes = await self._settle_on_app()
            screen = await capture(self._page, timeout=CAPTURE_TIMEOUT)
        return screen, notes

    def _save(self, jpeg: bytes) -> str:
        shot = screenshot_id(len(self._shots) + 1)
        self._dir.mkdir(parents=True, exist_ok=True)
        # "x": never overwrite a screenshot, nor follow a link left where one should go.
        with (self._dir / f"{shot}.jpg").open("xb") as handle:
            handle.write(jpeg)
        self._shots.append(shot)
        return shot

    # ------------------------------------------------------------------ secrets

    def _redact(self, text: str) -> str:
        """`text` with every secret replaced, after the characters that show nothing are
        removed, so that a password split by them is found too.
        """
        if not self._secrets:
            return text
        text = strip_invisible(text)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def _fit(self, text: str, cap: int, *, single_line: bool = False) -> str:
        """`text` cleaned and made one line where the field is, then redacted, then cut to
        `cap`. Every change to the text comes before redaction, so that none can make a
        password whole after it, as joining a line it was split over would; and the cut comes
        after, so that none leaves part of one."""
        redacted = self._redact(clean_text(text, _UNCAPPED, single_line=single_line))
        return redacted if len(redacted) <= cap else redacted[: cap - 1] + ELLIPSIS

    def _record(self, name: str, data: object) -> dict[str, Any]:
        """A tool input as the trace keeps it: the fields the tool has, each string cleaned,
        redacted and cut as `_SHAPES` says, each list cut to its most items, and the other
        fields named without their values. An input that is not an object, cannot be written
        as JSON or is larger than `MAX_INPUT_JSON` is noted, and none of it kept.
        """
        if not isinstance(data, Mapping):
            return {UNRECORDED: "the input was not an object"}
        try:
            size = len(json.dumps(data, allow_nan=False, ensure_ascii=False))
        except (TypeError, ValueError, RecursionError):
            return {UNRECORDED: "the input could not be written as JSON"}
        if size > MAX_INPUT_JSON:
            return {
                UNRECORDED: f"the input was {size:,} characters as JSON, more than the "
                f"{MAX_INPUT_JSON:,} the trace keeps"
            }
        return self._kept_fields(data, _SHAPES.get(name, _NO_FIELDS))

    def _kept_fields(self, data: Mapping[Any, Any], shape: _Fields) -> dict[str, Any]:
        kept = {
            key: self._kept(data[key], part) for key, part in shape.fields.items() if key in data
        }
        others = [key for key in data if key not in shape.fields]
        if others:
            names = ", ".join(
                f"`{self._fit(str(key), _KEY_CAP, single_line=True)}`"
                for key in others[:_MOST_NAMED]
            )
            more = len(others) - _MOST_NAMED
            kept[UNRECORDED] = (
                "fields the tool does not have, named only: "
                + names
                + (f" and {more} more" if more > 0 else "")
            )
        return kept

    def _kept(self, value: object, shape: _Shape) -> Any:
        if isinstance(value, str):
            if isinstance(shape, _Text):
                return self._fit(value, shape.cap, single_line=shape.single_line)
            return self._fit(value, _KEY_CAP, single_line=True)
        if isinstance(value, list):
            if not isinstance(shape, _List):
                return {
                    UNRECORDED: f"a list of {_count(len(value), 'item', 'items')}, not taken here"
                }
            kept = [self._kept(item, shape.item) for item in value[: shape.most]]
            if len(value) > shape.most:
                more = _count(len(value) - shape.most, "more item", "more items")
                kept.append({UNRECORDED: f"{more}, not recorded"})
            return kept
        if isinstance(value, Mapping):
            if not isinstance(shape, _Fields):
                fields = _count(len(value), "field", "fields")
                return {UNRECORDED: f"an object of {fields}, not taken here"}
            return self._kept_fields(value, shape)
        return value  # a number, true, false or null, which JSON has already been asked to write

    def _scrub(self, value: Any) -> Any:
        """`value` with every secret replaced, through lists and objects, keys included."""
        if not self._secrets:
            return value
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, dict):
            return {self._scrub(k): self._scrub(v) for k, v in value.items()}
        return value

    def _hide(self, error: CaptureError) -> CaptureError:
        """A copy of `error` to raise instead: secrets redacted, the page's error left behind.

        Raise it outside the `except` block that caught `error`, and `from None`.
        """
        return CaptureError(self._redact(str(error)))

    @staticmethod
    def _debug(what: str, error: BaseException) -> None:
        """Log what went wrong and the error's class, never its message.

        A browser error's message can quote what the page threw, and a page can throw the
        password typed a moment before, escaped or cut so that no redaction would find it.
        """
        LOG.debug("%s (%s)", what, type(error).__name__)

    # ------------------------------------------------------------------ results

    @staticmethod
    def _compose(events: list[GuardEvent], lines: list[str], shot: str, screen: Screen) -> str:
        parts = _event_lines(events) + [line for line in lines if line]
        parts += [f"Screenshot id: {shot}", describe(screen)]
        return "\n".join(parts)

    def _append(
        self,
        draft: Step,
        events: list[GuardEvent],
        *,
        error: str | None = None,
        action: _Action | None = None,
        path_after: str | None = None,
        screenshot_after: str | None = None,
    ) -> None:
        target = action or _Action()
        scrub = self._scrub
        self._trace.append(
            Step(
                number=draft.number,
                tool=scrub(draft.tool),
                input=scrub(draft.input),  # redacted as it was recorded; this is a backstop
                target_role=scrub(target.role),
                target_name=scrub(target.name),
                x=target.x,
                y=target.y,
                path_before=scrub(draft.path_before),
                path_after=scrub(path_after or draft.path_after),
                screenshot_before=draft.screenshot_before,
                screenshot_after=screenshot_after,
                error=scrub(error),
                events=scrub([_event_text(e) for e in events]),
            )
        )
