"""The development compose file: what each service may hold and reach, since CI cannot run it.

The file is read as text, a block for each service, so that no YAML library is needed.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "deploy" / "dev" / "compose.yaml").read_text()
SERVICES = ("proxy", "runner", "orchestrator")


def services() -> dict[str, str]:
    """Each service's lines, comments left out, keyed by its name."""
    body = COMPOSE.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks: dict[str, list[str]] = {}
    name = ""
    for line in body.splitlines():
        found = re.fullmatch(r"  ([a-z]+):", line)
        if found:
            name = found.group(1)
            blocks[name] = []
        elif name and not line.strip().startswith("#"):
            blocks[name].append(line.split("  #")[0].rstrip())
    return {key: "\n".join(lines) for key, lines in blocks.items()}


BLOCKS = services()


def test_three_services_from_the_dev_image() -> None:
    assert tuple(BLOCKS) == SERVICES
    for block in BLOCKS.values():
        assert "    image: footfall:dev\n" in block


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_is_hardened(name: str) -> None:
    block = BLOCKS[name]
    assert "    read_only: true" in block
    assert "    tmpfs:\n      - /tmp" in block
    assert "    cap_drop:\n      - ALL" in block
    assert "    security_opt:\n      - no-new-privileges:true" in block
    assert "    logging: *logging" in block
    assert "    init: true" in block  # a first process that reaps and passes signals on
    assert "privileged" not in block
    assert "ports:" not in block  # nothing listens on the host
    assert 'max-size: "10m"' in COMPOSE


def test_container_names_match_the_settings() -> None:
    for name in SERVICES:
        assert f"    container_name: footfall-{name}\n" in BLOCKS[name] + "\n"
    assert "FOOTFALL_PROXY_CONTAINER: footfall-proxy" in BLOCKS["runner"]
    assert "FOOTFALL_ORCHESTRATOR_CONTAINER: footfall-orchestrator" in BLOCKS["runner"]
    assert "FOOTFALL_PROXY_CONTAINER=footfall-proxy" in BLOCKS["orchestrator"]


def test_only_the_runner_holds_the_docker_socket() -> None:
    assert "/var/run/docker.sock:/var/run/docker.sock" in BLOCKS["runner"]
    assert "docker.sock" not in BLOCKS["proxy"] + BLOCKS["orchestrator"]


def test_the_runner_is_root_with_three_capabilities_and_no_network() -> None:
    block = BLOCKS["runner"]
    assert '    user: "0:10001"' in block
    added = re.search(r"    cap_add:\n((?:      - [A-Z_]+\n?)+)", block)
    assert added is not None
    assert re.findall(r"- ([A-Z_]+)", added.group(1)) == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert "    network_mode: none" in block
    # Docker reads bind sources on the host, so the runs folder keeps its path.
    assert "      - /var/lib/footfall:/var/lib/footfall" in block


def test_the_key_reaches_only_the_orchestrator() -> None:
    assert "      - ANTHROPIC_API_KEY\n" in BLOCKS["orchestrator"] + "\n"
    assert "ANTHROPIC" not in BLOCKS["proxy"] + BLOCKS["runner"]
    assert "env_file" not in COMPOSE


def test_the_orchestrator_listens_on_nothing_and_reads_the_app_only() -> None:
    block = BLOCKS["orchestrator"]
    assert '    entrypoint: ["sleep", "infinity"]' in block
    # The whole repository, its .git included, so that git can say what it would commit.
    assert "      - ${FOOTFALL_REPO:-../..}:/repo:ro" in block
    assert "--all-files" not in COMPOSE
    assert "      - runner-socket:/run/footfall" in block
    assert "proxy-control" not in block  # it cannot reach the proxy's control socket


def test_only_the_proxy_and_the_runner_share_the_control_socket() -> None:
    for name in ("proxy", "runner"):
        assert "      - proxy-control:/run/footfall-proxy" in BLOCKS[name]
        assert "FOOTFALL_PROXY_CONTROL: /run/footfall-proxy/proxy.sock" in BLOCKS[name]
    assert "network_mode: bridge" in BLOCKS["proxy"]


