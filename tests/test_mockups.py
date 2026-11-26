"""The mockups' images and the script that renders them.

docs/design/render.py draws each mockup in docs/design/mockups/ as a light and a dark
PNG in docs/images/mockups/. Run it after changing a mockup.
"""

import importlib.util
import pathlib
import struct

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_mockups", ROOT / "docs/design/render.py")
assert SPEC is not None and SPEC.loader is not None
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)

MOCKUPS = sorted((ROOT / "docs/design/mockups").glob("*.html"))


def png_size(path: pathlib.Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def test_there_are_mockups() -> None:
    assert MOCKUPS


@pytest.mark.parametrize("source", MOCKUPS, ids=lambda p: p.stem)
def test_each_mockup_has_a_light_and_a_dark_image(source: pathlib.Path) -> None:
    for scheme in ("light", "dark"):
        image = ROOT / "docs/images/mockups" / f"{source.stem}-{scheme}.png"
        assert png_size(image)[0] == render.WIDTH * render.SCALE


async def test_render_writes_both_schemes_at_double_width(tmp_path: pathlib.Path) -> None:
    page = tmp_path / "plain.html"
    page.write_text(
        "<!doctype html><style>body{background:#fff}"
        "@media (prefers-color-scheme:dark){body{background:#000}}</style><p>Plain</p>"
    )
    written = await render.render([page], tmp_path / "out")
    assert sorted(p.name for p in written) == ["plain-dark.png", "plain-light.png"]
    assert all(png_size(p)[0] == 2088 for p in written)
    assert written[0].read_bytes() != written[1].read_bytes()
