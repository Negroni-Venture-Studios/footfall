"""The isolation tests' own guard: they refuse to run without gVisor."""

import re
from pathlib import Path

import pytest

from tests.isolation.conftest import refusal
from tests.isolation.live import allowed, decision_outcome, is_private, refused


def test_the_isolation_tests_refuse_runc() -> None:
    reason = refusal({"FOOTFALL_ISOLATION": "1", "FOOTFALL_RUNTIME": "runc"})
    assert reason is not None
    assert "FOOTFALL_RUNTIME=runc" in reason


def test_they_run_under_gvisor_and_skip_without_the_setting() -> None:
    assert refusal({"FOOTFALL_ISOLATION": "1", "FOOTFALL_RUNTIME": "runsc"}) is None
    assert refusal({"FOOTFALL_ISOLATION": "1"}) is None
    # Without FOOTFALL_ISOLATION the folder is skipped, so runc there stops nothing.
    assert refusal({"FOOTFALL_RUNTIME": "runc"}) is None


@pytest.mark.parametrize(
    ("host", "reachable"),
    [
        ("172.18.0.2", True),
        ("10.1.0.4", True),
        ("127.0.0.1", True),
        ("169.254.169.254", False),  # the cloud's metadata service: private, but never ours
        ("fe80::1", False),
        ("0.0.0.0", False),  # noqa: S104
        ("::", False),
        ("8.8.8.8", False),
        ("footfall-proxy", False),  # a name, not an address
    ],
)
def test_the_isolation_tests_may_connect_to_private_addresses_only(
    host: str, reachable: bool
) -> None:
    assert is_private(host) is reachable


LINE = (
    "2026-11-25 08:39:20,888 INFO footfall.proxy.server: 172.18.0.4 run=r_x host=pypi.org port=443 "
)


@pytest.mark.parametrize(
    ("outcome", "is_allowed", "is_refused"),
    [
        ("allowed", True, False),
        ("refused (403): pypi.org is not on this client's allow list", False, True),
        ("refused: the TLS ClientHello hides its server name", False, True),
        # Refusal reasons that hold the word "allowed" are refusals all the same.
        ("refused (403): CONNECT is allowed only to port 443", False, True),
        ("refused (405): method not allowed", False, True),
    ],
)
def test_a_proxy_decision_is_judged_by_its_outcome_not_a_word_in_it(
    outcome: str, is_allowed: bool, is_refused: bool
) -> None:
    assert decision_outcome(LINE + outcome) == outcome
    assert allowed(LINE + outcome) is is_allowed
    assert refused(LINE + outcome) is is_refused


def test_a_refusal_can_be_matched_by_its_reason() -> None:
    line = LINE + "refused (403): pypi.org is not on this client's allow list"
    assert refused(line, "is not on this client's allow list")
    assert not refused(line, "not a valid host name")
    assert not refused(LINE + "allowed", "allowed")


ROOT = Path(__file__).resolve().parent.parent


def test_the_masks_reach_the_log_before_anything_else() -> None:
    """Footfall writes ::add-mask:: to file descriptor 1, which pytest's default capture
    would hold until after a failure's traceback; the isolation run captures at the sys
    level only, and the end-to-end run's footfall test writes straight to the job's output."""
    workflow = (ROOT / ".github" / "workflows" / "isolation.yml").read_text()
    pytest_line = re.search(r"^\s+PYTEST: (.+)$", workflow, re.MULTILINE)
    assert pytest_line is not None
    assert "--capture=sys" in pytest_line.group(1).split()
    end_to_end = (ROOT / "tests" / "isolation" / "test_end_to_end.py").read_text()
    assert "capture_output" not in end_to_end
    assert "stdout=" not in end_to_end
