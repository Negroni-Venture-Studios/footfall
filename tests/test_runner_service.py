"""The runner's logic, against a fake Docker engine and a fake proxy, with real folders."""

import errno
import inspect
import os
import re
import shutil
import stat
import threading
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

import pytest

from footfall.runner.engine import EngineError
from footfall.runner.service import (
    DISK_REASON,
    MAX_DEPTH,
    MEASURE_REASON,
    NEVER_PREPARED,
    REGISTRIES,
    UNSTEADY_REASON,
    RunnerError,
    RunnerService,
    _copy_file,
    _copy_tree,
    _disk_usage,
    _entries,
    _measure,
    _OverBudget,
    _size,
    _TooDeep,
)
from footfall.runner.spec import container_config, network_config
from tests.runner_fakes import (
    BROWSERS_IMAGE,
    IMAGES,
    ORCHESTRATOR,
    OUTSIDE_IPS,
    PROXY,
    FakeEngine,
    FakeProxy,
    Harness,
    make_harness,
)

RUN = "r_abcdefghijklmnopqrstuvwxyz"
OTHER = "r_" + "q" * 26
NETWORK = f"footfall-{RUN}"
INSTALL = f"footfall-{RUN}-install-prepare"
SEED = f"footfall-{RUN}-seed-prepare"
PASSWORD = "pw-7f3k2m9q4x8z"  # gitleaks:allow
MIB = 1024 * 1024
PAGE = 4096


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return make_harness(tmp_path)


