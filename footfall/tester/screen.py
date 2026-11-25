"""What the tester sees after each action: a screenshot and a numbered list of the screen.

`snapshot.js` runs in the page and lists what is visible. The page belongs to the app under
test, so everything it returns is untrusted data: this module checks its shape, cleans and
caps every string, clamps every coordinate, and quotes names so that no page can forge a line
of the list (AGENTS.md rule 6). The screen format and the accessibility check are adapted from
mobile-agent-testers' `a11y.py` (MIT).
"""

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, get_args
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from footfall.tester.report import ELLIPSIS, PAGE_CAP, clean_text

__all__ = [
    "MAX_ELEMENTS",
    "MAX_SCREENSHOT",
    "CaptureError",
    "Element",
    "Kind",
    "Screen",
    "capture",
    "describe",
    "quote",
]

Kind = Literal["button", "link", "field", "text"]

MAX_ELEMENTS: Final = 150
TEXT_CAP: Final = 120
TITLE_CAP: Final = 120
SNAPSHOT_TIMEOUT: Final = 10.0
JPEG_QUALITY: Final = 70
# The most bytes a screenshot may have. The largest viewport of any device is the laptops',
# 1280 by 720 CSS pixels, and random noise, the worst case for JPEG, filling it came to
# 485,567 bytes at this quality in Chromium. 2 MiB is more than four times that, over two
# bytes a pixel, so no honest screenshot comes near it, whatever the browser's encoder.
MAX_SCREENSHOT: Final = 2 * 1024 * 1024
_JPEG_START: Final = b"\xff\xd8\xff"  # the start of image marker, and the next one's first byte
MIN_TARGET: Final = 44
BIG_SHARE: Final = 0.4  # a control this much of the screen needs no name to be found
LISTED_IN_WARNING: Final = 10
MAX_SIZE: Final = 1_000_000
MAX_VIEWPORT: Final = 100_000
MAX_ENTRIES: Final = 1000  # the most snapshot.js ever sends; anything beyond is a lie
MAX_STRING: Final = 4096  # snapshot.js cuts every string at 400, so a longer one is tampered with
LOG = logging.getLogger(__name__)
SNAPSHOT_JS: Final = (Path(__file__).parent / "snapshot.js").read_text(encoding="utf-8")

_KINDS: Final = frozenset(get_args(Kind))
_ROLE: Final = re.compile(r"[a-z]{1,20}")
_EDGE_SLACK: Final = 0.5

Evaluate = Callable[[Page], Awaitable[object]]


class CaptureError(RuntimeError):
    """The page could not be captured: it hung, or it answered with nonsense."""


@dataclass(frozen=True)
class Element:
    """One visible control or block of text. `centre` is where to tap, in CSS pixels."""

    number: int
    kind: Kind
    role: str
    name: str
    value: str
    centre: tuple[int, int]
    size: tuple[int, int]
    covered: bool = False  # something else is on top of its centre
    disabled: bool = False
    readonly: bool = False


@dataclass(frozen=True)
class Screen:
    path: str
    title: str
    width: int
    height: int
    elements: list[Element]
    omitted: int
    warnings: list[str]
    jpeg: bytes
    # Things worth knowing that are not accessibility findings: skipped frames, dropped entries.
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Parsed:
    kind: Kind
    role: str
    name: str
    value: str
    centre: tuple[int, int]
    size: tuple[int, int]
    raw_size: tuple[float, float]
    visible_area: float
    cut: bool
    covered: bool
    disabled: bool
    readonly: bool


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too big for a float, such as a BigInt from the page
        return None
    return number if math.isfinite(number) else None


