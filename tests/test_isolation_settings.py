"""The isolation tests' own guard: they refuse to run without gVisor."""

from tests.isolation.conftest import refusal


def test_the_isolation_tests_refuse_runc() -> None:
    reason = refusal({"FOOTFALL_ISOLATION": "1", "FOOTFALL_RUNTIME": "runc"})
    assert reason is not None
    assert "FOOTFALL_RUNTIME=runc" in reason


def test_they_run_under_gvisor_and_skip_without_the_setting() -> None:
    assert refusal({"FOOTFALL_ISOLATION": "1", "FOOTFALL_RUNTIME": "runsc"}) is None
    assert refusal({"FOOTFALL_ISOLATION": "1"}) is None
    # Without FOOTFALL_ISOLATION the folder is skipped, so runc there stops nothing.
    assert refusal({"FOOTFALL_RUNTIME": "runc"}) is None
