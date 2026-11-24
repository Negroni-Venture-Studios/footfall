"""The container images: what the Dockerfiles say, since CI builds them and tests cannot."""

import json
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


def test_browsers_image_has_the_gate_and_the_ech_policy() -> None:
    text = (ROOT / "images" / "browsers" / "Dockerfile").read_text()
    assert "COPY images/browsers/gate.js /opt/footfall/gate.js" in text
    policy = "images/browsers/chromium-policy.json"
    for folder in (
        "/etc/chromium/policies/managed",
        "/etc/opt/chrome/policies/managed",
        "/etc/opt/chrome_for_testing/policies/managed",
    ):
        assert f"COPY {policy} {folder}/footfall.json" in text
    assert json.loads((ROOT / policy).read_text()) == {"EncryptedClientHelloEnabled": False}


def test_footfall_image_installs_git_from_its_own_release() -> None:
    # footfall test lists the files git would commit; the base image's Debian release
    # supplies git, with its security updates, and nothing it merely recommends.
    text = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    runtime = text.split(
        "# ---------------------------------------------------------------- runtime"
    )[1]
    install = re.search(r"apt-get install --yes --no-install-recommends git\b", runtime)
    assert install is not None
    assert "rm -rf /var/lib/apt/lists/*" in runtime
    assert re.search(r"^FROM python:[^ ]+-trixie@sha256:", runtime, re.MULTILINE)


# ---------------------------------------------------------------- the build context


def _pattern(text: str) -> re.Pattern[str]:
    """A .dockerignore pattern as a regular expression, as Docker's matcher reads it: `*`
    and `?` stay within one folder, and `**` crosses folders."""
    parts = []
    index = 0
    clean = text.strip().strip("/")
    while index < len(clean):
        if clean.startswith("**", index):
            parts.append(".*")
            index += 2
        elif clean[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif clean[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(clean[index]))
            index += 1
    return re.compile("".join(parts))


def admitted(path: str, ignore: str) -> bool:
    """Whether a build context path survives a .dockerignore: the last pattern that matches
    the path or any folder above it decides, and `!` lets it back in."""
    rules = []
    for line in ignore.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keep = line.startswith("!")
        rules.append((keep, _pattern(line.removeprefix("!"))))
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    verdict = True
    for keep, rule in rules:
        if any(rule.fullmatch(prefix) for prefix in prefixes):
            verdict = keep
    return verdict


def copy_sources() -> list[tuple[str, str]]:
    """Every COPY source in every Dockerfile that comes from the build context."""
    sources = []
    for path in DOCKERFILES:
        for line in path.read_text().splitlines():
            words = line.split()
            if not words or words[0].upper() != "COPY":
                continue
            arguments = [w for w in words[1:] if not w.startswith("--")]
            if any(w.startswith("--from=") for w in words[1:]):
                continue
            sources += [(path.parent.name, source) for source in arguments[:-1]]
    return sources


def context_files(source: str) -> list[str]:
    """The files a COPY source stands for: itself, or the tracked files below a folder."""
    path = ROOT / source
    if path.is_dir():
        return sorted(
            str(p.relative_to(ROOT))
            for p in path.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
    return [source]


def test_every_copy_source_is_in_the_build_context() -> None:
    ignore = (ROOT / ".dockerignore").read_text()
    sources = copy_sources()
    assert ("browsers", "images/browsers/gate.js") in sources
    assert ("footfall", "footfall") in sources
    refused = [
        (image, file)
        for image, source in sources
        for file in context_files(source)
        if not admitted(file, ignore)
    ]
    assert refused == []


def test_the_check_would_have_caught_the_missing_gate() -> None:
    old = """*
!pyproject.toml
!uv.lock
!README.md
!LICENSE
!footfall/
**/__pycache__
"""
    assert not admitted("images/browsers/gate.js", old)
    assert admitted("footfall/local.py", old)
    assert not admitted("footfall/__pycache__/local.cpython-312.pyc", old)
    assert not admitted("tests/test_local.py", old)
