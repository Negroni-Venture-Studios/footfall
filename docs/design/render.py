"""Draw each mockup in docs/design/mockups/ as a light and a dark PNG.

The images in docs/images/mockups/ come from this script. Run it after changing a
mockup:

    uv run python docs/design/render.py

Headless Chromium draws each page 1044 CSS pixels wide at twice that in device pixels,
once for each colour scheme. A mockup may load nothing from the network.
"""

import asyncio
import pathlib
from collections.abc import Iterable

from playwright.async_api import Route, async_playwright

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES = ROOT / "docs" / "design" / "mockups"
OUT = ROOT / "docs" / "images" / "mockups"
WIDTH = 1044
SCALE = 2


async def _local_only(route: Route) -> None:
    if route.request.url.startswith("file:"):
        await route.continue_()
    else:
        await route.abort()


async def render(sources: Iterable[pathlib.Path], out: pathlib.Path) -> list[pathlib.Path]:
    """Write NAME-light.png and NAME-dark.png into `out` for each source and return them."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for source in sources:
                for scheme in ("light", "dark"):
                    page = await browser.new_page(
                        viewport={"width": WIDTH, "height": 400},
                        device_scale_factor=SCALE,
                        color_scheme=scheme,
                    )
                    await page.route("**/*", _local_only)
                    await page.goto(source.resolve().as_uri())
                    path = out / f"{source.stem}-{scheme}.png"
                    await page.screenshot(path=path, full_page=True)
                    await page.close()
                    written.append(path)
        finally:
            await browser.close()
    return written


def main() -> None:
    for path in asyncio.run(render(sorted(SOURCES.glob("*.html")), OUT)):
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