def prepare(
    h: Harness,
    *,
    run_id: str = RUN,
    workspace: Path | None = None,
    runtime: str = "python3.13",
    install: str | None = None,
    seed: str | None = None,
    egress: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> None:
    if workspace is None:
        existing = h.workspaces / "app"
        workspace = existing if existing.exists() else h.workspace()
    h.service.prepare(run_id, workspace, runtime, install, seed, egress, env or {})


def app_name(copy: str, run_id: str = RUN) -> str:
    return f"footfall-{run_id}-app-{copy}"


def assert_nothing_left(h: Harness, run_id: str = RUN) -> None:
    assert h.engine.live == {}
    assert h.engine.networks == {}
    assert h.engine.attached == set()
    assert h.proxy.allowed == {}
    assert not (h.runs / run_id).exists()


# ---------------------------------------------------------------- prepare


def test_prepare_runs_install_then_seed_with_their_allow_lists(h: Harness) -> None:
    workspace = h.workspace(files={"serve.py": "print('hi')\n", "lib/util.py": "X = 1\n"})
    h.service.prepare(
        RUN,
        workspace,
        "python3.13",
        "pip install -r requirements.txt",
        "python3 seed.py",
        ["Api.Example.com", "data.example.org", "api.example.com"],
        {"FOOTFALL_USERNAME": "tester", "FOOTFALL_PASSWORD": PASSWORD},
    )

    assert h.engine.names("create") == [INSTALL, SEED]
    assert ("create_network", NETWORK, network_config(RUN)) in h.engine.calls
    assert h.engine.attached == {(NETWORK, PROXY), (NETWORK, ORCHESTRATOR)}

    prepared = h.runs / RUN / "prepared"
    install, seed = h.engine.configs[INSTALL], h.engine.configs[SEED]
    assert install["Cmd"] == ["sh", "-c", "pip install -r requirements.txt"]
    assert seed["Cmd"] == ["sh", "-c", "python3 seed.py"]
    for config, role in ((install, "install"), (seed, "seed")):
        assert config["Image"] == "footfall-python3.13:dev"
        assert config["Labels"] == {
            "footfall.run": RUN,
            "footfall.role": role,
            "footfall.copy": "prepare",
        }
        assert config["HostConfig"]["Binds"] == [f"{prepared}:/workspace:rw"]
        assert config["HostConfig"]["NetworkMode"] == NETWORK
        assert config["HostConfig"]["Runtime"] == "runsc"
        assert f"HTTPS_PROXY=http://{OUTSIDE_IPS[PROXY]}:3128" in config["Env"]
    # The throwaway password goes to the seed step and nowhere else.
    assert not any(item.startswith("FOOTFALL_") and item != "FOOTFALL=1" for item in install["Env"])
    assert f"FOOTFALL_PASSWORD={PASSWORD}" in seed["Env"]
    assert "FOOTFALL_USERNAME=tester" in seed["Env"]

    hosts = ("api.example.com", "data.example.org")
    install_ip, seed_ip = h.engine.ips[INSTALL], h.engine.ips[SEED]
    assert [call for call in h.proxy.calls if call[0] == "allow"] == [
        ("allow", install_ip, RUN, (*REGISTRIES, *hosts)),
        ("allow", seed_ip, RUN, hosts),
    ]
    # Install finishes, loses its allow list and is removed before seed is created.
    calls = h.engine.calls
    assert calls.index(("remove", INSTALL)) < calls.index(("create", SEED))
    assert h.proxy.calls.index(("revoke", install_ip)) < h.proxy.calls.index(
        ("allow", seed_ip, RUN, hosts)
    )
    assert ("revoke", seed_ip) in h.proxy.calls
    assert h.proxy.allowed == {}
    assert h.engine.live == {}
    assert (prepared / "serve.py").read_text() == "print('hi')\n"
    assert (prepared / "lib" / "util.py").read_text() == "X = 1\n"


def test_prepare_without_install_or_seed(h: Harness) -> None:
    h.workspace(files={"serve.py": "print('hi')\n", "lib/util.py": "X = 1\n"})
    prepare(h)
    assert h.engine.names("create") == []
    assert h.proxy.calls == []
    assert h.engine.attached == {(NETWORK, PROXY), (NETWORK, ORCHESTRATOR)}
    prepared = h.runs / RUN / "prepared"
    assert (prepared / "lib" / "util.py").read_text() == "X = 1\n"
    assert set(h.chowned) == {
        prepared,
        prepared / "serve.py",
        prepared / "lib",
        prepared / "lib" / "util.py",
    }


def test_install_failure_raises_with_log_tail_and_cleans_its_container(h: Harness) -> None:
    h.engine.exit_codes["install"] = 2
    h.engine.output["install"] = "ERROR: No matching distribution found for nothing\n"
    with pytest.raises(RunnerError) as caught:
        prepare(h, install="pip install nothing", seed="python3 seed.py")
    assert str(caught.value) == "install failed with exit code 2"
    assert caught.value.log_tail == "ERROR: No matching distribution found for nothing\n"
    assert SEED not in h.engine.names("create")
    assert ("remove", INSTALL) in h.engine.calls
    assert ("revoke", h.engine.ips[INSTALL]) in h.proxy.calls
    assert_nothing_left(h)
    assert h.service.status(RUN) is None


def test_step_timeout_raises(tmp_path: Path) -> None:
    h = make_harness(tmp_path, step_timeout=7)
    h.engine.slow.add("seed")
    h.engine.output["seed"] = "seeding 1 of 1000000\n"
    with pytest.raises(RunnerError) as caught:
        prepare(h, seed="python3 seed.py --forever")
    assert str(caught.value) == "seed did not finish within 7 seconds"
    assert caught.value.log_tail == "seeding 1 of 1000000\n"
    assert ("wait", SEED, 7) in h.engine.calls
    assert_nothing_left(h)


def test_prepare_twice_refused(h: Harness) -> None:
    prepare(h)
    with pytest.raises(RunnerError, match="already exists"):
        prepare(h)
    assert (h.runs / RUN / "prepared" / "serve.py").exists()
    assert h.engine.names("create_network") == [NETWORK]


def test_prepare_refuses_a_run_folder_left_behind(h: Harness) -> None:
    (h.runs / RUN).mkdir(parents=True)
    (h.runs / RUN / "keep").write_text("from an earlier runner\n")
    with pytest.raises(RunnerError, match="already exists"):
        prepare(h)
    assert h.engine.calls == []
    assert (h.runs / RUN / "keep").exists()


# ---------------------------------------------------------------- copies and apps


def test_each_copy_gets_its_own_workspace(h: Harness) -> None:
    h.engine.during_wait["seed"] = lambda w: (w / "data.json").write_text('{"tasks": 12}')
    prepare(h, seed="python3 seed.py", egress=["api.example.com"])

    anna = h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    malik = h.service.start_app(RUN, "malik", "python3 serve.py --quiet", 8123, [])
    assert anna == f"{h.engine.ips[app_name('anna')]}:8000"
    assert malik == f"{h.engine.ips[app_name('malik')]}:8123"

    prepared = h.runs / RUN / "prepared"
    copy_a, copy_m = h.runs / RUN / "copies" / "anna", h.runs / RUN / "copies" / "malik"
    for folder in (copy_a, copy_m):
        assert (folder / "data.json").read_text() == '{"tasks": 12}'
    (copy_a / "data.json").write_text("changed by anna")
    (copy_a / "new.txt").write_text("only anna")
    assert (copy_m / "data.json").read_text() == '{"tasks": 12}'
    assert (prepared / "data.json").read_text() == '{"tasks": 12}'
    assert not (copy_m / "new.txt").exists()

    config = h.engine.configs[app_name("anna")]
    assert config["Cmd"] == ["sh", "-c", "python3 serve.py"]
    assert config["Image"] == "footfall-python3.13:dev"
    assert config["HostConfig"]["Binds"] == [f"{copy_a}:/workspace:rw"]
    assert config["Labels"] == {
        "footfall.run": RUN,
        "footfall.role": "app",
        "footfall.copy": "anna",
    }
    assert "PORT=8000" in config["Env"]
    assert not any(item.startswith("FOOTFALL_PASSWORD") for item in config["Env"])
    assert h.engine.configs[app_name("malik")]["HostConfig"]["Binds"] == [f"{copy_m}:/workspace:rw"]
    assert h.proxy.allowed[h.engine.ips[app_name("anna")]] == (RUN, ("api.example.com",))
    assert h.proxy.allowed[h.engine.ips[app_name("malik")]] == (RUN, ())
    assert copy_a in h.chowned and copy_a / "data.json" in h.chowned


def test_duplicate_copy_refused(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    with pytest.raises(RunnerError, match="already exists"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert h.engine.names("create") == [app_name("anna")]


def test_start_app_failure_cleans_up_its_copy(h: Harness) -> None:
    prepare(h)
    h.engine.fail("start", EngineError("OCI runtime create failed"), role="app")
    with pytest.raises(RunnerError, match="OCI runtime create failed"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert app_name("anna") not in h.engine.live
    assert not (h.runs / RUN / "copies" / "anna").exists()
    with pytest.raises(RunnerError, match="no copy"):
        h.service.logs(RUN, "anna")

    h.engine.failures.clear()
    assert h.service.start_app(RUN, "anna", "python3 serve.py", 8000, []).endswith(":8000")


def test_start_app_failure_after_allow_revokes_it(h: Harness) -> None:
    prepare(h)
    h.proxy.failures["allow"] = ConnectionRefusedError(61, "Connection refused")
    with pytest.raises(RunnerError, match="Connection refused"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    ip = h.engine.ips[app_name("anna")]
    assert ("revoke", ip) in h.proxy.calls
    assert app_name("anna") not in h.engine.live


@pytest.mark.parametrize("port", [0, 65536, -1, True])
def test_bad_port_refused(h: Harness, port: int) -> None:
    prepare(h)
    with pytest.raises(RunnerError, match="port"):
        h.service.start_app(RUN, "anna", "python3 serve.py", port, [])
    assert h.engine.names("create") == []


@pytest.mark.parametrize("start", ["", "   ", "python3\x00 serve.py"])
def test_bad_start_command_refused(h: Harness, start: str) -> None:
    prepare(h)
    with pytest.raises(RunnerError, match="start"):
        h.service.start_app(RUN, "anna", start, 8000, [])
    assert h.engine.names("create") == []


def browsers_path(config: Mapping[str, Any]) -> str:
    """The secret path a browser container was started with: the gate listens on port 3000
    and passes the path's upgrades alone to run-server, which also checks it, on loopback."""
    command: list[str] = config["Cmd"]
    path = command[9]
    assert command == [
        "node",
        "/opt/footfall/gate.js",
        "--host",
        "0.0.0.0",  # noqa: S104
        "--port",
        "3000",
        "--upstream",
        "3001",
        "--path",
        path,
        "--",
        "playwright",
        "run-server",
        "--port",
        "3001",
        "--host",
        "127.0.0.1",
        "--path",
        path,
    ]
    return path


def test_start_browsers_runs_run_server_with_the_assets_hosts(h: Harness) -> None:
    prepare(h, egress=["api.example.com"])
    endpoint = h.service.start_browsers(RUN, ["fonts.gstatic.com", "Tiles.Example.org"])
    name = f"footfall-{RUN}-browsers-browsers"
    config = h.engine.configs[name]
    path = browsers_path(config)
    assert endpoint == f"ws://{h.engine.ips[name]}:3000{path}"
    assert config["Image"] == BROWSERS_IMAGE
    assert config["Labels"] == {
        "footfall.run": RUN,
        "footfall.role": "browsers",
        "footfall.copy": "browsers",
    }
    assert "Binds" not in config["HostConfig"]
    assert "User" not in config
    assert h.proxy.allowed[h.engine.ips[name]] == (RUN, ("fonts.gstatic.com", "tiles.example.org"))
    # Docker's name service does not answer inside gVisor: the proxy's name is in the hosts
    # file, at its address on the run's network, for every container the run starts.
    proxy_address = h.engine.address(PROXY, f"footfall-{RUN}")
    for each in h.engine.configs.values():
        assert each["HostConfig"]["ExtraHosts"] == [f"{PROXY}:{proxy_address}"]
    with pytest.raises(RunnerError, match="already"):
        h.service.start_browsers(RUN, [])


def test_each_run_server_gets_a_fresh_secret_path(h: Harness) -> None:
    # Code under test shares the run's network with the browsers, so the server answers only
    # on a path that the orchestrator alone is told.
    paths = []
    for run_id in (RUN, OTHER):
        prepare(h, run_id=run_id)
        endpoint = h.service.start_browsers(run_id, [])
        name = f"footfall-{run_id}-browsers-browsers"
        path = browsers_path(h.engine.configs[name])
        assert re.fullmatch(r"/[A-Za-z0-9_-]{32}", path)
        assert endpoint == f"ws://{h.engine.ips[name]}:3000{path}"
        paths.append(path)
    assert paths[0] != paths[1]
    for config in h.engine.configs.values():
        assert not any(path in " ".join(config["Env"]) for path in paths)


def test_logs_returns_the_copy_tail_with_the_password_redacted(h: Harness) -> None:
    prepare(h, env={"FOOTFALL_USERNAME": "tester", "FOOTFALL_PASSWORD": PASSWORD})
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.engine.output["app"] = f"GET / 200\nsigned in tester with {PASSWORD}\n"
    assert h.service.logs(RUN, "anna") == "GET / 200\nsigned in tester with [redacted]\n"
    assert ("logs", app_name("anna"), 200) in h.engine.calls
    with pytest.raises(RunnerError, match="no copy"):
        h.service.logs(RUN, "malik")


def test_log_tails_are_capped_at_the_end(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.engine.output["app"] = "x" * 50_000 + "the last line\n"
    tail = h.service.logs(RUN, "anna")
    assert len(tail) == 20_000
    assert tail.endswith("the last line\n")


def test_seed_failure_tail_is_redacted(h: Harness) -> None:
    h.engine.exit_codes["seed"] = 1
    h.engine.output["seed"] = f"creating tester / {PASSWORD}\nTraceback: boom\n"
    with pytest.raises(RunnerError) as caught:
        prepare(h, seed="python3 seed.py", env={"FOOTFALL_PASSWORD": PASSWORD})
    assert caught.value.log_tail == "creating tester / [redacted]\nTraceback: boom\n"


def test_ops_need_a_prepared_run(h: Harness) -> None:
    with pytest.raises(RunnerError, match="not been prepared"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    with pytest.raises(RunnerError, match="not been prepared"):
        h.service.start_browsers(RUN, [])
    with pytest.raises(RunnerError, match="not been prepared"):
        h.service.logs(RUN, "anna")
    assert h.engine.calls == []


# ---------------------------------------------------------------- stop


def plant_stray(engine: FakeEngine, run_id: str, copy: str) -> str:
    """A labelled container the runner has no record of, as after a crash."""
    name = app_name(copy, run_id)
    config = container_config(
        role="app",
        run_id=run_id,
        copy=copy,
        image="footfall-python3.13:dev",
        command=["sh", "-c", "sleep 1000"],
        workspace=None,
        network=f"footfall-{run_id}",
        proxy_url="http://10.89.0.2:3128",
        env={},
    )
    engine.create(name, config)
    return name


def test_stop_removes_everything_and_is_idempotent(h: Harness) -> None:
    prepare(h, egress=["api.example.com"])
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    h.service.start_app(RUN, "malik", "python3 serve.py", 8000, [])
    h.service.start_browsers(RUN, ["fonts.gstatic.com"])
    stray = plant_stray(h.engine, RUN, "stray")
    h.proxy.allow(h.engine.ips[stray], RUN, ["api.example.com"])
    assert len(h.proxy.allowed) == 4

    h.service.stop(RUN)
    assert_nothing_left(h)
    assert {(NETWORK, PROXY), (NETWORK, ORCHESTRATOR)} <= {
        (call[1], call[2]) for call in h.engine.calls if call[0] == "disconnect"
    }

    h.service.stop(RUN)
    assert_nothing_left(h)
    h.service.stop(OTHER)  # never prepared
    with pytest.raises(RunnerError, match="was stopped"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert h.service.status(RUN) is None


def test_stop_after_a_restart_sweeps_by_label(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    restarted = RunnerService(
        h.engine,
        h.proxy,
        workspaces=h.workspaces,
        runs=h.runs,
        images=IMAGES,
        browsers_image=BROWSERS_IMAGE,
        proxy_container=PROXY,
        orchestrator_container=ORCHESTRATOR,
        chown=h.chowned.append,
    )
    restarted.stop(RUN)
    assert_nothing_left(h)


def test_stop_reports_what_it_could_not_remove(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.engine.fail("remove_network", EngineError("network is busy"))
    with pytest.raises(RunnerError, match="network is busy"):
        h.service.stop(RUN)
    assert h.engine.live == {}
    h.engine.failures.clear()
    h.service.stop(RUN)
    assert_nothing_left(h)


# ---------------------------------------------------------------- refusals


def test_workspace_outside_root_refused(h: Harness, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "serve.py").write_text("print('outside')\n")
    link = h.workspaces / "link"
    link.symlink_to(outside)
    loop = h.workspaces / "loop"
    loop.symlink_to(loop)
    (h.workspaces / "file").write_text("not a folder\n")
    candidates = [
        outside,
        link,
        h.workspaces,
        h.workspaces / ".." / "elsewhere",
        Path("workspaces/app"),
        h.workspaces / "missing",
        loop,
        h.workspaces / "file",
    ]
    for workspace in candidates:
        with pytest.raises(RunnerError, match="workspace"):
            prepare(h, workspace=workspace)
    assert h.engine.calls == []
    assert not h.runs.exists()


@pytest.mark.parametrize("runtime", ["ruby3.3", "node22", "", "python3.13 "])
def test_unknown_runtime_refused(tmp_path: Path, runtime: str) -> None:
    h = make_harness(tmp_path)
    h = Harness(
        h.engine,
        h.proxy,
        RunnerService(
            h.engine,
            h.proxy,
            workspaces=h.workspaces,
            runs=h.runs,
            images={"python3.13": "footfall-python3.13:dev"},
            browsers_image=BROWSERS_IMAGE,
            proxy_container=PROXY,
            orchestrator_container=ORCHESTRATOR,
            chown=h.chowned.append,
        ),
        h.workspaces,
        h.runs,
    )
    with pytest.raises(RunnerError, match="runtime"):
        prepare(h, runtime=runtime)
    assert h.engine.calls == []


BAD_RUN_IDS = [
    "",
    "r_short",
    "R_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "r_abcdefghijklmnopqrstuvwxy1",
    "../../etc",
    f"{RUN}/..",
    f"{RUN}\n",
    "r_" + "a" * 27,
]


@pytest.mark.parametrize("run_id", BAD_RUN_IDS)
def test_bad_run_id_refused(h: Harness, run_id: str) -> None:
    h.workspace()
    with pytest.raises(RunnerError, match="run id"):
        prepare(h, run_id=run_id)
    with pytest.raises(RunnerError, match="run id"):
        h.service.start_app(run_id, "anna", "python3 serve.py", 8000, [])
    with pytest.raises(RunnerError, match="run id"):
        h.service.start_browsers(run_id, [])
    with pytest.raises(RunnerError, match="run id"):
        h.service.logs(run_id, "anna")
    with pytest.raises(RunnerError, match="run id"):
        h.service.stop(run_id)
    with pytest.raises(RunnerError, match="run id"):
        h.service.status(run_id)
    assert h.engine.calls == []


@pytest.mark.parametrize(
    "copy", ["", "Anna", "../anna", "a/b", "-anna", "1anna", "a" * 33, "anna\n"]
)
def test_bad_copy_refused(h: Harness, copy: str) -> None:
    prepare(h)
    with pytest.raises(RunnerError, match="copy"):
        h.service.start_app(RUN, copy, "python3 serve.py", 8000, [])
    with pytest.raises(RunnerError, match="copy"):
        h.service.logs(RUN, copy)
    assert h.engine.names("create") == []
    assert not (h.runs / RUN / "copies").exists()


BAD_HOSTS: list[Any] = [
    ["10.0.0.1"],
    ["169.254.169.254"],
    ["*.example.com"],
    ["localhost"],
    ["example.com:443"],
    ["http://example.com"],
    ["example.com", "exa mple.com"],
    "example.com",
]


@pytest.mark.parametrize("hosts", BAD_HOSTS)
def test_invalid_egress_refused(h: Harness, hosts: Any) -> None:
    with pytest.raises(RunnerError):
        prepare(h, install="pip install -r requirements.txt", egress=hosts)
    assert h.engine.calls == []
    prepare(h)
    with pytest.raises(RunnerError):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, hosts)
    with pytest.raises(RunnerError):
        h.service.start_browsers(RUN, hosts)
    assert h.engine.names("create") == []


@pytest.mark.parametrize(
    "env",
    [{"PORT": "8000"}, {"ANTHROPIC_API_KEY": "sk-ant-x"}, {"FOOTFALL_PASSWORD": "a\nb"}],
)
def test_bad_seed_env_refused(h: Harness, env: dict[str, str]) -> None:
    with pytest.raises(RunnerError, match="env"):
        prepare(h, seed="python3 seed.py", env=env)
    assert h.engine.calls == []


@pytest.mark.parametrize("command", ["", "  ", "pip\x00install"])
def test_bad_install_or_seed_refused(h: Harness, command: str) -> None:
    with pytest.raises(RunnerError, match="install"):
        prepare(h, install=command)
    with pytest.raises(RunnerError, match="seed"):
        prepare(h, seed=command)
    assert h.engine.calls == []


def test_runc_override_is_passed_to_spec(tmp_path: Path) -> None:
    h = make_harness(tmp_path / "a", runtime="runc")
    prepare(h, install="pip install -r requirements.txt", seed="python3 seed.py")
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.service.start_browsers(RUN, [])
    assert len(h.engine.configs) == 4
    for config in h.engine.configs.values():
        assert config["HostConfig"]["Runtime"] == "runc"

    default = make_harness(tmp_path / "b")
    prepare(default, install="pip install -r requirements.txt")
    assert default.engine.configs[INSTALL]["HostConfig"]["Runtime"] == "runsc"


@pytest.mark.parametrize(
    "settings",
    [
        {"runtime": "kata"},
        {"images": {"ruby3.3": "ruby:latest"}},
        {"proxy_port": 0},
        {"step_timeout": 0},
        {"disk_budget": 0},
        {"disk_interval": -1},
        {"runs_inside_workspaces": True},
    ],
)
def test_bad_settings_refused(tmp_path: Path, settings: dict[str, Any]) -> None:
    (tmp_path / "workspaces").mkdir()
    kwargs: dict[str, Any] = {
        "workspaces": tmp_path / "workspaces",
        "runs": tmp_path / "runs",
        "images": IMAGES,
        "browsers_image": BROWSERS_IMAGE,
        "proxy_container": PROXY,
        "orchestrator_container": ORCHESTRATOR,
    }
    if settings.pop("runs_inside_workspaces", False):
        kwargs["runs"] = tmp_path / "workspaces" / "runs"
    kwargs.update(settings)
    with pytest.raises(ValueError):
        RunnerService(FakeEngine(), FakeProxy(), **kwargs)


# ---------------------------------------------------------------- links and special files


def test_symlink_to_outside_file_is_copied_as_a_link(h: Harness, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("a host secret\n")
    workspace = h.workspace()
    (workspace / "leak").symlink_to(secret)
    (workspace / "relative").symlink_to("../../secret.txt")
    (workspace / "folder").symlink_to(tmp_path)
    (workspace / "dangling").symlink_to("/nowhere/at/all")
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])

    for top in (h.runs / RUN / "prepared", h.runs / RUN / "copies" / "anna"):
        assert (top / "leak").is_symlink()
        assert (top / "leak").readlink() == secret
        assert (top / "relative").readlink() == Path("../../secret.txt")
        assert (top / "folder").is_symlink()
        assert (top / "dangling").is_symlink()
        for path in top.rglob("*"):
            if not path.is_symlink() and path.is_file():
                assert "a host secret" not in path.read_text()
        assert top / "leak" in h.chowned
    assert secret.read_text() == "a host secret\n"


def test_fifo_in_workspace_refused(h: Harness) -> None:
    workspace = h.workspace()
    (workspace / "deep").mkdir()
    os.mkfifo(workspace / "deep" / "pipe")
    with pytest.raises(RunnerError, match=r"FIFO at 'deep/pipe'"):
        prepare(h)
    assert h.engine.names("create_network") == []
    assert_nothing_left(h)


def test_fifo_left_by_seed_refused(h: Harness) -> None:
    h.engine.during_wait["seed"] = lambda w: os.mkfifo(w / "pipe")
    with pytest.raises(RunnerError, match="FIFO at 'pipe'"):
        prepare(h, seed="python3 seed.py")
    assert_nothing_left(h)


def make_chain(top: Path, depth: int) -> Path:
    """Make `depth` nested folders below `top` and return the deepest."""
    folder = top.joinpath(*["d"] * depth)
    folder.mkdir(parents=True)
    return folder


def test_deep_workspace_refused(h: Harness) -> None:
    workspace = h.workspace()
    make_chain(workspace / "ok", MAX_DEPTH - 1)
    (make_chain(workspace / "deep", MAX_DEPTH) / "x.txt").write_text("too deep\n")
    with pytest.raises(RunnerError, match=f"more than {MAX_DEPTH} folders deep at 'deep/"):
        prepare(h)
    assert h.service.status(RUN) == DISK_REASON  # measured before the copy, as over budget
    assert h.engine.names("create_network") == []
    assert_nothing_left(h)


def test_workspace_just_within_the_depth_limit_is_copied(h: Harness) -> None:
    workspace = h.workspace()
    (make_chain(workspace, MAX_DEPTH - 1) / "leaf.txt").write_text("deep enough\n")
    prepare(h)
    copied = (h.runs / RUN / "prepared").joinpath(*["d"] * (MAX_DEPTH - 1), "leaf.txt")
    assert copied.read_text() == "deep enough\n"


def test_deep_tree_left_by_seed_refused(h: Harness) -> None:
    h.engine.during_wait["seed"] = lambda w: make_chain(w, MAX_DEPTH + 1)
    with pytest.raises(RunnerError, match=f"the prepared app is more than {MAX_DEPTH} folders"):
        prepare(h, seed="python3 seed.py")
    assert_nothing_left(h)


def open_folder(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def test_copy_file_refuses_anything_but_a_regular_file(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "target").write_text("followed\n")
    (tmp_path / "link").symlink_to(tmp_path / "target")
    folder = open_folder(tmp_path)
    try:
        with pytest.raises(RunnerError, match="FIFO"):
            _copy_file(folder, "pipe", tmp_path / "out-pipe", 10**9)  # never blocks
        with pytest.raises(OSError):
            _copy_file(folder, "link", tmp_path / "out-link", 10**9)
        assert not (tmp_path / "out-pipe").exists()
        assert not (tmp_path / "out-link").exists()

        source = tmp_path / "tool"
        source.write_bytes(b"\x00\x01" * 700_000)
        source.chmod(0o4755)
        assert _copy_file(folder, "tool", tmp_path / "copied", 10**9) == 1_400_000
        copied = tmp_path / "copied"
        assert copied.read_bytes() == source.read_bytes()
        assert copied.stat().st_mode & 0o7777 == 0o755  # no set-user-id bit
        assert copied.stat().st_mtime_ns == source.stat().st_mtime_ns
        with pytest.raises(OSError):
            _copy_file(folder, "tool", tmp_path / "copied", 10**9)  # never overwrites

        with pytest.raises(_OverBudget):
            _copy_file(folder, "tool", tmp_path / "too-big", 1_399_999)
    finally:
        os.close(folder)


def test_copy_counts_bytes_against_its_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"\x01" * 3000)
    (source / "b.bin").write_bytes(b"\x01" * 3000)
    # Each entry is charged at least a 4 KiB block: the folder and two small files.
    written = _copy_tree(
        tmp_path, PurePath("source"), tmp_path / "fits", limit=3 * PAGE, chown=noop, what="it"
    )
    assert written == 3 * PAGE
    with pytest.raises(_OverBudget):
        _copy_tree(
            tmp_path,
            PurePath("source"),
            tmp_path / "over",
            limit=3 * PAGE - 1,
            chown=noop,
            what="it",
        )


def noop(path: Path) -> None:
    pass


def test_copy_refuses_special_files_in_the_same_pass(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "serve.py").write_text("print('hi')\n")
    os.mkfifo(source / "pipe")
    with pytest.raises(RunnerError, match="it contains a FIFO at 'pipe'"):
        _copy_tree(tmp_path, PurePath("source"), tmp_path / "t", limit=10**9, chown=noop, what="it")


def test_workspace_swapped_for_a_link_after_the_check_is_not_followed(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("a host secret\n")
    workspace = h.workspace()
    checked = h.service._workspace

    def check_then_swap(path: Path) -> Path:
        resolved = checked(path)
        workspace.rename(h.workspaces / "moved")
        workspace.symlink_to(outside)
        return resolved

    monkeypatch.setattr(h.service, "_workspace", check_then_swap)
    with pytest.raises(RunnerError):
        prepare(h, workspace=workspace)
    assert not any(path.name == "secret.txt" for path in h.chowned)
    assert_nothing_left(h)


def test_folder_swapped_for_a_link_during_the_copy_is_not_followed(tmp_path: Path) -> None:
    h: Harness  # made below, once the hook that uses it exists
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("a host secret\n")
    seen: list[Path] = []

    def chown_then_swap(path: Path) -> None:
        seen.append(path)
        if path.parts[-2:] == ("a", "b"):  # its target is made; now swap the source
            source = h.workspaces / "app" / "a" / "b"
            source.rename(source.with_name("b-moved"))
            source.symlink_to(outside)

    h = make_harness(tmp_path / "h", chown=chown_then_swap)
    workspace = h.workspace()
    (workspace / "a" / "b").mkdir(parents=True)
    (workspace / "a" / "b" / "data.txt").write_text("app data\n")
    with pytest.raises(RunnerError, match="changed while it was read"):
        prepare(h)
    assert not any(path.name == "secret.txt" for path in seen)
    assert_nothing_left(h)


def test_no_container_mounts_a_tree_while_it_is_copied(tmp_path: Path) -> None:
    h: Harness  # made below, once the hook that uses it exists
    # The invariant the copy relies on: install and seed are removed before prepared is copied,
    # app containers mount only their own copies, and no container ever mounts the workspace.
    checked: list[Path] = []

    def chown_and_check(path: Path) -> None:
        # The runner calls chown only while it copies, once for each entry it makes.
        checked.append(path)
        for name in h.engine.live:
            for bind in h.engine.configs[name]["HostConfig"].get("Binds", []):
                source = Path(bind.rsplit(":", 2)[0])
                assert source not in (h.workspaces / "app", h.runs / RUN / "prepared"), (
                    f"{name} mounts {source} while {path} is copied"
                )

    h = make_harness(tmp_path, chown=chown_and_check)
    h.engine.during_wait["seed"] = lambda w: (w / "data.json").write_text("{}")
    prepare(h, install="pip install -r requirements.txt", seed="python3 seed.py")
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.service.start_app(RUN, "malik", "python3 serve.py", 8000, [])
    assert any("/copies/malik" in str(path) for path in checked)


# ---------------------------------------------------------------- failures and cleanup


@pytest.mark.parametrize(
    ("method", "error", "role"),
    [
        ("create_network", ConnectionRefusedError(61, "Connection refused"), None),
        ("connect", FileNotFoundError(2, "No such file or directory"), None),
        ("address", TimeoutError("timed out"), None),
        ("create", EngineError("POST /containers/create failed (500): no space"), "install"),
        ("start", FileNotFoundError(2, "No such file or directory"), "install"),
        ("wait", ConnectionResetError(54, "Connection reset by peer"), "seed"),
        ("remove", EngineError("DELETE /containers failed (500): busy"), "install"),
    ],
)
def test_engine_failure_becomes_runner_error_and_cleans_up(
    h: Harness, method: str, error: BaseException, role: str | None
) -> None:
    h.engine.fail(method, error, role)
    with pytest.raises(RunnerError):
        prepare(h, install="pip install -r requirements.txt", seed="python3 seed.py")
    h.engine.failures.clear()
    if method == "remove":
        h.service.stop(RUN)  # the container that would not go is swept up here
    assert_nothing_left(h)
    # A failed prepare leaves nothing on record, so its id can be prepared again; a stopped
    # run's id cannot.
    prepare(h, run_id=OTHER if method == "remove" else RUN)


def test_proxy_failure_becomes_runner_error_and_cleans_up(h: Harness) -> None:
    h.proxy.failures["allow"] = ConnectionRefusedError(61, "Connection refused")
    with pytest.raises(RunnerError, match="Connection refused"):
        prepare(h, install="pip install -r requirements.txt")
    del h.proxy.failures["allow"]
    assert_nothing_left(h)


def test_prepare_partial_failure_leaves_nothing_behind(h: Harness) -> None:
    h.engine.fail("start", EngineError("OCI runtime create failed"), role="seed")
    h.engine.during_wait["install"] = lambda w: (w / "site-packages").mkdir()
    with pytest.raises(RunnerError, match="OCI runtime create failed"):
        prepare(h, install="pip install -r requirements.txt", seed="python3 seed.py")
    assert ("remove", INSTALL) in h.engine.calls
    assert ("remove", SEED) in h.engine.calls
    assert ("remove_network", NETWORK) in h.engine.calls
    assert_nothing_left(h)
    assert h.service.status(RUN) is None


def test_cleanup_problems_do_not_hide_the_first_error(h: Harness) -> None:
    h.engine.exit_codes["install"] = 3
    h.engine.fail("remove_network", EngineError("network is busy"))
    with pytest.raises(RunnerError, match="exit code 3"):
        prepare(h, install="pip install -r requirements.txt")


# ---------------------------------------------------------------- disk budget and status


def test_disk_budget_exceeded_after_install_stops_the_run(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024)
    h.engine.during_wait["install"] = lambda w: (w / "big.bin").write_bytes(b"\x01" * MIB)
    h.engine.output["install"] = "Successfully installed big-1.0\n"
    with pytest.raises(RunnerError) as caught:
        prepare(h, install="pip install big", seed="python3 seed.py")
    assert str(caught.value) == DISK_REASON
    assert caught.value.log_tail == "Successfully installed big-1.0\n"
    assert SEED not in h.engine.names("create")
    assert_nothing_left(h)
    assert h.service.status(RUN) == DISK_REASON
    with pytest.raises(RunnerError, match=DISK_REASON):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h)
    h.service.stop(RUN)  # stopping again is still fine
    assert h.service.status(RUN) == DISK_REASON


def test_disk_budget_exceeded_after_seed_stops_the_run(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024)
    h.engine.during_wait["seed"] = lambda w: (w / "data.db").write_bytes(b"\x01" * MIB)
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h, seed="python3 seed.py")
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


def test_workspace_over_budget_is_refused(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024)
    workspace = h.workspace()
    (workspace / "big.bin").write_bytes(b"\x01" * MIB)
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h, workspace=workspace)
    assert_nothing_left(h)


def test_disk_watch_stops_a_run_over_budget(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=512 * 1024)
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    prepare(h, run_id=OTHER)
    h.service.check_runs()
    assert h.service.status(RUN) is None

    (h.runs / RUN / "copies" / "anna" / "upload.bin").write_bytes(b"\x01" * MIB)
    h.service.check_runs()
    assert h.service.status(RUN) == DISK_REASON
    assert h.service.status(OTHER) is None
    assert app_name("anna") not in h.engine.live
    assert not (h.runs / RUN).exists()
    assert (h.runs / OTHER / "prepared").exists()
    assert all(run_id != RUN for run_id, _ in h.proxy.allowed.values())
    with pytest.raises(RunnerError, match=DISK_REASON):
        h.service.logs(RUN, "anna")
    with pytest.raises(RunnerError, match=DISK_REASON):
        h.service.start_browsers(RUN, [])


def test_hard_links_count_once_per_name(tmp_path: Path) -> None:
    # A copy writes every name in full, so the measure counts every name.
    h = make_harness(tmp_path, disk_budget=MIB)

    def seed(workspace: Path) -> None:
        (workspace / "one.bin").write_bytes(b"\x01" * 400 * 1024)
        for index in range(3):
            (workspace / f"link{index}.bin").hardlink_to(workspace / "one.bin")

    h.engine.during_wait["seed"] = seed
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h, seed="python3 seed.py")
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


def test_sparse_file_counts_at_full_size(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=MIB)

    def seed(workspace: Path) -> None:
        with (workspace / "sparse.bin").open("wb") as sparse:
            sparse.truncate(1024 * MIB)  # takes almost no disk, but a copy writes all of it

    h.engine.during_wait["seed"] = seed
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h, seed="python3 seed.py")
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


def test_disk_usage_measures_what_a_copy_writes(tmp_path: Path) -> None:
    top = tmp_path / "top"
    top.mkdir()
    (top / "a.bin").write_bytes(b"\x01" * 100_000)
    (top / "b.bin").hardlink_to(top / "a.bin")
    with (top / "sparse.bin").open("wb") as sparse:
        sparse.truncate(5_000_000)
    (top / "link").symlink_to(tmp_path)  # counted as a link, never followed
    assert _disk_usage(top) >= 5_200_000
    assert _disk_usage(tmp_path / "missing") == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_disk_usage_raises_on_a_folder_it_cannot_read(tmp_path: Path) -> None:
    locked = tmp_path / "top" / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            _disk_usage(tmp_path / "top")
    finally:
        locked.chmod(0o700)


def test_start_app_checks_the_budget_before_copying(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=int(1.5 * MIB))
    h.engine.during_wait["seed"] = lambda w: (w / "data.bin").write_bytes(b"\x01" * MIB)
    prepare(h, seed="python3 seed.py")
    with pytest.raises(RunnerError, match=DISK_REASON):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    assert not any("copies" in path.parts for path in h.chowned)  # the copy never started
    assert app_name("anna") not in h.engine.configs
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


def test_prepare_checks_the_workspace_size_before_copying(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024)
    (h.workspace() / "big.bin").write_bytes(b"\x01" * MIB)
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h)
    assert not any(path.name == "big.bin" for path in h.chowned)
    assert_nothing_left(h)


def test_a_run_that_cannot_be_measured_is_stopped(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])

    def too_many_files(top: Path, max_depth: int) -> tuple[int, int]:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("footfall.runner.service._measure", too_many_files)
    h.service.check_runs()
    assert h.service.status(RUN) == MEASURE_REASON
    assert_nothing_left(h)


def test_a_run_whose_folder_has_gone_is_left_alone(h: Harness) -> None:
    prepare(h)
    shutil.rmtree(h.runs / RUN)
    h.service.check_runs()
    assert h.service.status(RUN) is None
    h.service.start_browsers(RUN, [])


def test_recursion_error_becomes_runner_error(h: Harness) -> None:
    h.engine.fail("create_network", RecursionError("maximum recursion depth exceeded"))
    with pytest.raises(RunnerError, match="recursion"):
        prepare(h)
    assert_nothing_left(h)


def test_failed_container_removal_keeps_the_network_and_folder(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    h.engine.fail("remove", EngineError("container is stuck"), role="app")
    with pytest.raises(RunnerError, match="container is stuck"):
        h.service.stop(RUN)
    assert NETWORK in h.engine.networks
    assert (h.runs / RUN / "copies" / "anna").exists()
    assert ("remove_network", NETWORK) not in h.engine.calls
    status = h.service.status(RUN)
    assert status is not None and "container is stuck" in status
    with pytest.raises(RunnerError, match="was stopped"):
        h.service.logs(RUN, "anna")
    with pytest.raises(RunnerError, match="was stopped"):
        prepare(h)

    h.engine.failures.clear()
    h.clock.advance(15)
    h.service.check_runs()  # the watch finishes the stop
    assert_nothing_left(h)
    assert h.service.status(RUN) is None


def test_failed_revoke_keeps_the_network(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    ip = h.engine.ips[app_name("anna")]
    h.proxy.failures["revoke"] = ConnectionRefusedError(61, "Connection refused")
    with pytest.raises(RunnerError, match="Connection refused"):
        h.service.stop(RUN)
    # The address keeps its allow list, so its network stays and Docker cannot hand the
    # address to another container.
    assert ip in h.proxy.allowed
    assert NETWORK in h.engine.networks
    assert (h.runs / RUN).exists()

    del h.proxy.failures["revoke"]
    h.service.stop(RUN)
    assert ("revoke", ip) in h.proxy.calls
    assert_nothing_left(h)


def test_failed_stop_of_an_unknown_run_is_retried_by_the_watch(h: Harness) -> None:
    h.engine.networks[NETWORK] = {"footfall.run": RUN}
    stray = plant_stray(h.engine, RUN, "stray")
    h.engine.fail("remove", EngineError("container is stuck"))
    with pytest.raises(RunnerError, match="container is stuck"):
        h.service.stop(RUN)
    assert stray in h.engine.live
    assert NETWORK in h.engine.networks
    h.engine.failures.clear()
    h.clock.advance(15)
    h.service.check_runs()
    assert h.engine.live == {}
    assert h.engine.networks == {}


def test_stop_racing_prepare_waits_for_it(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without install or seed, prepare never lets go of the run's lock, so a stop that finds
    # the run registered cannot finish until prepare has.
    h.workspace()
    register = h.service._register
    started = threading.Event()
    racers: list[threading.Thread] = []

    def stop() -> None:
        started.set()
        h.service.stop(RUN)

    def register_then_race(run: Any) -> None:
        register(run)
        racer = threading.Thread(target=stop, daemon=True)
        racers.append(racer)
        racer.start()
        assert started.wait(5)
        racer.join(timeout=0.2)
        assert racer.is_alive(), "the stop did not wait for prepare"

    monkeypatch.setattr(h.service, "_register", register_then_race)
    prepare(h)
    racers[0].join(timeout=5)
    assert not racers[0].is_alive()
    assert_nothing_left(h)  # the stop ran after prepare and removed what it made
    with pytest.raises(RunnerError, match="was stopped"):
        prepare(h)


# ---------------------------------------------------------------- fix round 2


def test_entries_are_yielded_as_they_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A folder of a million empty files must not become a million stat results in memory.
    for index in range(50):
        (tmp_path / f"e{index}").touch()
    stats: list[object] = []
    real_stat = os.stat

    def counting(*args: Any, **kwargs: Any) -> os.stat_result:
        stats.append(args)
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(os, "stat", counting)
    folder = open_folder(tmp_path)
    try:
        entries = _entries(folder, "it")
        next(entries)
        assert len(stats) <= 3  # the walk's own look at the top, and the first entry
        entries.close()
    finally:
        os.close(folder)


def test_every_name_is_charged_at_least_a_block(tmp_path: Path) -> None:
    top = tmp_path / "top"
    top.mkdir()
    for index in range(10):
        (top / f"e{index}").touch()
    (top / "folder").mkdir()
    (top / "link").symlink_to("e0")
    (top / "small.txt").write_bytes(b"x" * 5000)  # rounds up to two blocks
    assert _disk_usage(top) >= (1 + 10 + 1 + 1 + 2) * PAGE


def test_a_seed_that_plants_many_empty_files_is_stopped(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=MIB)  # 256 blocks of 4 KiB

    def seed(workspace: Path) -> None:
        for index in range(300):
            (workspace / f"e{index}").touch()

    h.engine.during_wait["seed"] = seed
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h, seed="python3 seed.py")
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


def test_copy_charges_each_entry_a_block(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for index in range(100):
        (source / f"e{index}").touch()
    with pytest.raises(_OverBudget):
        _copy_tree(
            tmp_path, PurePath("source"), tmp_path / "over", limit=50 * PAGE, chown=noop, what="it"
        )
    written = _copy_tree(
        tmp_path, PurePath("source"), tmp_path / "fits", limit=101 * PAGE, chown=noop, what="it"
    )
    assert written == 101 * PAGE

    folders = tmp_path / "folders"
    for index in range(100):
        (folders / f"f{index}").mkdir(parents=True)
    with pytest.raises(_OverBudget):
        _copy_tree(
            tmp_path,
            PurePath("folders"),
            tmp_path / "f-over",
            limit=50 * PAGE,
            chown=noop,
            what="it",
        )


def test_stop_before_prepare_refuses_the_prepare(h: Harness) -> None:
    h.workspace()
    h.service.stop(RUN)
    assert h.service.status(RUN) == NEVER_PREPARED
    with pytest.raises(RunnerError, match=NEVER_PREPARED):
        prepare(h)
    assert h.engine.names("create_network") == []
    assert not (h.runs / RUN).exists()


def test_a_stopped_run_id_is_never_prepared_again(h: Harness) -> None:
    prepare(h)
    h.service.stop(RUN)
    h.service.stop(RUN)  # stopping again does not turn it into one never prepared
    assert h.service.status(RUN) is None
    with pytest.raises(RunnerError, match="was stopped"):
        prepare(h)
    assert h.engine.names("create_network") == [NETWORK]


def test_a_stop_during_a_step_keeps_the_id_from_a_second_prepare(h: Harness) -> None:
    # The first prepare's cleanup sweeps by the run's label, so a second prepare of the same id
    # must never get to make anything.
    second: list[BaseException] = []

    def again() -> None:
        try:
            prepare(h, install="pip install -r requirements.txt")
        except RunnerError as error:
            second.append(error)

    def install(_: Path) -> None:
        if not second:
            in_another_thread(lambda: h.service.stop(RUN))
            in_another_thread(again)

    h.engine.during_wait["install"] = install
    with pytest.raises(RunnerError, match="was stopped"):
        prepare(h, install="pip install -r requirements.txt")
    assert len(second) == 1
    assert "was stopped" in str(second[0])
    assert h.engine.names("create_network") == [NETWORK]
    assert_nothing_left(h)


def test_teardown_retries_back_off(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    h.engine.fail("remove", EngineError("container is stuck"), role="app")
    with pytest.raises(RunnerError, match="container is stuck"):
        h.service.stop(RUN)

    def attempts() -> int:
        return h.engine.calls.count(("remove", app_name("anna")))

    seen = attempts()
    for delay in (15, 30, 60, 120, 240, 480, 600, 600):
        h.clock.advance(delay - 1)
        h.service.check_runs()
        assert attempts() == seen, f"retried before {delay} s"
        h.clock.advance(1)
        h.service.check_runs()
        assert attempts() == seen + 1, f"not retried after {delay} s"
        seen += 1
    h.engine.failures.clear()
    h.clock.advance(600)
    h.service.check_runs()
    assert_nothing_left(h)


def test_measure_stops_at_the_depth_limit(tmp_path: Path) -> None:
    top = tmp_path / "top"
    make_chain(top, 5)
    with pytest.raises(_TooDeep):
        _disk_usage(top, 4)
    assert _disk_usage(top, 5) >= 6 * PAGE


def test_a_run_deeper_than_the_limit_is_stopped_as_over_budget(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    make_chain(h.runs / RUN / "copies" / "anna", MAX_DEPTH + 1)
    h.service.check_runs()
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


@pytest.mark.parametrize("name", [b"pi\npe", b"pipe\xff"])
def test_hostile_names_are_escaped_in_errors(h: Harness, name: bytes) -> None:
    workspace = h.workspace()
    try:
        os.mkfifo(os.fsencode(workspace) + b"/" + name)
    except OSError:
        pytest.skip("this filesystem refuses the name")
    with pytest.raises(RunnerError) as caught:
        prepare(h)
    message = str(caught.value)
    assert "FIFO" in message
    assert "\n" not in message
    message.encode("utf-8")  # no lone surrogate from an undecodable byte


def test_hostile_folder_names_are_escaped_and_capped(h: Harness) -> None:
    workspace = h.workspace()
    make_chain(workspace / ("x\n" + "y" * 200), MAX_DEPTH)
    with pytest.raises(RunnerError) as caught:
        prepare(h)
    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 200


def test_a_stray_whose_address_cannot_be_read_keeps_the_network(h: Harness) -> None:
    h.engine.networks[NETWORK] = {"footfall.run": RUN}
    plant_stray(h.engine, RUN, "stray")
    h.engine.fail("address", EngineError("inspect failed"))
    with pytest.raises(RunnerError, match="inspect failed"):
        h.service.stop(RUN)
    assert NETWORK in h.engine.networks
    h.engine.failures.clear()
    h.clock.advance(15)
    h.service.check_runs()
    assert h.engine.live == {}
    assert h.engine.networks == {}


def test_teardown_revokes_the_whole_run_before_removing_its_network(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    seen: list[tuple[bool, list[str]]] = []
    revoke_run = h.proxy.revoke_run

    def watched(run_id: str) -> None:
        seen.append((NETWORK in h.engine.networks, list(h.engine.live)))
        revoke_run(run_id)

    h.proxy.revoke_run = watched  # type: ignore[method-assign]
    h.service.stop(RUN)
    # Called once, with every container gone and the network still there.
    assert seen == [(True, [])]
    assert ("revoke_run", RUN) in h.proxy.calls
    assert_nothing_left(h)


def test_failed_revoke_run_keeps_the_network_until_it_succeeds(h: Harness) -> None:
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    h.proxy.failures["revoke_run"] = ConnectionRefusedError(61, "Connection refused")
    with pytest.raises(RunnerError, match="allow lists of run"):
        h.service.stop(RUN)
    assert h.engine.live == {}
    assert NETWORK in h.engine.networks
    assert (h.runs / RUN).exists()
    status = h.service.status(RUN)
    assert status is not None and "Connection refused" in status

    h.clock.advance(15)
    h.service.check_runs()  # still failing: the network stays
    assert NETWORK in h.engine.networks
    del h.proxy.failures["revoke_run"]
    h.clock.advance(30)
    h.service.check_runs()
    assert_nothing_left(h)
    assert h.service.status(RUN) is None


def test_a_stray_with_an_unreadable_address_loses_its_list_by_run(h: Harness) -> None:
    # A container the runner has no record of, whose allow list survived a runner restart.
    h.engine.networks[NETWORK] = {"footfall.run": RUN}
    stray = plant_stray(h.engine, RUN, "stray")
    h.proxy.allow(h.engine.ips[stray], RUN, ["api.example.com"])
    h.engine.fail("address", EngineError("inspect failed"))
    with pytest.raises(RunnerError, match="inspect failed"):
        h.service.stop(RUN)
    assert stray not in h.engine.live  # removed, but its address was never read
    assert NETWORK in h.engine.networks
    h.engine.failures.clear()

    h.proxy.failures["revoke_run"] = ConnectionRefusedError(61, "Connection refused")
    h.clock.advance(15)
    h.service.check_runs()
    assert NETWORK in h.engine.networks  # the list may live on, so the address must not move
    assert ("remove_network", NETWORK) not in h.engine.calls

    del h.proxy.failures["revoke_run"]
    h.clock.advance(30)
    h.service.check_runs()
    assert h.proxy.allowed == {}
    assert h.engine.networks == {}
    assert h.service.status(RUN) == NEVER_PREPARED  # and nothing left to finish


def test_an_allow_refused_for_another_runs_address_fails_the_start_closed(h: Harness) -> None:
    prepare(h)
    taken = f"10.89.0.{h.engine._count + 1}"  # the address the next container will get
    h.proxy.allowed[taken] = (OTHER, ("api.example.com",))
    with pytest.raises(RunnerError, match="another run"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, ["api.example.com"])
    assert h.engine.ips[app_name("anna")] == taken
    assert app_name("anna") not in h.engine.live
    assert not (h.runs / RUN / "copies" / "anna").exists()
    assert all(owner != RUN for owner, _ in h.proxy.allowed.values())
    assert ("revoke", taken) in h.proxy.calls  # failing closed takes the stale list too
    assert h.proxy.allowed == {}
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])  # the copy name is free


def test_relaunch_revokes_an_address_kept_after_a_failed_revoke(h: Harness) -> None:
    prepare(h)
    name = f"footfall-{RUN}-browsers-browsers"
    h.proxy.failures["allow"] = ConnectionRefusedError(61, "Connection refused")
    h.proxy.failures["revoke"] = ConnectionRefusedError(61, "Connection refused")
    with pytest.raises(RunnerError):
        h.service.start_browsers(RUN, [])
    old = h.engine.ips[name]
    del h.proxy.failures["allow"]
    with pytest.raises(RunnerError, match="Connection refused"):
        h.service.start_browsers(RUN, [])  # cannot revoke the old address, so does not start
    assert h.engine.names("create").count(name) == 1
    del h.proxy.failures["revoke"]
    h.service.start_browsers(RUN, [])
    assert ("revoke", old) in h.proxy.calls
    assert h.engine.names("create").count(name) == 2


def test_default_disk_budget_is_8_gib() -> None:
    parameter = inspect.signature(RunnerService).parameters["disk_budget"]
    assert parameter.default == 8 * 1024**3


def test_watch_checks_until_told_to_stop(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024, disk_interval=0.01)
    prepare(h)
    stop = threading.Event()
    watcher = threading.Thread(target=h.service.watch, args=(stop,), daemon=True)
    watcher.start()
    try:
        (h.runs / RUN / "prepared" / "big.bin").write_bytes(b"\x01" * MIB)
        deadline = time.monotonic() + 5
        while h.service.status(RUN) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h.service.status(RUN) == DISK_REASON
    finally:
        stop.set()
        watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert_nothing_left(h)


def in_another_thread(action: Any) -> None:
    """Run `action` in a thread and wait for it, failing rather than hanging on a held lock."""
    thread = threading.Thread(target=action, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "blocked: the run's lock was held during the wait"


def test_stop_during_a_step_ends_the_run(h: Harness) -> None:
    h.engine.during_wait["install"] = lambda _: in_another_thread(lambda: h.service.stop(RUN))
    with pytest.raises(RunnerError, match=f"run {RUN} was stopped"):
        prepare(h, install="pip install -r requirements.txt", seed="python3 seed.py")
    assert SEED not in h.engine.names("create")
    assert_nothing_left(h)
    assert h.service.status(RUN) is None


def test_disk_watch_during_a_step_ends_the_run(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024)

    def install(workspace: Path) -> None:
        (workspace / "big.bin").write_bytes(b"\x01" * MIB)
        in_another_thread(h.service.check_runs)

    h.engine.during_wait["install"] = install
    with pytest.raises(RunnerError, match=DISK_REASON):
        prepare(h, install="pip install big", seed="python3 seed.py")
    assert h.service.status(RUN) == DISK_REASON
    assert SEED not in h.engine.names("create")
    assert_nothing_left(h)


def test_watch_survives_a_failed_stop(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=256 * 1024)
    prepare(h)
    (h.runs / RUN / "prepared" / "big.bin").write_bytes(b"\x01" * MIB)
    h.engine.fail("remove_network", EngineError("network is busy"))
    h.service.check_runs()
    status = h.service.status(RUN)
    assert status is not None and status.startswith(f"{DISK_REASON}; ")
    assert "network is busy" in status
    h.engine.failures.clear()
    h.clock.advance(15)
    h.service.check_runs()  # tries the stop again
    assert h.service.status(RUN) == DISK_REASON
    assert_nothing_left(h)


def test_status_is_none_while_live_and_for_unknown_runs(h: Harness) -> None:
    assert h.service.status(RUN) is None
    prepare(h)
    assert h.service.status(RUN) is None
    h.service.stop(RUN)
    assert h.service.status(RUN) is None


# ---------------------------------------------------------------- cleanup pass


def test_a_copy_is_charged_what_the_measure_finds(tmp_path: Path) -> None:
    """A folder with many names is larger than a block, and the copy charges it as the
    measure does, at its size, not at one block. The copy is compared with the measure of
    what it copies: a folder filled in another order, as the copy is, can take a block more
    or less on ext4."""
    source = tmp_path / "source"
    (source / "wide").mkdir(parents=True)
    for index in range(1500):
        (source / "wide" / f"entry-{index:05d}").touch()
    (source / "small.txt").write_bytes(b"x" * 5000)
    (source / "link").symlink_to("small.txt")
    assert _size(os.lstat(source / "wide")) > PAGE  # the case that matters
    written = _copy_tree(
        tmp_path, PurePath("source"), tmp_path / "copy", limit=10**9, chown=noop, what="it"
    )
    assert written == _disk_usage(source)


def test_every_folder_is_opened_without_following_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder swapped for a link between its listing and its opening is never opened
    through the link, whatever it leads to, by the copy's walk or by the measure."""
    top = tmp_path / "top"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "b" / "file").touch()
    opened: list[int] = []
    real_open = os.open

    def recording(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            opened.append(flags)
        return fd

    monkeypatch.setattr(os, "open", recording)
    folder = open_folder(top)
    try:
        assert len(list(_entries(folder, "it"))) == 3
    finally:
        os.close(folder)
    _disk_usage(top)
    assert len(opened) >= 5  # every folder below the top, by both walks
    assert all(flags & os.O_NOFOLLOW for flags in opened[1:])  # the first is the test's own


def test_a_folder_swapped_for_a_link_is_named_and_not_walked(tmp_path: Path) -> None:
    top = tmp_path / "top"
    (top / "a" / "b").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").touch()
    folder = open_folder(top)
    seen: list[str] = []
    try:
        with pytest.raises(RunnerError, match="changed while it was read, at 'a/b'"):
            for here, _, name, _ in _entries(folder, "it"):
                seen.append(str(here / name))
                if name == "b":  # listed as a folder, not yet opened
                    (top / "a" / "b").rename(top / "a" / "moved")
                    (top / "a" / "b").symlink_to(outside)
    finally:
        os.close(folder)
    assert "secret.txt" not in "".join(seen)


def test_a_wide_tree_is_walked_without_a_record_of_every_folder(tmp_path: Path) -> None:
    """The walk holds what one path down the tree needs, not every folder it has seen."""
    top = tmp_path / "top"
    for index in range(3000):
        (top / f"folder-{index:05d}").mkdir(parents=True)
    folder = open_folder(top)
    try:
        tracemalloc.start()
        try:
            count = sum(1 for _ in _entries(folder, "it"))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        os.close(folder)
    assert count == 3000
    assert peak < 100_000, peak


def test_only_the_latest_stopped_run_ids_are_remembered(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("footfall.runner.service.REMEMBERED_STOPS", 3)
    ids = [f"r_{letter * 26}" for letter in "bcdef"]
    for run_id in ids:
        h.service.stop(run_id)
    # The two oldest are forgotten, reason and all; the three latest are still refused.
    assert [h.service.status(run_id) for run_id in ids] == [None, None, *[NEVER_PREPARED] * 3]
    assert len(h.service._stopped) == 3
    assert len(h.service._reasons) == 3
    h.workspace()
    with pytest.raises(RunnerError, match=NEVER_PREPARED):
        prepare(h, run_id=ids[-1])


def test_a_run_still_on_record_keeps_its_reason_however_many_others_stop(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("footfall.runner.service.REMEMBERED_STOPS", 2)
    prepare(h)
    h.engine.fail("remove_network", EngineError("still in use"))
    with pytest.raises(RunnerError, match="still in use"):
        h.service.stop(RUN)
    h.engine.failures.clear()  # every other run stops cleanly
    for letter in "bcd":
        h.service.stop(f"r_{letter * 26}")
    assert RUN in h.service._stopped  # it is still on record, so it is not forgotten
    assert "still in use" in (h.service.status(RUN) or "")


def test_a_failure_to_start_the_browsers_never_carries_their_path(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path goes to the caller on success and nowhere else, not in an error either."""
    prepare(h)
    real_start = h.engine.start

    def start(container: str) -> None:
        config = h.engine.configs.get(container) or {}
        if config.get("Labels", {}).get("footfall.role") == "browsers":
            raise EngineError(f"OCI runtime create failed: {' '.join(config['Cmd'])}")
        real_start(container)

    monkeypatch.setattr(h.engine, "start", start)
    with pytest.raises(RunnerError) as caught:
        h.service.start_browsers(RUN, [])
    path = h.engine.configs[f"footfall-{RUN}-browsers-browsers"]["Cmd"][-1]
    assert path not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "OCI runtime create failed" in str(caught.value)
    assert "[the browsers' path]" in str(caught.value)


def test_a_folder_swapped_while_measured_is_skipped_and_the_rest_still_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap must not end the measure early. The swapped folder itself is not counted, so
    a run that keeps swapping can hide part of its size from any one measure; the measure
    counts what it skips, and a run whose measures keep skipping is stopped (see below)."""
    top = tmp_path / "top"
    for name in ("a", "z"):
        (top / name).mkdir(parents=True)
        (top / name / "big.bin").write_bytes(b"\x01" * 400_000)
    whole = _disk_usage(top)
    real_open = os.open
    swapped: list[str] = []

    def swap_the_first_folder(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path in ("a", "z") and not swapped:  # whichever the walk opens first
            swapped.append(path)
            (top / path).rename(top / f"{path}-moved")
            (top / path).symlink_to(top / ("z" if path == "a" else "a"))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_the_first_folder)
    measured = _disk_usage(top)
    assert swapped
    assert measured >= 400_000  # the other folder and its file are still counted
    assert measured <= whole  # and nothing is counted through the link


def test_a_measure_counts_what_it_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top = tmp_path / "top"
    (top / "folder").mkdir(parents=True)
    (top / "gone.bin").write_bytes(b"\x01" * 400_000)
    (top / "kept.txt").write_text("kept\n")
    assert _measure(top, MAX_DEPTH)[1] == 0
    real_stat = os.stat
    real_open = os.open

    def vanishing(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "gone.bin":
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    def changing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "folder":
            raise OSError(errno.ELOOP, "a link now")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "stat", vanishing)
    monkeypatch.setattr(os, "open", changing)
    usage, skipped = _measure(top, MAX_DEPTH)
    assert skipped == 2  # the file that vanished, and the folder that changed
    assert usage < 400_000


def unsteady(h: Harness, monkeypatch: pytest.MonkeyPatch, skips: list[int]) -> None:
    """Make each check of the run measure a little, skipping as `skips` says, in turn."""
    left = list(skips)

    def measure(top: Path, max_depth: int) -> tuple[int, int]:
        return PAGE, left.pop(0) if left else 0

    monkeypatch.setattr("footfall.runner.service._measure", measure)


def test_a_run_whose_measures_keep_skipping_is_stopped(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that keeps renaming or swapping its files could hide them from every measure,
    so three measures in a row that skip something stop it."""
    prepare(h)
    h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])
    unsteady(h, monkeypatch, [1, 3, 1])
    h.service.check_runs()
    h.service.check_runs()
    assert h.service.status(RUN) is None
    h.service.check_runs()
    assert h.service.status(RUN) == UNSTEADY_REASON
    assert UNSTEADY_REASON.startswith(MEASURE_REASON)
    assert_nothing_left(h)


def test_a_steady_measure_between_unsteady_ones_starts_the_count_again(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(h)
    unsteady(h, monkeypatch, [1, 1, 0, 1, 1])
    for _ in range(5):
        h.service.check_runs()
    assert h.service.status(RUN) is None
    h.service.start_browsers(RUN, [])


def test_the_budget_checks_count_towards_the_unsteady_measures(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(h)
    unsteady(h, monkeypatch, [1, 1, 1, 1, 1, 1])
    h.service.check_runs()
    h.service.check_runs()
    with pytest.raises(RunnerError, match="kept changing"):
        h.service.start_app(RUN, "anna", "python3 serve.py", 8000, [])  # its budget check
    assert h.service.status(RUN) == UNSTEADY_REASON
    assert_nothing_left(h)
