"""The container images: what the Dockerfiles say, since CI builds them and tests cannot."""

import importlib.metadata
import importlib.resources
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


def from_images(text: str) -> list[str]:
    """The images a Dockerfile's FROM lines start from, leaving out its own earlier stages."""
    images: list[str] = []
    stages: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].upper() == "FROM":
            words = [p for p in parts[1:] if not p.startswith("--")]
            if words[0] not in stages:
                images.append(words[0])
            if len(words) == 3 and words[1].upper() == "AS":
                stages.add(words[2])
    return images


def test_a_stage_named_before_is_not_an_image() -> None:
    text = "FROM a:1@sha256:x AS base\nFROM base AS test\nFROM base\nFROM test\nFROM other"
    assert from_images(text) == ["a:1@sha256:x", "other"]


def test_every_from_is_pinned_by_digest() -> None:
    froms = [
        (path.parent.name, image) for path in DOCKERFILES for image in from_images(path.read_text())
    ]
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


def test_footfall_images_test_stage_is_built_only_when_asked_for() -> None:
    text = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    stages = re.findall(r"^FROM (\S+)(?: AS (\S+))?$", text, re.MULTILINE)
    # A build without --target builds the last stage, which must be the runtime image.
    assert stages[-1] == ("runtime", "")
    assert ("runtime", "test") in stages
    test = text.split("FROM runtime AS test", 1)[1].split("\nFROM ", 1)[0]
    assert "COPY tests ./tests" in test
    assert re.search(r"^RUN uv sync --locked \\$", test, re.MULTILINE)
    instructions = [line for line in test.splitlines() if line and not line.startswith("#")]
    assert instructions[-1] == "USER footfall"


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


def bundled_driver_version() -> str:
    """The Playwright release of the driver inside the installed Python package.

    playwright-python numbers its releases apart from Playwright's own: its 1.65.2 bundles
    Playwright 1.65.1. `playwright --version` prints the Python package's number. The
    driver's version is in its own package.json.
    """
    package = importlib.resources.files("playwright").joinpath("driver", "package", "package.json")
    return str(json.loads(package.read_text())["version"])


def test_browsers_image_runs_the_driver_the_locked_client_bundles() -> None:
    """The image's server and the client's driver are the same Playwright release.

    Microsoft tags the image by Playwright's own release. The image follows the driver inside
    the locked Python package, not that package's version.
    """
    locked = locked_playwright_version()
    assert importlib.metadata.version("playwright") == locked, "run uv sync first"
    driver = bundled_driver_version()
    wanted = f"the browser image must be Playwright {driver}, the driver in playwright {locked}"
    text = (ROOT / "images" / "browsers" / "Dockerfile").read_text()
    assert f"mcr.microsoft.com/playwright:v{driver}-noble@sha256:" in text, wanted
    assert f"npm install --global --ignore-scripts playwright@{driver}" in text, wanted
    assert re.search(r"^USER pwuser$", text, re.MULTILINE)


def test_the_browsers_image_serves_nothing_without_the_runners_command() -> None:
    """Ruling R68: run on its own, the image must not open run-server on every interface."""
    text = (ROOT / "images" / "browsers" / "Dockerfile").read_text()
    (command,) = re.findall(r"^CMD (.*)$", text, re.MULTILINE)
    arguments = json.loads(command)
    assert arguments[:2] == ["node", "-e"]
    assert "run-server" not in command and "0.0.0.0" not in command  # noqa: S104
    assert "process.exit(64)" in arguments[2]
    assert "serves nothing" in arguments[2]


def test_dependabot_moves_playwright_on_its_own() -> None:
    """A Playwright update needs the browser image in the same pull request.

    So Dependabot opens Playwright's update on its own, apart from the weekly Python group.
    In that group its failing image test would hold back every other update. Dependabot
    never moves the browser image by itself. CONTRIBUTING.md says how a person adds the
    image to Dependabot's pull request.
    """
    text = (ROOT / ".github" / "dependabot.yml").read_text()
    uv = text.split('package-ecosystem: "uv"', 1)[1].split("package-ecosystem:", 1)[0]
    assert re.search(r'patterns: \["\*"\]\n\s+exclude-patterns: \["playwright"\]', uv)
    docker = text.split('package-ecosystem: "docker"', 1)[1]
    assert re.search(r'- dependency-name: "mcr\.microsoft\.com/playwright"\n', docker)
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    assert "images/browsers/Dockerfile" in contributing
    assert "Dependabot" in contributing


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
    settings = json.loads((ROOT / policy).read_text())
    # Chromium's spellchecker downloads a dictionary from redirector.gvt1.com when a fresh
    # profile first sees a key press. Only the blocklist stops it. SpellcheckEnabled set to
    # false still downloads the dictionary and turns the blocklist off, so it stays unset.
    # The blocklist names all 57 languages for which Chromium has a dictionary, as
    # components/spellcheck/common/spellcheck_common.cc lists them, so a new locale in the
    # image brings no new download. en-US is the dictionary of the image's own locale.
    blocklist = settings.pop("SpellcheckLanguageBlocklist")
    assert "en-US" in blocklist
    assert len(set(blocklist)) == len(blocklist) == 57
    assert settings == {
        # The egress proxy refuses every ClientHello that hides its server name.
        "EncryptedClientHelloEnabled": False,
        # The rest would have Chromium call Google, which the proxy refuses: full Chromium
        # asked content-autofill.googleapis.com about the demo app's forms ten times in one
        # end-to-end run.
        "AutofillAddressEnabled": False,
        "AutofillCreditCardEnabled": False,
        "PasswordManagerEnabled": False,
        "TranslateEnabled": False,
        "SpellCheckServiceEnabled": False,
        "SearchSuggestEnabled": False,
        "SafeBrowsingProtectionLevel": 0,
        "MetricsReportingEnabled": False,
    }


