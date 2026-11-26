import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_set() -> None:
    import footfall

    assert footfall.__version__ == "0.1.0"


def test_network_is_blocked() -> None:
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)


def image_python() -> str:
    """The Python version Footfall's own image runs, as `X.Y`, from its Dockerfile."""
    dockerfile = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    found = re.findall(r"^FROM python:(\d+\.\d+)\.\d+-\S+ AS runtime$", dockerfile, re.MULTILINE)
    assert len(found) == 1, found
    return str(found[0])


def runtime_packages() -> set[str]:
    """Every package Footfall installs to run in its image, on Linux with the image's Python,
    as uv reads them from uv.lock, so that every test job checks the same set."""
    uv = shutil.which("uv")
    assert uv, "uv runs the tests, so it is on the path"
    done = subprocess.run(
        [
            uv,
            "tree",
            "--frozen",
            "--offline",
            "--no-dev",
            "--no-dedupe",
            "--python-platform",
            "x86_64-unknown-linux-gnu",
            "--python-version",
            image_python(),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    names = set(re.findall(r"([a-z0-9][a-z0-9._-]*) v\d", done.stdout))
    return names - {"footfall"}


def test_the_runtime_packages_are_read_for_the_image() -> None:
    """uv leaves out what Linux never installs, such as httpx2-jsfetch, for the browser."""
    packages = runtime_packages()
    assert {"anthropic", "playwright", "typing-extensions", "truststore"} <= packages
    assert "httpx2-jsfetch" not in packages
    assert not packages & {"pytest", "mypy", "ruff"}


def test_third_party_licences_lists_every_runtime_package_on_its_own_row() -> None:
    text = (ROOT / "THIRD_PARTY_LICENCES.md").read_text()
    table = text.split("## Python packages Footfall runs on", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| ([^|]+?) \|", table, re.MULTILINE)[1:]  # after the heading row
    assert all(re.fullmatch(r"[a-z0-9-]+", row) for row in rows), rows
    assert sorted(rows) == sorted(runtime_packages())
