"""The fixed container settings are the sandbox's definition, so every value is asserted exactly."""

from pathlib import Path
from typing import Any, get_args

import pytest

from footfall.runner import spec
from footfall.runner.spec import APP_UID, LIMITS, Role, container_config, network_config

RUN_ID = "r_" + "a" * 26
PROXY = "http://footfall-proxy:3128"
ROLES: tuple[Role, ...] = get_args(Role)
APP_ROLES = [r for r in ROLES if r != "browsers"]


def make(role: Role = "app", **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "role": role,
        "run_id": RUN_ID,
        "copy": "main",
        "image": "footfall-python3.13:dev",
        "command": ["python", "-m", "app"],
        "workspace": Path("/srv/footfall/work/main"),
        "network": f"footfall-{RUN_ID}",
        "proxy_url": PROXY,
        "env": {},
    }
    kwargs.update(overrides)
    return container_config(**kwargs)


def host(role: Role = "app", **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = make(role, **overrides)["HostConfig"]
    return result


def test_limits_table() -> None:
    gib = 1024**3
    assert {r: (v.memory_bytes, v.nano_cpus, v.pids) for r, v in LIMITS.items()} == {
        "install": (gib, 10**9, 256),
        "seed": (gib // 2, 10**9, 256),
        "app": (gib // 2, 10**9, 256),
        # Twelve personas peaked at 1,985 MiB under the old 2 GiB on a droplet in phase 1.
        "browsers": (4 * gib, 2 * 10**9, 1024),
    }
    assert APP_UID == 10010


def test_top_level_shape() -> None:
    body = make()
    assert body["Image"] == "footfall-python3.13:dev"
    assert body["Cmd"] == ["python", "-m", "app"]
    assert set(body) == {"Image", "Cmd", "Env", "User", "Labels", "WorkingDir", "HostConfig"}
    assert set(make("browsers")) == {"Image", "Cmd", "Env", "Labels", "HostConfig"}


# Every key a container's HostConfig may have. Anything Docker would also accept, such as
# PortBindings, Devices, Mounts, CapAdd, IpcMode "host" or PidMode, is absent because the
# set is exact.
_HOST_KEYS = {
    "Runtime",
    "Init",
    "CapDrop",
    "SecurityOpt",
    "ReadonlyRootfs",
    "Privileged",
    "LogConfig",
    "IpcMode",
    "Ulimits",
    "Tmpfs",
    "Memory",
    "MemorySwap",
    "NanoCpus",
    "PidsLimit",
    "NetworkMode",
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("with_workspace", [True, False])
@pytest.mark.parametrize("with_proxy_name", [True, False])
def test_host_config_has_exactly_these_keys(
    role: Role, with_workspace: bool, with_proxy_name: bool
) -> None:
    overrides: dict[str, Any] = {}
    if not with_workspace:
        overrides["workspace"] = None
    if with_proxy_name:
        overrides |= {"proxy_url": "http://172.18.0.2:3128", "proxy_name": "footfall-proxy"}
    expected = set(_HOST_KEYS)
    expected |= {"Binds"} if with_workspace else set()
    expected |= {"ExtraHosts"} if with_proxy_name else set()
    expected |= {"ShmSize"} if role == "browsers" else set()
    assert set(host(role, **overrides)) == expected


@pytest.mark.parametrize("role", ROLES)
def test_env_is_exactly_the_proxy_the_marker_and_the_allowed_keys(role: Role) -> None:
    env = make(role, env={"PORT": "8000", "FOOTFALL_USERNAME": "u"})["Env"]
    assert env == [
        f"HTTP_PROXY={PROXY}",
        f"HTTPS_PROXY={PROXY}",
        f"http_proxy={PROXY}",
        f"https_proxy={PROXY}",
        "NO_PROXY=localhost,127.0.0.1",
        "FOOTFALL=1",
        "PORT=8000",
        "FOOTFALL_USERNAME=u",
    ]


@pytest.mark.parametrize("role", ROLES)
def test_every_hardening_setting_reaches_every_container(role: Role) -> None:
    """A setting added to the hardening table cannot be left out of a container."""
    hc = host(role)
    for key, value in spec._HARDENING.items():
        assert hc[key] == spec._thaw(value), key


def test_the_tables_cannot_be_changed() -> None:
    with pytest.raises(TypeError):
        spec._HARDENING["Privileged"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        spec._HARDENING["LogConfig"]["Type"] = "none"
    with pytest.raises(TypeError):
        LIMITS["app"] = LIMITS["browsers"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        spec._HARDENING["CapDrop"].append("X")


@pytest.mark.parametrize("role", ROLES)
def test_ipc_is_private(role: Role) -> None:
    # Its own IPC namespace, which no other container can join.
    assert host(role)["IpcMode"] == "private"


@pytest.mark.parametrize("role", ROLES)
def test_no_core_dumps(role: Role) -> None:
    # A crash writes no core file, which could hold what the process held, such as a password.
    assert host(role)["Ulimits"] == [{"Name": "core", "Soft": 0, "Hard": 0}]


@pytest.mark.parametrize("role", ROLES)
def test_runtime_defaults_to_runsc(role: Role) -> None:
    assert host(role)["Runtime"] == "runsc"


def test_runtime_override_only_runc() -> None:
    assert host(runtime="runc")["Runtime"] == "runc"
    for bad in ["kata", "", "RUNSC", "runsc "]:
        with pytest.raises(ValueError, match="runtime"):
            make(runtime=bad)


@pytest.mark.parametrize("role", ROLES)
def test_all_capabilities_dropped_none_added(role: Role) -> None:
    hc = host(role)
    assert hc["CapDrop"] == ["ALL"]
    assert "CapAdd" not in hc


@pytest.mark.parametrize("role", ROLES)
def test_no_new_privileges(role: Role) -> None:
    assert host(role)["SecurityOpt"] == ["no-new-privileges:true"]


@pytest.mark.parametrize("role", ROLES)
def test_read_only_root(role: Role) -> None:
    assert host(role)["ReadonlyRootfs"] is True


@pytest.mark.parametrize("role", ROLES)
def test_not_privileged(role: Role) -> None:
    assert host(role)["Privileged"] is False


@pytest.mark.parametrize("role", APP_ROLES)
def test_tmpfs_for_app_roles(role: Role) -> None:
    # noexec is what Docker adds to a tmpfs anyway; the spec says so rather than relying on it.
    assert host(role)["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=268435456"}  # noqa: S108


def test_tmpfs_for_browsers_adds_pwuser_home() -> None:
    opts = "rw,noexec,nosuid,nodev,size=268435456"
    assert host("browsers")["Tmpfs"] == {"/tmp": opts, "/home/pwuser": opts}  # noqa: S108


@pytest.mark.parametrize("role", ROLES)
def test_resource_limits(role: Role) -> None:
    hc = host(role)
    limits = LIMITS[role]
    assert hc["Memory"] == limits.memory_bytes
    assert hc["MemorySwap"] == limits.memory_bytes
    assert hc["NanoCpus"] == limits.nano_cpus
    assert hc["PidsLimit"] == limits.pids


def test_network_mode() -> None:
    assert host()["NetworkMode"] == f"footfall-{RUN_ID}"


def test_binds_with_workspace() -> None:
    assert host(workspace=Path("/srv/w/main"))["Binds"] == ["/srv/w/main:/workspace:rw"]


def test_no_binds_without_workspace() -> None:
    assert "Binds" not in host(workspace=None)


@pytest.mark.parametrize("role", ROLES)
def test_forbidden_keys_absent(role: Role) -> None:
    body = make(role)
    hc = body["HostConfig"]
    for key in ("PortBindings", "Devices", "ExposedPorts", "Mounts", "CapAdd", "PidMode"):
        assert key not in body
        assert key not in hc


@pytest.mark.parametrize("role", APP_ROLES)
def test_user_for_app_roles(role: Role) -> None:
    assert make(role)["User"] == "10010:10010"


def test_browsers_has_no_user() -> None:
    assert "User" not in make("browsers")


@pytest.mark.parametrize("role", APP_ROLES)
def test_working_dir_workspace(role: Role) -> None:
    assert make(role)["WorkingDir"] == "/workspace"


def test_browsers_has_no_working_dir() -> None:
    assert "WorkingDir" not in make("browsers")


def test_env_fixed_entries() -> None:
    env = make()["Env"]
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{name}={PROXY}" in env
    assert "NO_PROXY=localhost,127.0.0.1" in env
    assert "FOOTFALL=1" in env


def test_env_allowed_keys_are_passed_through() -> None:
    env = make(env={"FOOTFALL_USERNAME": "u", "FOOTFALL_PASSWORD": "p", "PORT": "8000"})["Env"]
    assert {"FOOTFALL_USERNAME=u", "FOOTFALL_PASSWORD=p", "PORT=8000"} <= set(env)


@pytest.mark.parametrize(
    "key", ["PATH", "HTTP_PROXY", "NO_PROXY", "FOOTFALL", "footfall_username", ""]
)
def test_env_keys_outside_allowlist_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="env"):
        make(env={key: "x"})


@pytest.mark.parametrize("value", ["a\nb", "a\x00b", "\n"])
def test_env_values_with_nul_or_newline_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="env"):
        make(env={"PORT": value})


def test_labels() -> None:
    assert make("seed", copy="feature-1")["Labels"] == {
        "footfall.run": RUN_ID,
        "footfall.role": "seed",
        "footfall.copy": "feature-1",
    }


@pytest.mark.parametrize("role", ROLES)
def test_log_config(role: Role) -> None:
    assert host(role)["LogConfig"] == {
        "Type": "json-file",
        "Config": {"max-size": "5m", "max-file": "2"},
    }


def test_pure_same_inputs_same_dict() -> None:
    assert make() == make()
    first = make()
    first["HostConfig"]["CapDrop"].append("X")
    first["HostConfig"]["Tmpfs"]["/x"] = "y"
    assert make()["HostConfig"]["CapDrop"] == ["ALL"]
    assert "/x" not in make()["HostConfig"]["Tmpfs"]


@pytest.mark.parametrize("bad", ["install2", "", "APP", "browser"])
def test_bad_role_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="role"):
        make(role=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "r_" + "a" * 25,
        "r_" + "a" * 27,
        "r_" + "1" * 26,
        "R_" + "a" * 26,
        "x_" + "a" * 26,
        "r_" + "a" * 26 + "\n",
    ],
)
def test_bad_run_id_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        make(run_id=bad)


@pytest.mark.parametrize("bad", ["", "1a", "-a", "A", "a_b", "a" * 33, "a\n"])
def test_bad_copy_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="copy"):
        make(copy=bad)


def test_copy_boundaries_accepted() -> None:
    assert make(copy="a")["Labels"]["footfall.copy"] == "a"
    assert make(copy="a" + "b" * 31)["Labels"]["footfall.copy"] == "a" + "b" * 31


@pytest.mark.parametrize("bad", [[], (), "python", b"x", ["ok", 1], [None]])
def test_bad_command_rejected(bad: Any) -> None:
    with pytest.raises(ValueError, match="command"):
        make(command=bad)


def test_command_accepts_tuple() -> None:
    assert make(command=("a", "b"))["Cmd"] == ["a", "b"]


def test_relative_workspace_rejected() -> None:
    with pytest.raises(ValueError, match="workspace"):
        make(workspace=Path("work/main"))


@pytest.mark.parametrize(
    "bad",
    [
        "/srv/w:/etc",  # a colon would end the bind's source and name another target
        "/srv/w/main:ro",
        "/srv/w/../../etc",
        "/srv/w/..",
        "/srv/w/main\n",
        "/srv/w/ma\x00in",
        "/srv/w/\x7f",
    ],
)
def test_workspace_that_could_reshape_the_bind_is_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="workspace"):
        make(workspace=Path(bad))


