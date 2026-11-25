"""`footfall test` from start to finish in the dev stack, with a scripted model.

It runs the real command in the orchestrator container against the demo app as the stack
mounts it, at /repo/demo-app with the repository's .git, so the copy is git's and needs no
`--all-files`. Two personas on two engines sign in, walk the app and report. The workflow
measures each container's memory meanwhile, and `test_docker_host.py` checks afterwards, on
the host, that nothing labelled with the run is left.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.isolation.live import DEMO_APP, HERE, WORKSPACES, ProxyLog, record_result

pytestmark = pytest.mark.isolation

SCRIPT = HERE.parent / "fixtures" / "scripts" / "demo-walkthrough.json"
PERSONAS = ("sven", "grace")


def test_footfall_test_runs_the_demo_app_end_to_end(tmp_path: Path, proxy_log: ProxyLog) -> None:
    out = tmp_path / "results"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "footfall",
            "test",
            str(DEMO_APP),
            "--model",
            f"fake:{SCRIPT}",
            "--personas",
            ",".join(PERSONAS),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
        env={**os.environ, "ANTHROPIC_API_KEY": ""},
    )
    assert done.returncode == 0, done.stderr[-4000:]
    assert "--all-files" not in done.stderr
    summary = json.loads((out / "summary.json").read_text())
    record_result("end-to-end", {"run_id": summary["run_id"]})
    assert [p["persona"] for p in summary["personas"]] == list(PERSONAS)
    assert [p["status"] for p in summary["personas"]] == ["finished", "finished"]
    for persona in PERSONAS:
        report = json.loads((out / persona / "report.json").read_text())
        assert report["status"] == "finished"
        assert [f["title"] for f in report["findings"]] == [
            "The settings page has no way back to the list"
        ]
        steps = [
            json.loads(line) for line in (out / persona / "trace.jsonl").read_text().splitlines()
        ]
        # Signed in with the account the seed step made, so /settings opens rather than
        # sending the persona to the sign-in page.
        assert [(s["tool"], s["path_after"]) for s in steps] == [
            ("go", "/settings"),
            ("go", "/"),
            ("finish", "/"),
        ]
        shots = sorted(p.name for p in (out / persona / "screenshots").iterdir())
        assert len(shots) >= 3, shots
    # The run's workspace is gone too.
    assert not (WORKSPACES / summary["run_id"]).exists()
    # The demo app names no egress and no assets, so nothing in the run asked the proxy for
    # anything: not the app, and not the browsers, whose calls to Google the policy stops.
    assert proxy_log.all_decisions() == []
