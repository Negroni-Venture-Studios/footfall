"""The egress proxy in the real stack: what an app container may reach through it, and when.

Each probe fetches through the proxy named in its own environment, as an app's HTTP client
would, and the proxy's own log says what it decided. These tests reach pypi.org and
example.com, and only through the proxy.
"""

import json

import pytest

from tests.isolation.live import PROBE, LiveRun, NewRun, ProxyLog

pytestmark = pytest.mark.isolation

NOT_LISTED = "is not on this client's allow list"


def _host(address: str) -> str:
    return address.rpartition(":")[0]


async def test_a_manifest_host_is_reachable(run: LiveRun, proxy_log: ProxyLog) -> None:
    result = await run.probe("listed", "fetch", "https://example.com/", egress=["example.com"])
    fetched = result["https://example.com/"]
    assert fetched["proxy_status"] == 200, fetched
    assert fetched["status"] == 200, fetched
    await proxy_log.wait_for(_host(run.copies["listed"]), "example.com", "allowed")


async def test_a_host_not_on_the_list_gets_403(run: LiveRun, proxy_log: ProxyLog) -> None:
    urls = ["https://pypi.org/simple/", "http://example.org/", "https://www.example.com/"]
    result = await run.probe("unlisted", "fetch", *urls, egress=["example.com"])
    assert result["https://pypi.org/simple/"]["proxy_status"] == 403, result
    assert result["https://www.example.com/"]["proxy_status"] == 403, result
    assert result["http://example.org/"]["status"] == 403, result
    for url in urls:
        assert "status" not in result[url] or result[url]["status"] == 403
    client = _host(run.copies["unlisted"])
    for host in ("pypi.org", "example.org", "www.example.com"):
        await proxy_log.wait_for(client, host, NOT_LISTED)
        assert [line for line in proxy_log.decisions(client, host) if "allowed" in line] == []


async def test_addresses_and_the_metadata_service_are_refused(
    run: LiveRun, proxy_log: ProxyLog
) -> None:
    urls = ["http://169.254.169.254/latest/meta-data/", "https://1.1.1.1/"]
    result = await run.probe("addresses", "fetch", *urls, egress=["example.com"])
    assert result[urls[0]]["status"] == 403, result
    assert result[urls[1]]["proxy_status"] == 403, result
    client = _host(run.copies["addresses"])
    assert [line for line in proxy_log.decisions(client) if " allowed" in line] == []


async def test_registries_are_reachable_during_install_and_refused_after(
    new_run: NewRun, proxy_log: ProxyLog
) -> None:
    run = await new_run(install=f"{PROBE} fetch-to-file install.json https://pypi.org/simple/")
    during = await run.probe("during", "read", "install.json")
    fetched = json.loads(during["install.json"])["https://pypi.org/simple/"]
    assert (fetched["proxy_status"], fetched["status"]) == (200, 200), fetched
    after = await run.probe("after", "fetch", "https://pypi.org/simple/")
    assert after["https://pypi.org/simple/"]["proxy_status"] == 403, after
    await proxy_log.wait_for(_host(run.copies["after"]), "pypi.org", NOT_LISTED)


async def test_footfall_proxy_resolves_on_a_run_network(run: LiveRun) -> None:
    sandbox = await run.probe("sandbox", "sandbox")
    proxy = sandbox["proxy"].removeprefix("http://").rpartition(":")[0]
    resolved = await run.probe("resolve", "resolve", "footfall-proxy")
    assert resolved == {"footfall-proxy": [proxy]}


async def test_the_proxy_answers_on_its_port_alone(run: LiveRun) -> None:
    sandbox = await run.probe("sandbox", "sandbox")
    proxy = sandbox["proxy"].removeprefix("http://").rpartition(":")[0]
    scan = await run.probe("scan", "scan", f"{proxy}:1-65535")
    assert scan[f"{proxy}:1-65535"]["open"] == [3128], scan