def _count(value: object, limit: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        return None
    return value


def _parse_entry(raw: object, width: int, height: int) -> _Parsed | None:
    """Check one entry from the page; `None` if it is malformed. It never raises."""
    try:
        return _parse_checked(raw, width, height)
    except (TypeError, ValueError, OverflowError, KeyError):
        return None


def _parse_checked(raw: object, width: int, height: int) -> _Parsed | None:
    if not isinstance(raw, dict):
        return None
    kind, role, name, value = (raw.get(k) for k in ("kind", "role", "name", "value"))
    if not (isinstance(kind, str) and kind in _KINDS):
        return None
    if not (isinstance(role, str) and isinstance(name, str) and isinstance(value, str)):
        return None
    if max(len(role), len(name), len(value)) > MAX_STRING or not _ROLE.fullmatch(role):
        return None
    flags = [raw.get(k, False) for k in ("covered", "disabled", "readonly")]
    if not all(isinstance(f, bool) for f in flags):
        return None
    box = [_number(raw.get(k)) for k in ("x", "y", "w", "h")]
    if any(n is None for n in box):
        return None
    x, y, w, h = (n for n in box if n is not None)
    left, right = max(x, 0.0), min(x + w, float(width))
    top, bottom = max(y, 0.0), min(y + h, float(height))
    if w <= 0 or h <= 0 or right <= left or bottom <= top:
        return None
    centre = (
        min(max(int((left + right) / 2), 0), width - 1),
        min(max(int((top + bottom) / 2), 0), height - 1),
    )
    cut = (
        x < -_EDGE_SLACK
        or y < -_EDGE_SLACK
        or x + w > width + _EDGE_SLACK
        or y + h > height + _EDGE_SLACK
    )
    return _Parsed(
        kind=kind,  # type: ignore[arg-type]  # checked against _KINDS above
        role=role,
        name=clean_text(name, TEXT_CAP, single_line=True),
        value=clean_text(value, TEXT_CAP, single_line=True),
        centre=centre,
        size=(min(math.floor(w), MAX_SIZE), min(math.floor(h), MAX_SIZE)),
        raw_size=(w, h),
        visible_area=(right - left) * (bottom - top),
        cut=cut,
        covered=flags[0],
        disabled=flags[1],
        readonly=flags[2],
    )


def _numbers(numbers: list[str]) -> str:
    shown = ", ".join(numbers[:LISTED_IN_WARNING])
    more = len(numbers) - LISTED_IN_WARNING
    return f"{shown} and {more} more" if more > 0 else shown


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _accessibility_warnings(items: list[tuple[int, _Parsed]], width: int, height: int) -> list[str]:
    area = width * height
    unnamed = [
        f"[{n}]"
        for n, p in items
        if p.kind != "text" and not p.name and p.visible_area <= BIG_SHARE * area
    ]
    small = [
        f"[{n}] {p.size[0]}x{p.size[1]}"
        for n, p in items
        if p.kind != "text"
        and not p.cut
        and (p.raw_size[0] < MIN_TARGET or p.raw_size[1] < MIN_TARGET)
    ]
    warnings = []
    if unnamed:
        has = "has" if len(unnamed) == 1 else "have"
        warnings.append(
            f"{_plural(len(unnamed), 'control', 'controls')} {has} no name: {_numbers(unnamed)}"
        )
    if small:
        warnings.append(
            f"{_plural(len(small), 'touch target is', 'touch targets are')} under "
            f"{MIN_TARGET}x{MIN_TARGET} CSS pixels: {_numbers(small)}"
        )
    return warnings


def _path(url: str) -> str:
    parts = urlsplit(url[:MAX_STRING])
    if parts.scheme not in {"http", "https"}:
        return "/"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return clean_text(path, PAGE_CAP, single_line=True)


async def _default_evaluate(page: Page) -> object:
    return await page.evaluate(SNAPSHOT_JS)


def _unexpected(what: str) -> CaptureError:
    return CaptureError(f"the page's snapshot had an unexpected shape: {what}")


async def capture(
    page: Page, *, evaluate: Evaluate | None = None, timeout: float = SNAPSHOT_TIMEOUT
) -> Screen:
    """Take the screenshot and the list of what is on screen.

    `evaluate` replaces the call that runs `snapshot.js`, so a test can stand in for a page
    that lies. Nothing the page returns is believed until it has been checked here, the
    screenshot included, which must be a JPEG of at most `MAX_SCREENSHOT` bytes.

    After a `CaptureError` for a timeout the caller must close the browser context: the
    page's renderer may be stuck in a loop and will not answer anything else.
    """
    source = evaluate or _default_evaluate
    try:
        async with asyncio.timeout(timeout):
            data = await source(page)
    except TimeoutError:
        raise CaptureError(
            f"the page did not answer within {timeout:g} s while its screen was being read"
        ) from None
    except PlaywrightError as error:
        # The class only, in the log and in the error: what the page threw may hold anything,
        # a password included, and no redaction can be sure of catching an escaped or cut one.
        # The cause stays chained for the caller to inspect, never to show or log.
        LOG.debug("the page could not be read: %s", type(error).__name__)
        raise CaptureError(f"the page could not be read ({type(error).__name__})") from error
    if not isinstance(data, dict):
        raise _unexpected(type(data).__name__)
    entries, title, total = data.get("entries"), data.get("title"), data.get("total")
    if not isinstance(entries, list) or not isinstance(title, str):
        raise _unexpected("`entries` or `title` is missing or of the wrong type")
    listed_total = _count(total, 10**9)
    if listed_total is None:
        raise _unexpected("`total` is not a count")

    size = page.viewport_size
    width = size["width"] if size else _count(data.get("width"), MAX_VIEWPORT)
    height = size["height"] if size else _count(data.get("height"), MAX_VIEWPORT)
    if not width or not height:
        raise _unexpected("the viewport has no size")

    ignored = max(0, len(entries) - MAX_ENTRIES)
    parsed = [_parse_entry(raw, width, height) for raw in entries[:MAX_ENTRIES]]
    good = [p for p in parsed if p is not None]
    malformed = len(parsed) - len(good)
    kept = list(enumerate(good[:MAX_ELEMENTS], start=1))
    elements = [
        Element(
            n, p.kind, p.role, p.name, p.value, p.centre, p.size, p.covered, p.disabled, p.readonly
        )
        for n, p in kept
    ]
    omitted = max(0, listed_total - len(elements) - malformed)

    warnings = _accessibility_warnings(kept, width, height)
    notes = []
    frames = _count(data.get("frames"), 10**9) or 0
    if frames:
        notes.append(
            f"{_plural(frames, 'embedded frame was', 'embedded frames were')} not described"
        )
    if data.get("scanned_all") is False:
        notes.append("the page is very large, so only part of the page was scanned")
    if ignored:
        notes.append(f"{ignored} entries beyond the first {MAX_ENTRIES} were ignored")
    if malformed:
        notes.append(
            f"{_plural(malformed, 'malformed entry', 'malformed entries')} from the page dropped"
        )

    try:
        # Playwright's client only passes `timeout` on to the browser, and keeps no timer of
        # its own, so a browser container that stops answering is given up on here.
        async with asyncio.timeout(timeout):
            jpeg = await page.screenshot(
                type="jpeg",
                quality=JPEG_QUALITY,
                scale="css",
                full_page=False,
                timeout=timeout * 1000,
            )
    except TimeoutError:
        raise CaptureError(
            f"the page did not answer within {timeout:g} s while its screenshot was taken"
        ) from None
    except PlaywrightError as error:
        LOG.debug("the screenshot could not be taken: %s", type(error).__name__)
        raise CaptureError(f"the screenshot could not be taken ({type(error).__name__})") from error
    _check_screenshot(jpeg)

    return Screen(
        path=_path(page.url),
        title=clean_text(title[:MAX_STRING], TITLE_CAP, single_line=True),
        width=width,
        height=height,
        elements=elements,
        omitted=omitted,
        warnings=warnings,
        jpeg=jpeg,
        notes=notes,
    )


def _check_screenshot(jpeg: object) -> None:
    """Refuse a screenshot that cannot be one the browser took of the screen: not bytes, not
    a JPEG, or larger than `MAX_SCREENSHOT`. The bytes come from the browser container, which
    content the app shows might take over, and they are saved and sent to the model."""
    if not isinstance(jpeg, bytes):
        raise CaptureError(f"the screenshot was not bytes ({type(jpeg).__name__})")
    if len(jpeg) > MAX_SCREENSHOT:
        raise CaptureError(
            f"the screenshot was {len(jpeg):,} bytes, more than the {MAX_SCREENSHOT:,} a "
            "screen can take"
        )
    if not jpeg.startswith(_JPEG_START):
        raise CaptureError("the screenshot was not a JPEG")


def quote(text: str, cap: int = TEXT_CAP) -> str:
    """Make page text one line and put it in quotes it cannot close early.

    The text between the quotes, escapes included, is at most `cap` characters; text cut to
    fit ends with an ellipsis.
    """
    flat = clean_text(text, cap, single_line=True)
    pieces = ["\\" + ch if ch in '\\"' else ch for ch in flat]
    if sum(len(piece) for piece in pieces) > cap:
        kept: list[str] = []
        size = 0
        for piece in pieces:
            if size + len(piece) > cap - len(ELLIPSIS):
                break
            kept.append(piece)
            size += len(piece)
        pieces = [*kept, ELLIPSIS]
    return '"' + "".join(pieces) + '"'


def describe(screen: Screen) -> str:
    """The screen as text for the model: one element to a line, whatever the page says."""
    path = clean_text(screen.path, PAGE_CAP, single_line=True)
    title = clean_text(screen.title, TITLE_CAP, single_line=True)
    lines = [f"Page: {path} | {title}"]
    for e in screen.elements:
        value = f" value {quote(e.value)}" if e.value else ""
        role = e.role if _ROLE.fullmatch(e.role) else "generic"
        states = "".join(
            f" {word}"
            for word, on in (
                ("disabled", e.disabled),
                ("readonly", e.readonly),
                ("covered", e.covered),
            )
            if on
        )
        lines.append(
            f"[{e.number}] {role} {quote(e.name)}{value} "
            f"centre=({e.centre[0]},{e.centre[1]}){states}"
        )
    if screen.omitted:
        lines.append(f"… and {screen.omitted} more not listed")
    if screen.notes:
        lines.append(
            "note: " + "; ".join(clean_text(n, 300, single_line=True) for n in screen.notes)
        )
    if screen.warnings:
        joined = "; ".join(clean_text(w, 300, single_line=True) for w in screen.warnings)
        lines.append(f"accessibility check (confirm on the screenshot): {joined}")
    return "\n".join(lines)
