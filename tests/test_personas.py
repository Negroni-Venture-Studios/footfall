import re
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright

from footfall.personas import (
    DEVICES,
    PERSONAS,
    Device,
    PersonaError,
    load_devices,
    load_personas,
    select,
)

COPY_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

DEVICE_FILE = """
[[device]]
name = "Phone"
playwright = "Pixel 7"
engine = "chromium"
kind = "phone"
"""

PERSONA_FIELDS: dict[str, Any] = {
    "id": "ada",
    "profile": "Engineer, 40, patient.",
    "goal": "Finish the main task.",
    "device": "Phone",
    "locale": "en-GB",
    "timezone": "Europe/London",
    "color_scheme": "light",
    "reduced_motion": False,
    "text_scale": 1.0,
    "geolocation": [51.5, -0.12],
    "allow_location": True,
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _persona_block(**changes: Any) -> str:
    fields = {**PERSONA_FIELDS, **changes}
    lines = [f"{key} = {_toml_value(value)}" for key, value in fields.items() if value is not None]
    return "[[persona]]\n" + "\n".join(lines) + "\n"


def _load(tmp_path: Path, *blocks: str) -> Any:
    devices_path = tmp_path / "devices.toml"
    devices_path.write_text(DEVICE_FILE)
    personas_path = tmp_path / "personas.toml"
    personas_path.write_text("\n".join(blocks))
    return load_personas(load_devices(devices_path), personas_path)


async def _playwright_devices() -> dict[str, Any]:
    async with async_playwright() as playwright:
        return dict(playwright.devices)


async def test_every_device_exists_in_playwright() -> None:
    descriptors = await _playwright_devices()
    for device in load_devices().values():
        assert device.playwright in descriptors, device.name
        assert descriptors[device.playwright]["default_browser_type"] == device.engine, device.name


def test_twelve_personas() -> None:
    personas = load_personas(load_devices())
    assert len(personas) == 12
    assert list(personas) == [p.id for p in personas.values()]
    assert all(p.profile and p.goal for p in personas.values())
    assert all(len(p.profile) < 140 and len(p.goal) < 140 for p in personas.values())


async def test_coverage() -> None:
    descriptors = await _playwright_devices()
    devices = load_devices()
    personas = list(load_personas(devices).values())
    used = [(p, devices[p.device], descriptors[devices[p.device].playwright]) for p in personas]
    assert any(d.kind == "phone" and desc["viewport"]["width"] <= 375 for _, d, desc in used)
    assert any(d.kind == "tablet" for _, d, _ in used)
    assert any(d.kind == "laptop" and not desc["is_mobile"] for _, d, desc in used)
    assert {p.color_scheme for p in personas} == {"light", "dark"}
    assert any(p.text_scale >= 2.0 for p in personas)
    languages = {p.locale.split("-")[0] for p in personas}
    assert {"ar", "ja", "de", "sv"} <= languages
    assert any(p.reduced_motion for p in personas)


def test_shipped_data_details() -> None:
    personas = load_personas(load_devices())
    assert personas["clara"].allow_location is False
    assert all(p.allow_location for p in personas.values() if p.id != "clara")
    assert all(p.geolocation is not None for p in personas.values())
    assert personas["layla"].text_scale == 1.15
    assert personas["sven"].device == "iPhone SE"
    assert [p.id for p in personas.values() if p.reduced_motion] == ["clara"]


def test_select_unknown_id() -> None:
    personas = load_personas(load_devices())
    with pytest.raises(PersonaError, match="nobody"):
        select(personas, ["sven", "nobody"])


def test_select_orders_by_ids_and_empty_selects_all() -> None:
    personas = load_personas(load_devices())
    assert [p.id for p in select(personas, ["olga", "anna"])] == ["olga", "anna"]
    assert [p.id for p in select(personas, [])] == list(personas)


def test_ids_are_copy_names() -> None:
    assert all(COPY_NAME.fullmatch(pid) for pid in load_personas(load_devices()))


def test_default_paths_exist() -> None:
    assert DEVICES.is_file()
    assert PERSONAS.is_file()


def test_good_custom_file_loads(tmp_path: Path) -> None:
    personas = _load(tmp_path, _persona_block(geolocation=None, locale="zh-Hant-TW"))
    ada = personas["ada"]
    assert ada.geolocation is None
    assert (ada.locale, ada.timezone, ada.device) == ("zh-Hant-TW", "Europe/London", "Phone")


def test_shipped_devices_are_devices() -> None:
    assert isinstance(load_devices()["iPhone SE"], Device)


def test_a_profile_that_promises_large_text_has_it() -> None:
    """The profile is what the model reads, so it must not claim a text size the browser lacks."""
    personas = load_personas(load_devices()).values()
    large = [p for p in personas if re.search(r"\blarge(st)? text|text at its largest", p.profile)]
    assert {p.id for p in large} == {"sven", "grace", "anna"}
    assert all(p.text_scale > 1.0 for p in large)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"device": "Toaster"}, "Toaster"),
        ({"text_scale": 3.5}, "text_scale"),
        ({"text_scale": 0.4}, "text_scale"),
        ({"color_scheme": "sepia"}, "color_scheme"),
        ({"id": "Bad_Id"}, "Bad_Id"),
        ({"profile": None}, "profile"),
        ({"profile": ""}, "profile"),
        ({"profile": "   "}, "profile"),
        ({"locale": "english"}, "locale"),
        ({"locale": "en_GB"}, "locale"),
        ({"locale": "en-gb"}, "locale"),
        ({"timezone": "Europe/Londn"}, "timezone"),
        ({"timezone": "europe/london"}, "timezone"),
        ({"timezone": "../../etc/passwd"}, "timezone"),
        ({"timezone": "/usr/share/zoneinfo/UTC"}, "timezone"),
        ({"timezone": "posixrules"}, "timezone"),
        ({"surprise": 1}, "surprise"),
        ({"geolocation": [1]}, "geolocation"),
    ],
)
def test_invalid_persona_is_named(tmp_path: Path, changes: dict[str, Any], problem: str) -> None:
    with pytest.raises(PersonaError) as error:
        _load(tmp_path, _persona_block(**changes))
    message = str(error.value)
    assert "personas.toml" in message
    assert problem in message
    if "id" not in changes:
        assert "ada" in message