@pytest.mark.parametrize(
    "bad",
    [
        "bridge",
        "host",
        "none",
        "",
        "footfall-x",
        "footfall_r_x",
        "footfall-r_xyz",
        "footfall-r_" + "b" * 26,  # another run's network
        f"footfall-{RUN_ID}-x",
    ],
)
def test_bad_network_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="network"):
        make(network=bad)


@pytest.mark.parametrize(
    "good", ["http://footfall-proxy:3128", "http://172.18.0.2:3128", "http://[fd00::2]:3128"]
)
def test_proxy_urls_footfall_makes_are_accepted(good: str) -> None:
    assert f"HTTP_PROXY={good}" in make(proxy_url=good)["Env"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "footfall-proxy:3128",
        "https://172.18.0.2:3128",
        "socks5://172.18.0.2:1080",
        "http://172.18.0.2",
        "http://172.18.0.2:0",
        "http://172.18.0.2:70000",
        "http://user:secret@172.18.0.2:3128",
        "http://172.18.0.2:3128/path",
        "http://172.18.0.2:3128/",
        "http://172.18.0.2:3128?x=1",
        "http://172.18.0.2:3128#x",
        "http://172.18.0.2:3128\nEVIL=1",
        "http://Footfall Proxy:3128",
        "http://:3128",
    ],
)
def test_bad_proxy_url_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="proxy"):
        make(proxy_url=bad)


