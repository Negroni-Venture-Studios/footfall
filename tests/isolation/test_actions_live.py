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

import pytest

from footfall.actions import mask

pytestmark = pytest.mark.isolation


def test_a_masked_value_is_hidden_in_the_actions_log() -> None:
    if os.environ.get("FOOTFALL_TEST_HOST_PORT") and os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.fail("the isolation workflow passes GITHUB_ACTIONS to the orchestrator")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("only a GitHub Actions job reads ::add-mask::")
    value = "footfall-mask-check-" + secrets.token_urlsafe(16)
    mask(value)
    os.write(1, f"FOOTFALL-MASK-CHECK {value}\n".encode())
