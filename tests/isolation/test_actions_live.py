"""That a secret Footfall masks is hidden in the GitHub Actions log of the isolation run.

The isolation workflow runs pytest in the orchestrator through `docker compose exec`, so the
orchestrator's standard output is the step's, which the Actions runner reads as it goes.
This test masks a value made for it alone, as Footfall masks a password, then prints it.
The run's log must show `FOOTFALL-MASK-CHECK ***`: the workflow's masks job reads the log
through the API and fails the run unless it does, and unless the value never shows in clear.
Were the mask to fail, the log would show a random value that guards nothing.
"""

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from footfall.actions import mask
from tests.isolation.live import DEMO_APP, HERE

pytestmark = pytest.mark.isolation


def test_a_masked_value_is_hidden_in_the_actions_log() -> None:
    if os.environ.get("FOOTFALL_TEST_HOST_PORT") and os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.fail("the isolation workflow passes GITHUB_ACTIONS to the orchestrator")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("only a GitHub Actions job reads ::add-mask::")
    value = "footfall-mask-check-" + secrets.token_urlsafe(16)
    mask(value)
    os.write(1, f"FOOTFALL-MASK-CHECK {value}\n".encode())


# An install that prints two workflow commands, the current form and the legacy one, and fails.
FORGER = (
    """install = "printf '%s\\\\n' '::error title=FOOTFALL-FORGED::a forged annotation' """
    """'##[error]FOOTFALL-FORGED in the legacy form'; exit 3"\n"""
)


def test_the_apps_last_output_gives_the_job_no_command(tmp_path: Path) -> None:
    """Audit F-03 in a real job: an app whose install prints workflow commands and fails.
    footfall test prints the app's last output on its standard error, which this test passes
    on to the step's own, so the Actions runner reads it; the masks job then fails the run if
    the job has an annotation the app forged, and unless the log shows the lines as text."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("only a GitHub Actions job reads workflow commands")
    folder = tmp_path / "forger"
    shutil.copytree(DEMO_APP, folder, ignore=shutil.ignore_patterns("__pycache__", "*.sqlite3"))
    manifest = folder / "footfall.toml"
    manifest.write_text(manifest.read_text().replace("[app]\n", "[app]\n" + FORGER, 1))
    script = HERE.parent / "fixtures" / "scripts" / "demo-walkthrough.json"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "footfall",
            "test",
            str(folder),
            "--all-files",
            "--model",
            f"fake:{script}",
            "--personas",
            "largest-text",
            "--out",
            str(tmp_path / "results"),
        ],
        stderr=subprocess.PIPE,
        text=True,
        timeout=600,
        check=False,
        env={**os.environ, "ANTHROPIC_API_KEY": ""},
    )
    os.write(2, done.stderr.encode())  # the step's own standard error, which the runner reads
    assert done.returncode == 1, done.stderr[-2000:]
    assert "install failed with exit code 3" in done.stderr
    assert "| ::error title=FOOTFALL-FORGED::a forged annotation" in done.stderr
    assert "| ## [error]FOOTFALL-FORGED in the legacy form" in done.stderr
    for line in done.stderr.splitlines():
        assert not line.lstrip().startswith("::"), line
        assert "##[" not in line, line