def test_the_image_makes_the_socket_folders() -> None:
    dockerfile = (ROOT / "images" / "footfall" / "Dockerfile").read_text()
    assert "--owner root --group footfall --mode 0750 /run/footfall " in dockerfile
    assert "--owner footfall --group footfall --mode 0700 /run/footfall-proxy" in dockerfile


TEST_OVERLAY = (ROOT / "deploy" / "dev" / "compose.test.yaml").read_text()


def overlay_blocks(text: str = TEST_OVERLAY) -> dict[str, str]:
    """Each service's lines in the test overlay, comments left out, keyed by its name."""
    body = text.split("\nservices:\n", 1)[1]
    blocks: dict[str, list[str]] = {}
    name = ""
    for line in body.splitlines():
        found = re.fullmatch(r"  ([a-z]+):", line)
        if found:
            name = found.group(1)
            blocks[name] = []
        elif name and line.strip() and not line.strip().startswith("#"):
            blocks[name].append(line.rstrip())
    return {key: "\n".join(lines) for key, lines in blocks.items()}


def test_the_test_overlay_widens_only_what_r54_allows() -> None:
    # The isolation tests must test the stack's own settings. The overlay may run the
    # orchestrator from the test stage (R7), copy the proxy's log to a host folder, give
    # the runner a small disk budget (R54), and tell the orchestrator whether it runs in
    # GitHub Actions, so that it masks its secrets in the log, and nothing else: no socket,
    # port, capability, privilege, user or network, and no hardening changed.
    assert overlay_blocks() == {
        "proxy": "\n".join(
            [
                "    entrypoint:",
                "      - sh",
                "      - -c",
                "      - 'footfall \"$$@\" 2>&1 | tee -a /var/lib/footfall-test/proxy.log'",
                "      - sh",
                "    volumes:",
                "      - /var/lib/footfall/test-logs:/var/lib/footfall-test",
            ]
        ),
        "runner": "\n".join(["    environment:", '      FOOTFALL_DISK_BUDGET_MIB: "128"']),
        "orchestrator": "\n".join(
            [
                "    image: footfall:test",
                "    environment:",
                "      - FOOTFALL_ISOLATION=1",
                "      - FOOTFALL_TEST_HOST_ADDRESSES=${FOOTFALL_TEST_HOST_ADDRESSES:-}",
                "      - FOOTFALL_TEST_HOST_PORT=${FOOTFALL_TEST_HOST_PORT:-}",
                "      - GITHUB_ACTIONS=${GITHUB_ACTIONS:-}",
            ]
        ),
    }
    assert preamble_is_only_comments(TEST_OVERLAY)
    assert not re.search(r"^\S", TEST_OVERLAY.split("\nservices:\n", 1)[1], re.MULTILINE)


def preamble_is_only_comments(text: str) -> bool:
    """Whether every line before `services:` is blank or a comment, so that no top-level key,
    such as an include of another compose file, networks, configs or secrets, comes in."""
    head = text.split("\nservices:\n", 1)[0]
    return all(not line.strip() or line.lstrip().startswith("#") for line in head.splitlines())


@pytest.mark.parametrize(
    "key",
    ["include:\n  - ../prod/compose.yaml", "networks:\n  outside: {}", "secrets:\n  k: {}"],
)
def test_the_overlay_check_sees_a_top_level_key(key: str) -> None:
    assert not preamble_is_only_comments(f"# a comment\n{key}\n\nservices:\n  proxy:\n")
    assert preamble_is_only_comments("# a comment\n\nservices:\n  proxy:\n")


def test_the_overlay_check_sees_an_added_volume() -> None:
    widened = TEST_OVERLAY.replace(
        "    image: footfall:test\n", "    image: footfall:test\n    volumes:\n      - /:/host\n"
    )
    assert "      - /:/host" in overlay_blocks(widened)["orchestrator"]
    assert overlay_blocks(widened) != overlay_blocks()
