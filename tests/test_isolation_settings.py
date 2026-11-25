"""The isolation tests' own guard: they refuse to run without gVisor."""

import pytest

from tests.isolation.conftest import refusal
from tests.isolation.live import is_private


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
