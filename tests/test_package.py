import re
import socket
import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker

ROOT = Path(__file__).resolve().parent.parent
LINUX = {"sys_platform": "linux", "platform_system": "Linux"}


def test_version_is_set() -> None:
    import footfall

    assert footfall.__version__ == "0.1.0"


def test_network_is_blocked() -> None:
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)


def runtime_packages() -> set[str]:
    """Every package Footfall installs to run on Linux, from uv.lock, transitively."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {package["name"]: package for package in lock["package"]}

    def applies(dependency: dict[str, str]) -> bool:
        marker = dependency.get("marker")
        return marker is None or Marker(marker).evaluate(LINUX)

    def needed(dependencies: list[dict[str, str]]) -> list[str]:
        return [d["name"] for d in dependencies if applies(d)]

    found: set[str] = set()
    waiting = needed(packages["footfall"]["dependencies"])
    while waiting:
        name = waiting.pop()
        if name not in found:
            found.add(name)
            waiting += needed(packages[name].get("dependencies", []))
    return found


def test_third_party_licences_lists_every_runtime_package_on_its_own_row() -> None:
    text = (ROOT / "THIRD_PARTY_LICENCES.md").read_text()
    table = text.split("## Python packages Footfall runs on", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| ([^|]+?) \|", table, re.MULTILINE)[1:]  # after the heading row
    assert all(re.fullmatch(r"[a-z0-9-]+", row) for row in rows), rows
    assert sorted(rows) == sorted(runtime_packages())