def test_duplicate_id(tmp_path: Path) -> None:
    with pytest.raises(PersonaError, match=r"personas\.toml.*ada.*duplicate"):
        _load(tmp_path, _persona_block(), _persona_block())


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ('engine = "chromium"', 'engine = "gecko"', "engine"),
        ('kind = "phone"', 'kind = "watch"', "kind"),
        ('kind = "phone"\n', "", "kind"),
        ('kind = "phone"', 'kind = "phone"\nextra = 1', "extra"),
    ],
)
def test_invalid_device_is_named(tmp_path: Path, old: str, new: str, problem: str) -> None:
    path = tmp_path / "devices.toml"
    path.write_text(DEVICE_FILE.replace(old, new))
    with pytest.raises(PersonaError) as error:
        load_devices(path)
    assert "devices.toml" in str(error.value)
    assert "Phone" in str(error.value)
    assert problem in str(error.value)


def test_duplicate_device(tmp_path: Path) -> None:
    path = tmp_path / "devices.toml"
    path.write_text(DEVICE_FILE * 2)
    with pytest.raises(PersonaError, match=r"devices\.toml.*Phone.*duplicate"):
        load_devices(path)


def test_unreadable_toml(tmp_path: Path) -> None:
    path = tmp_path / "devices.toml"
    path.write_text("[[device")
    with pytest.raises(PersonaError, match=r"devices\.toml"):
        load_devices(path)
    with pytest.raises(PersonaError, match=r"missing\.toml"):
        load_devices(tmp_path / "missing.toml")
