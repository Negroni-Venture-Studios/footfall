"""The documentation's diagrams: every built file matches its hand-drawn source.

docs/diagrams/build.py writes a light and a dark copy of each diagram into
docs/images/diagrams/. A source changed without a rebuild would leave the
documents showing an old drawing.
"""

import importlib.util
import pathlib
import re

import pytest

from tests.test_doc_links import DOCUMENTS

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_diagrams", ROOT / "docs/diagrams/build.py")
assert SPEC is not None and SPEC.loader is not None
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

BUILT = build.outputs()


def test_there_are_diagrams() -> None:
    assert BUILT


@pytest.mark.parametrize("path", sorted(BUILT), ids=lambda p: p.name)
def test_each_built_diagram_is_up_to_date(path: pathlib.Path) -> None:
    assert path.exists(), f"run python3 docs/diagrams/build.py to build {path.name}"
    assert path.read_text() == BUILT[path], f"run python3 docs/diagrams/build.py: {path.name}"


def test_no_built_diagram_is_left_without_a_source() -> None:
    assert set(build.OUT.glob("*.svg")) == set(BUILT)


@pytest.mark.parametrize("source", sorted(build.DIAGRAMS.glob("*.svg")), ids=lambda p: p.name)
def test_sources_use_the_palette_not_colours(source: pathlib.Path) -> None:
    """Colours come from the classes, so one drawing serves both themes."""
    text = build.without_preview(source.read_text())
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(|(fill|stroke)=\"(?!none)", text)


@pytest.mark.parametrize("source", sorted(build.DIAGRAMS.glob("*.svg")), ids=lambda p: p.name)
def test_each_source_carries_its_light_preview(source: pathlib.Path) -> None:
    """GitHub shows a source on its own, so it carries the light palette to look right there."""
    text = source.read_text()
    assert text == build.with_preview(text), f"run python3 docs/diagrams/build.py: {source.name}"
    assert text.count(build.PREVIEW_OPEN) == 1


def test_the_preview_never_reaches_a_built_copy() -> None:
    """The built copies are made from the drawing alone, whatever preview the source holds."""
    source = next(iter(sorted(build.DIAGRAMS.glob("*.svg")))).read_text()
    bare = build.without_preview(source)
    for theme in (0, 1):
        assert build.build(source, theme) == build.build(bare, theme)
        assert build.PREVIEW_OPEN not in build.build(source, theme)


def test_every_diagram_is_used_in_the_documents() -> None:
    """Only the published documents count, not the plans or a local workspace's notes."""
    documents = "".join(p.read_text() for p in DOCUMENTS)
    for path in BUILT:
        assert f"images/diagrams/{path.name}" in documents, path.name