def test_footfall_image_has_what_playwrights_driver_needs() -> None:
    # The orchestrator drives the browsers through Playwright's bundled Node, which will not
    # start without libatomic, and the slim base image leaves it out.
    text = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    assert re.search(r"apt-get install --yes --no-install-recommends git libatomic1\b", text)


def test_footfall_image_installs_git_from_its_own_release() -> None:
    # footfall test lists the files git tracks; the base image's Debian release
    # supplies git, with its security updates, and nothing it merely recommends.
    text = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    runtime = text.split(
        "# ---------------------------------------------------------------- runtime"
    )[1]
    install = re.search(r"apt-get install --yes --no-install-recommends git\b", runtime)
    assert install is not None
    assert "rm -rf /var/lib/apt/lists/*" in runtime
    # Only the dev stack's mount point is trusted, whoever owns it, and only system-wide.
    assert re.search(r"&& git config --system --add safe\.directory /repo\b", runtime)
    assert re.findall(r"safe\.directory\s+(\S+)", text) == ["/repo"]
    assert "safe.directory=" not in text
    assert "safe.directory" not in (ROOT / "footfall" / "local.py").read_text()
    assert re.search(r"^FROM python:[^ ]+-trixie@sha256:", runtime, re.MULTILINE)


# ---------------------------------------------------------------- the build context


def _pattern(text: str) -> re.Pattern[str]:
    """A .dockerignore pattern as a regular expression, as Docker's matcher reads it: `*`
    and `?` stay within one folder, `**` and `**/` stand for any folders or none, a final
    `**` for anything, and `[...]` is a character class."""
    parts = []
    index = 0
    clean = text.strip().strip("/")
    while index < len(clean):
        if clean.startswith("**", index):
            # As Docker's patternmatcher compiles it: a / after ** is eaten, and ** stands for
            # any folders or none, except at the end, where it stands for anything.
            index += 2
            if clean.startswith("/", index):
                index += 1
            parts.append(".*" if index == len(clean) else "(?:.*/)?")
        elif clean[index] == "[" and "]" in clean[index + 1 :]:
            end = clean.index("]", index + 1)
            parts.append(clean[index : end + 1])
            index = end + 1
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


def sources_in(dockerfile: str) -> list[str]:
    """Every COPY or ADD source in a Dockerfile's text that comes from the build context.

    Raises ValueError for a form this reading does not follow: an instruction continued on
    the next line, or one in JSON array form, which must not slip past the check unread.
    """
    sources = []
    for line in dockerfile.splitlines():
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        if line.rstrip().endswith("\\"):
            raise ValueError(f"a continued {words[0]} is not read here: {line!r}")
        arguments = [w for w in words[1:] if not w.startswith("--")]
        if arguments and arguments[0].startswith("["):
            raise ValueError(f"a {words[0]} in JSON form is not read here: {line!r}")
        if any(w.startswith("--from=") for w in words[1:]):
            continue
        sources += arguments[:-1]
    return sources


def copy_sources() -> list[tuple[str, str]]:
    """Every COPY or ADD source in every Dockerfile that comes from the build context."""
    return [
        (path.parent.name, source)
        for path in DOCKERFILES
        for source in sources_in(path.read_text())
    ]


def test_add_counts_and_unread_forms_are_refused() -> None:
    assert sources_in("ADD site.tar /srv/\nCOPY --from=build /app /app\nCOPY a b ./") == [
        "site.tar",
        "a",
        "b",
    ]
    with pytest.raises(ValueError, match="continued"):
        sources_in("COPY a \\\n  b ./")
    with pytest.raises(ValueError, match="JSON"):
        sources_in('COPY ["gate.js", "/opt/footfall/gate.js"]')


def test_the_matcher_reads_patterns_as_docker_does() -> None:
    # `**/` may match no folder at all, and a bracket is a character class.
    assert not admitted("stray.pyc", "**/*.pyc")
    assert not admitted("deep/down/stray.pyc", "**/*.pyc")
    assert admitted("stray.py", "**/*.pyc")
    assert not admitted("notes3.txt", "notes[0-9].txt")
    assert admitted("notesX.txt", "notes[0-9].txt")
    assert not admitted("a/b", "a/*")
    # A ** inside a pattern stands for any folders, or none, as Docker reads it.
    assert not admitted("a/b", "a/**b")
    assert not admitted("a/x/y/b", "a/**b")
    assert admitted("a/xb", "a/**b")
    assert admitted("a/b/c", "*/b\n!a/b/c") is True


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


@pytest.mark.parametrize(
    "secret", ["footfall/.env", "tests/.env.local", "tests/keys/server.pem", "footfall/deploy.key"]
)
def test_no_secret_a_developer_keeps_beside_the_code_enters_an_image(secret: str) -> None:
    """Audit N-12: the build context takes footfall/ and tests/ whole."""
    ignore = (ROOT / ".dockerignore").read_text()
    assert not admitted(secret, ignore)
    assert admitted("footfall/local.py", ignore)  # the control
    assert admitted("tests/test_local.py", ignore)


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
