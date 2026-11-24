"""The container images: what the Dockerfiles say, since CI builds them and tests cannot."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = sorted((ROOT / "images").glob("*/Dockerfile"))
IMAGES = {"footfall", "python", "node", "browsers"}
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def test_all_four_dockerfiles_exist() -> None:
    assert {path.parent.name for path in DOCKERFILES} == IMAGES


def test_every_from_is_pinned_by_digest() -> None:
    froms = []
    for path in DOCKERFILES:
        for line in path.read_text().splitlines():
            parts = line.split()
            if parts and parts[0].upper() == "FROM":
                image = next(p for p in parts[1:] if not p.startswith("--"))
                froms.append((path.parent.name, image))
    assert froms
    unpinned = [(name, image) for name, image in froms if not DIGEST.search(image)]
    assert unpinned == []
    untagged = [(name, image) for name, image in froms if ":" not in image.split("@")[0]]
    assert untagged == [], "write the tag before the digest so Dependabot can update it"


@pytest.mark.parametrize("name", ["python", "node"])
def test_runtime_images_run_as_10010(name: str) -> None:
    text = (ROOT / "images" / name / "Dockerfile").read_text()
    assert re.search(r"--gid 10010\b", text)
    assert re.search(r"--uid 10010\b", text)
    assert "/usr/sbin/nologin" in text
    assert re.search(r"^USER 10010:10010$", text, re.MULTILINE)
    assert re.search(r"^WORKDIR /workspace$", text, re.MULTILINE)


def test_footfall_image_runs_as_10001() -> None:
    text = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    assert re.search(r"--uid 10001\b", text)
    assert re.search(r"^USER footfall$", text, re.MULTILINE)


def test_python_image_keeps_installs_in_the_workspace() -> None:
    text = (ROOT / "images" / "python" / "Dockerfile").read_text()
    for setting in ("PIP_USER=1", "PYTHONUSERBASE=/workspace/.local", "PIP_NO_CACHE_DIR=1"):
        assert setting in text
    assert "/workspace/.local/bin" in text


def test_node_image_caches_in_tmp() -> None:
    assert "npm_config_cache=/tmp/npm" in (ROOT / "images" / "node" / "Dockerfile").read_text()


def locked_playwright_version() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return str(next(p["version"] for p in lock["package"] if p["name"] == "playwright"))


def test_browsers_playwright_version_matches_lock() -> None:
    version = locked_playwright_version()
    text = (ROOT / "images" / "browsers" / "Dockerfile").read_text()
    assert f"mcr.microsoft.com/playwright:v{version}-noble@sha256:" in text
    assert f"npm install --global playwright@{version}" in text
    assert re.search(r"^USER pwuser$", text, re.MULTILINE)
    assert '"playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"' in text