@pytest.mark.parametrize("role", ROLES)
def test_every_container_has_an_init(role: Role) -> None:
    # A process of the container's own reaps its zombies and passes signals on.
    assert host(role)["Init"] is True


def test_browsers_get_shared_memory_for_chromium() -> None:
    assert host("browsers")["ShmSize"] == 512 * 1024**2
    assert host("browsers")["ShmSize"] < LIMITS["browsers"].memory_bytes


@pytest.mark.parametrize("role", APP_ROLES)
def test_app_roles_keep_dockers_shared_memory(role: Role) -> None:
    assert "ShmSize" not in host(role)


@pytest.mark.parametrize("role", ROLES)
def test_the_proxy_has_its_name_in_every_containers_hosts_file(role: Role) -> None:
    # Docker's own name service does not answer inside gVisor, so the browsers would not
    # find the proxy by the name footfall test gives them.
    hc = host(role, proxy_url="http://172.18.0.2:3128", proxy_name="footfall-proxy")
    assert hc["ExtraHosts"] == ["footfall-proxy:172.18.0.2"]


def test_no_hosts_entry_without_a_proxy_name() -> None:
    assert "ExtraHosts" not in host()


@pytest.mark.parametrize(
    ("proxy_url", "proxy_name"),
    [
        ("http://footfall-proxy:3128", "footfall-proxy"),  # the address must be an address
        ("http://172.18.0.2:3128", "Footfall Proxy"),
        ("http://172.18.0.2:3128", "proxy\nevil"),
        ("http://172.18.0.2:3128", ""),
    ],
)
def test_bad_proxy_hosts_entries_are_refused(proxy_url: str, proxy_name: str) -> None:
    with pytest.raises(ValueError, match="proxy"):
        make(proxy_url=proxy_url, proxy_name=proxy_name)


def test_a_runs_network_is_internal_and_the_host_has_no_address_on_it() -> None:
    assert network_config(RUN_ID) == {
        "Name": f"footfall-{RUN_ID}",
        "Internal": True,
        "Labels": {"footfall.run": RUN_ID},
        "CheckDuplicate": True,
        # Without this the host takes the network's first address on its bridge, and every
        # container on the network can reach whatever the host serves there, its SSH too.
        "Options": {"com.docker.network.bridge.inhibit_ipv4": "true"},
    }


@pytest.mark.parametrize("bad", ["", "r_short", "r_" + "A" * 26, "x" + RUN_ID])
def test_a_network_for_a_bad_run_id_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        network_config(bad)
