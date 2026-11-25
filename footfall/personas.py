"""The personas and the devices they use.

Both come from TOML files that ship with Footfall as package data. Deployers can edit or
replace them, so reading is strict: a missing or unknown field, a duplicate, or a value out
of range raises a `PersonaError` that names the file, the entry and the problem.
"""

import re
import tomllib
import zoneinfo
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal, get_args

__all__ = [
    "DEVICES",
    "PERSONAS",
    "Device",
    "Persona",
    "PersonaError",
    "load_devices",
    "load_personas",
    "select",
]

DATA = Path(__file__).parent / "data"
DEVICES = DATA / "devices.toml"
PERSONAS = DATA / "personas.toml"

Engine = Literal["chromium", "webkit"]
Kind = Literal["phone", "tablet", "laptop"]
ColorScheme = Literal["light", "dark"]

MIN_TEXT_SCALE, MAX_TEXT_SCALE = 0.5, 3.0
_PERSONA_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")  # becomes a copy name later
# A BCP 47 language tag of the kind browsers take: a language, then perhaps a script and a
# region, such as en-GB, zh-Hant-TW or es-419.
_LOCALE = re.compile(r"[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?")


class PersonaError(ValueError):
    """A personas or devices file is not acceptable, or a persona was not found."""


@dataclass(frozen=True)
class Device:
    """A named device: a Playwright descriptor and the engine that runs it."""

    name: str
    playwright: str
    engine: Engine
    kind: Kind


@dataclass(frozen=True)
class Persona:
    """A person with a device and a way of browsing."""

    id: str
    profile: str
    goal: str
    device: str
    locale: str
    timezone: str
    color_scheme: ColorScheme
    reduced_motion: bool
    text_scale: float
    geolocation: tuple[float, float] | None
    allow_location: bool


def _read(path: Path, table: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PersonaError(f"{path}: cannot read the file: {error}") from error
    unknown = set(data) - {table}
    if unknown:
        raise PersonaError(f"{path}: unknown top-level key {sorted(unknown)[0]!r}")
    entries = data.get(table, [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise PersonaError(f"{path}: {table!r} must be an array of tables")
    return entries


def _check_fields(
    path: Path,
    label: str,
    entry: dict[str, Any],
    fields: Sequence[str],
    optional: Sequence[str] = (),
) -> None:
    for field in fields:
        if field not in entry:
            raise PersonaError(f"{path}: {label}: missing field {field!r}")
    for field in entry:
        if field not in fields and field not in optional:
            raise PersonaError(f"{path}: {label}: unknown field {field!r}")


def _choice(path: Path, label: str, entry: dict[str, Any], field: str, kind: Any) -> Any:
    value = entry[field]
    allowed = get_args(kind)
    if value not in allowed:
        raise PersonaError(f"{path}: {label}: {field} must be one of {', '.join(allowed)}")
    return value


def _text(path: Path, label: str, entry: dict[str, Any], field: str) -> str:
    value = entry[field]
    if not isinstance(value, str) or not value.strip():
        raise PersonaError(f"{path}: {label}: {field} must be a non-empty string")
    return value


def _locale(path: Path, label: str, entry: dict[str, Any]) -> str:
    value = _text(path, label, entry, "locale")
    if not _LOCALE.fullmatch(value):
        raise PersonaError(
            f"{path}: {label}: locale must be a language tag such as en-GB, not {value!r}"
        )
    return value


@cache
def _time_zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def _timezone(path: Path, label: str, entry: dict[str, Any]) -> str:
    value = _text(path, label, entry, "timezone")
    if value not in _time_zones():
        raise PersonaError(
            f"{path}: {label}: timezone must be a time zone such as Europe/London, not {value!r}"
        )
    return value


def load_devices(path: Path = DEVICES) -> dict[str, Device]:
    """Read the device list, keyed by device name, in file order."""
    devices: dict[str, Device] = {}
    for index, entry in enumerate(_read(path, "device"), start=1):
        label = f"device {entry.get('name', f'#{index}')!r}"
        _check_fields(path, label, entry, ["name", "playwright", "engine", "kind"])
        name = _text(path, label, entry, "name")
        if name in devices:
            raise PersonaError(f"{path}: {label}: duplicate device name")
        devices[name] = Device(
            name=name,
            playwright=_text(path, label, entry, "playwright"),
            engine=_choice(path, label, entry, "engine", Engine),
            kind=_choice(path, label, entry, "kind", Kind),
        )
    return devices


def _geolocation(path: Path, label: str, value: Any) -> tuple[float, float]:
    ok = (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(n, int | float) and not isinstance(n, bool) for n in value)
        and -90 <= value[0] <= 90
        and -180 <= value[1] <= 180
    )
    if not ok:
        raise PersonaError(f"{path}: {label}: geolocation must be [latitude, longitude] in degrees")
    return (float(value[0]), float(value[1]))


def _flag(path: Path, label: str, entry: dict[str, Any], field: str) -> bool:
    value = entry[field]
    if not isinstance(value, bool):
        raise PersonaError(f"{path}: {label}: {field} must be true or false")
    return value


def load_personas(devices: Mapping[str, Device], path: Path = PERSONAS) -> dict[str, Persona]:
    """Read the personas, keyed by id, in file order. Each device must be in `devices`."""
    fields = [f for f in Persona.__dataclass_fields__ if f != "geolocation"]
    personas: dict[str, Persona] = {}
    for index, entry in enumerate(_read(path, "persona"), start=1):
        label = f"persona {entry.get('id', f'#{index}')!r}"
        _check_fields(path, label, entry, fields, optional=["geolocation"])
        persona_id = _text(path, label, entry, "id")
        if not _PERSONA_ID.fullmatch(persona_id):
            raise PersonaError(
                f"{path}: {label}: id must be lower-case letters, digits and hyphens, "
                "starting with a letter, at most 32 characters"
            )
        if persona_id in personas:
            raise PersonaError(f"{path}: {label}: duplicate persona id")
        device = _text(path, label, entry, "device")
        if device not in devices:
            raise PersonaError(f"{path}: {label}: device {device!r} is not in the device list")
        scale = entry["text_scale"]
        if (
            isinstance(scale, bool)
            or not isinstance(scale, int | float)
            or not MIN_TEXT_SCALE <= scale <= MAX_TEXT_SCALE
        ):
            raise PersonaError(
                f"{path}: {label}: text_scale must be a number from "
                f"{MIN_TEXT_SCALE} to {MAX_TEXT_SCALE}"
            )
        personas[persona_id] = Persona(
            id=persona_id,
            profile=_text(path, label, entry, "profile"),
            goal=_text(path, label, entry, "goal"),
            device=device,
            locale=_locale(path, label, entry),
            timezone=_timezone(path, label, entry),
            color_scheme=_choice(path, label, entry, "color_scheme", ColorScheme),
            reduced_motion=_flag(path, label, entry, "reduced_motion"),
            text_scale=float(scale),
            geolocation=(
                _geolocation(path, label, entry["geolocation"]) if "geolocation" in entry else None
            ),
            allow_location=_flag(path, label, entry, "allow_location"),
        )
    return personas


def select(personas: Mapping[str, Persona], ids: Sequence[str]) -> list[Persona]:
    """Return the personas named in `ids`, in that order; no ids selects them all."""
    if not ids:
        return list(personas.values())
    unknown = [persona_id for persona_id in ids if persona_id not in personas]
    if unknown:
        raise PersonaError(
            f"unknown persona id {', '.join(repr(u) for u in unknown)}; "
            f"available: {', '.join(personas)}"
        )
    return [personas[persona_id] for persona_id in ids]
