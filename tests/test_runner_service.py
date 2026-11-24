"""The runner's logic, against a fake Docker engine and a fake proxy, with real folders."""

import os
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from footfall.runner.engine import EngineError
from footfall.runner.service import (
    DISK_REASON,
    REGISTRIES,
    RunnerError,
    RunnerService,
    _copy_file,
    _copy_tree,
)
from footfall.runner.spec import container_config
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
PASSWORD = "pw-7f3k2m9q4x8z"
MIB = 1024 * 1024


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
    assert ("create_network", NETWORK, {"footfall.run": RUN}) in h.engine.calls
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


def test_start_browsers_runs_run_server_with_the_assets_hosts(h: Harness) -> None:
    prepare(h, egress=["api.example.com"])
    endpoint = h.service.start_browsers(RUN, ["fonts.gstatic.com", "Tiles.Example.org"])
    name = f"footfall-{RUN}-browsers-browsers"
    assert endpoint == f"ws://{h.engine.ips[name]}:3000/"
    config = h.engine.configs[name]
    assert config["Image"] == BROWSERS_IMAGE
    assert config["Cmd"] == ["playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]  # noqa: S104
    assert config["Labels"] == {
        "footfall.run": RUN,
        "footfall.role": "browsers",
        "footfall.copy": "browsers",
    }
    assert "Binds" not in config["HostConfig"]
    assert "User" not in config
    assert h.proxy.allowed[h.engine.ips[name]] == (RUN, ("fonts.gstatic.com", "tiles.example.org"))
    with pytest.raises(RunnerError, match="already"):
        h.service.start_browsers(RUN, [])


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
    with pytest.raises(RunnerError, match="not been prepared"):
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
    with pytest.raises(RunnerError, match=r"FIFO at deep/pipe"):
        prepare(h)
    assert h.engine.calls == []
    assert not h.runs.exists()


def test_fifo_left_by_seed_refused(h: Harness) -> None:
    h.engine.during_wait["seed"] = lambda w: os.mkfifo(w / "pipe")
    with pytest.raises(RunnerError, match="FIFO at pipe"):
        prepare(h, seed="python3 seed.py")
    assert_nothing_left(h)


def test_copy_refuses_a_fifo_the_scan_did_not_see(tmp_path: Path) -> None:
    # The scan before a copy catches a FIFO; this is the second wall, for one made after it.
    source = tmp_path / "source"
    source.mkdir()
    (source / "serve.py").write_text("print('hi')\n")
    os.mkfifo(source / "pipe")
    with pytest.raises(RunnerError, match=r"could not copy source: .*FIFO"):
        _copy_tree(source, tmp_path / "target")


def test_copy_function_refuses_anything_but_a_regular_file(tmp_path: Path) -> None:
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(OSError, match="FIFO"):
        _copy_file(str(pipe), str(tmp_path / "out-pipe"))  # returns at once, never blocks
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "target")
    (tmp_path / "target").write_text("followed\n")
    with pytest.raises(OSError):
        _copy_file(str(link), str(tmp_path / "out-link"))
    assert not (tmp_path / "out-pipe").exists()
    assert not (tmp_path / "out-link").exists()

    source = tmp_path / "tool"
    source.write_bytes(b"\x00\x01" * 700_000)
    source.chmod(0o4755)
    _copy_file(str(source), str(tmp_path / "copied"))
    copied = tmp_path / "copied"
    assert copied.read_bytes() == source.read_bytes()
    assert copied.stat().st_mode & 0o7777 == 0o755  # no set-user-id bit
    assert copied.stat().st_mtime_ns == source.stat().st_mtime_ns


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
    # Nothing is remembered: the same run can be prepared again.
    prepare(h)


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
    h.service.check_disks()
    assert h.service.status(RUN) is None

    (h.runs / RUN / "copies" / "anna" / "upload.bin").write_bytes(b"\x01" * MIB)
    h.service.check_disks()
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


def test_disk_watch_counts_hard_links_once(tmp_path: Path) -> None:
    h = make_harness(tmp_path, disk_budget=int(1.5 * MIB))
    prepare(h)
    prepared = h.runs / RUN / "prepared"
    (prepared / "one.bin").write_bytes(b"\x01" * MIB)
    for index in range(3):
        (prepared / f"link{index}.bin").hardlink_to(prepared / "one.bin")
    h.service.check_disks()
    assert h.service.status(RUN) is None


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
        in_another_thread(h.service.check_disks)

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
    h.service.check_disks()
    assert h.service.status(RUN) == DISK_REASON
    h.engine.failures.clear()
    h.service.stop(RUN)
    assert_nothing_left(h)


def test_status_is_none_while_live_and_for_unknown_runs(h: Harness) -> None:
    assert h.service.status(RUN) is None
    prepare(h)
    assert h.service.status(RUN) is None
    h.service.stop(RUN)
    assert h.service.status(RUN) is None
