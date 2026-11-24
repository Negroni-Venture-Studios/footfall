"""The egress proxy's allow lists, one per client address."""

from typing import Any

import pytest

from footfall.proxy.policy import ALLOWED_PORTS, UNKNOWN_CLIENT, Decision, Policy

RUN = "r_abcdefghijklmnopqrstuvwxyz"
OTHER = "r_" + "q" * 26
APP = "10.89.0.10"
BROWSERS = "10.89.0.11"


@pytest.fixture
def policy() -> Policy:
    value = Policy()
    value.allow(APP, RUN, ["api.example.com", "Tiles.Example.ORG"])
    return value


def test_allowed_ports_are_80_and_443() -> None:
    assert frozenset({80, 443}) == ALLOWED_PORTS


def test_exact_names_only(policy: Policy) -> None:
    assert policy.decide(APP, "api.example.com", 443) == Decision(True, "allowed", RUN)
    assert policy.decide(APP, "API.EXAMPLE.COM", 443).allowed
    assert policy.decide(APP, "tiles.example.org", 80).allowed
    for host in (
        "example.com",
        "x.api.example.com",
        "api.example.com.attacker.example",
        "api-example.com",
        "fonts.gstatic.com",
    ):
        decision = policy.decide(APP, host, 443)
        assert not decision.allowed, host
        assert decision.run_id == RUN
        assert "not on" in decision.reason


@pytest.mark.parametrize(
    "host", ["169.254.169.254", "127.0.0.1", "127.1", "[::1]", "::1", "0x7f.1", "", "a b.com"]
)
def test_addresses_and_bad_names_are_refused(policy: Policy, host: str) -> None:
    decision = policy.decide(APP, host, 443)
    assert decision == Decision(False, "not a valid host name", RUN)


@pytest.mark.parametrize("port", [0, 22, 8080, 8443, 65536, -1])
def test_other_ports_are_refused(policy: Policy, port: int) -> None:
    decision = policy.decide(APP, "api.example.com", port)
    assert decision == Decision(False, f"port {port} is not allowed", RUN)


def test_unknown_client(policy: Policy) -> None:
    assert policy.decide(BROWSERS, "api.example.com", 443) == Decision(False, UNKNOWN_CLIENT, None)
    assert policy.decide("not an address", "api.example.com", 443) == Decision(
        False, UNKNOWN_CLIENT, None
    )


def test_an_ipv4_mapped_client_address_is_the_ipv4_address(policy: Policy) -> None:
    assert policy.decide(f"::ffff:{APP}", "api.example.com", 443).allowed
    policy.allow(f"::ffff:{BROWSERS}", RUN, ["fonts.gstatic.com"])
    assert policy.decide(BROWSERS, "fonts.gstatic.com", 443).allowed


def test_revoke(policy: Policy) -> None:
    policy.revoke(APP)
    assert policy.decide(APP, "api.example.com", 443).run_id is None
    policy.revoke(APP)  # twice is harmless
    policy.revoke(BROWSERS)  # never allowed


def test_allow_replaces_the_earlier_list_of_the_same_run(policy: Policy) -> None:
    policy.allow(APP, RUN, ["fonts.gstatic.com"])
    assert not policy.decide(APP, "api.example.com", 443).allowed
    assert policy.decide(APP, "fonts.gstatic.com", 443) == Decision(True, "allowed", RUN)
    policy.allow(APP, RUN, [])
    assert not policy.decide(APP, "fonts.gstatic.com", 443).allowed
    assert policy.decide(APP, "fonts.gstatic.com", 443).run_id == RUN


def test_allow_never_takes_an_address_from_another_run(policy: Policy) -> None:
    with pytest.raises(ValueError, match="another run"):
        policy.allow(APP, OTHER, ["fonts.gstatic.com"])
    with pytest.raises(ValueError, match="another run"):
        policy.allow(f"::ffff:{APP}", OTHER, [])
    assert policy.decide(APP, "api.example.com", 443) == Decision(True, "allowed", RUN)
    assert not policy.decide(APP, "fonts.gstatic.com", 443).allowed
    policy.revoke(APP)  # once revoked, the address may go to any run
    policy.allow(APP, OTHER, ["fonts.gstatic.com"])
    assert policy.decide(APP, "fonts.gstatic.com", 443) == Decision(True, "allowed", OTHER)


def test_revoke_run_revokes_every_list_of_that_run(policy: Policy) -> None:
    policy.allow(BROWSERS, RUN, ["fonts.gstatic.com"])
    policy.allow("10.90.0.10", OTHER, ["api.example.com"])
    policy.revoke_run(RUN)
    assert policy.decide(APP, "api.example.com", 443).run_id is None
    assert policy.decide(BROWSERS, "fonts.gstatic.com", 443).run_id is None
    assert policy.decide("10.90.0.10", "api.example.com", 443).allowed
    policy.revoke_run(RUN)  # twice is harmless


@pytest.mark.parametrize(
    ("ip", "run_id", "hosts"),
    [
        ("10.89.0.300", RUN, ["api.example.com"]),
        ("", RUN, ["api.example.com"]),
        (APP, "r_short", ["api.example.com"]),
        (APP, "../../etc", ["api.example.com"]),
        (APP, RUN, ["169.254.169.254"]),
        (APP, RUN, ["*.example.com"]),
        (APP, RUN, "api.example.com"),
    ],
)
def test_allow_refuses_bad_input_and_changes_nothing(
    policy: Policy, ip: str, run_id: str, hosts: Any
) -> None:
    with pytest.raises(ValueError, match=r"."):
        policy.allow(ip, run_id, hosts)
    assert policy.decide(APP, "api.example.com", 443) == Decision(True, "allowed", RUN)


def test_revoke_refuses_bad_input() -> None:
    policy = Policy()
    with pytest.raises(ValueError, match="address"):
        policy.revoke("not an address")
    with pytest.raises(ValueError, match="run id"):
        policy.revoke_run("r_short")
    with pytest.raises(ValueError, match="run id"):
        policy.reached("r_short")


def test_reached_is_deduplicated_and_sorted(policy: Policy) -> None:
    assert policy.reached(RUN) == []
    policy.record(RUN, "tiles.example.org")
    policy.record(RUN, "api.example.com")
    policy.record(RUN, "tiles.example.org")
    policy.record(OTHER, "fonts.gstatic.com")
    assert policy.reached(RUN) == ["api.example.com", "tiles.example.org"]
    assert policy.reached(OTHER) == ["fonts.gstatic.com"]
    policy.revoke_run(RUN)  # the run's page still needs what it reached
    assert policy.reached(RUN) == ["api.example.com", "tiles.example.org"]


def test_reached_remembers_a_bounded_number_of_runs() -> None:
    policy = Policy(remembered_runs=2)
    runs = ["r_" + letter * 26 for letter in "abc"]
    for run_id in runs:
        policy.record(run_id, "api.example.com")
    assert policy.reached(runs[0]) == []
    assert policy.reached(runs[1]) == ["api.example.com"]
    assert policy.reached(runs[2]) == ["api.example.com"]
    with pytest.raises(ValueError, match="remembered_runs"):
        Policy(remembered_runs=0)
