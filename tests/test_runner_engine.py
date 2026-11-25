"""The Docker Engine client, against a fake daemon on a Unix socket."""

import json
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from footfall.runner.engine import DockerEngine, EngineError, _demultiplex
from tests.fake_docker import FakeDocker, Reply, frame, json_reply, serve


@pytest.fixture
def docker() -> Iterator[tuple[FakeDocker, DockerEngine]]:
    # pytest's tmp_path is too deep for a Unix socket path (about 100 bytes at most on macOS),
    # so the socket lives in a short temporary directory of its own.
    with tempfile.TemporaryDirectory(prefix="ff") as directory:
        path = Path(directory) / "d.sock"
        with serve(path) as fake:
            yield fake, DockerEngine(path)


def test_create_sends_name_as_query_and_returns_id(
    docker: tuple[FakeDocker, DockerEngine],
) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/create", json_reply(201, {"Id": "abc123"}))
    assert engine.create("footfall-r_x-app-a b", {"Image": "img"}) == "abc123"
    (request,) = fake.requests
    assert request.query == "name=footfall-r_x-app-a%20b"
    assert request.json() == {"Image": "img"}


def test_start(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/c1/start", Reply(204))
    engine.start("c1")
    assert fake.seen("POST", "/containers/c1/start")


def test_start_already_started_is_success(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/c1/start", Reply(304))
    engine.start("c1")


def test_wait_returns_status_code(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/c1/wait", json_reply(200, {"StatusCode": 3}))
    assert engine.wait("c1", timeout=5) == 3
    assert not fake.seen("POST", "/containers/c1/kill")


def test_wait_timeout_kills(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    gate = threading.Event()
    fake.on("POST", "/containers/x/wait", Reply(200, b'{"StatusCode": 0}', gate=gate))
    fake.on("POST", "/containers/x/kill", Reply(204))
    try:
        with pytest.raises(TimeoutError):
            engine.wait("x", timeout=0.2)
    finally:
        gate.set()
    assert fake.seen("POST", "/containers/x/kill")


def test_a_failed_kill_still_reports_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timeout is the news: a kill that fails, however it fails, must not replace it."""
    engine = DockerEngine(Path("/nowhere/d.sock"))
    seen: list[str] = []

    def request(method: str, target: str, *args: object, **kwargs: object) -> tuple[int, bytes]:
        seen.append(target)
        if target.endswith("/wait"):
            raise TimeoutError("timed out")
        raise ConnectionRefusedError("the daemon went away")

    monkeypatch.setattr(engine, "_request", request)
    with pytest.raises(TimeoutError, match=r"still running after 0\.2s"):
        engine.wait("x", timeout=0.2)
    assert seen == ["/containers/x/wait", "/containers/x/kill"]


@pytest.mark.parametrize(
    ("method", "path", "call", "payload"),
    [
        ("POST", "/containers/create", lambda e: e.create("n", {}), ["Id", "abc"]),
        ("POST", "/containers/c1/wait", lambda e: e.wait("c1", 5), [0]),
        ("GET", "/containers/c1/json", lambda e: e.address("c1", "net"), "text"),
        ("GET", "/containers/c1/json", lambda e: e.address("c1", "net"), {"NetworkSettings": 1}),
        (
            "GET",
            "/containers/c1/json",
            lambda e: e.address("c1", "net"),
            {"NetworkSettings": {"Networks": {"net": "10.0.0.7"}}},
        ),
        ("GET", "/containers/json", lambda e: e.containers("l"), {"Id": "a"}),
        ("GET", "/containers/json", lambda e: e.containers("l"), [{"Names": ["a"]}]),
        ("GET", "/containers/json", lambda e: e.containers("l"), ["a"]),
        ("GET", "/containers/json", lambda e: e.containers("l"), [{"Id": 7}]),
    ],
)
def test_a_reply_of_the_wrong_shape_is_an_engine_error(
    docker: tuple[FakeDocker, DockerEngine],
    method: str,
    path: str,
    call: Callable[[DockerEngine], object],
    payload: object,
) -> None:
    fake, engine = docker
    fake.on(method, path, json_reply(200, payload))
    with pytest.raises(EngineError):
        call(engine)


def test_logs_of_a_terminal_are_read_as_they_come() -> None:
    """With a terminal, Docker sends the output as it is, with no frame headers."""
    assert _demultiplex(b"plain output\nfrom a terminal\n") == "plain output\nfrom a terminal\n"
    assert _demultiplex(b"") == ""
    assert _demultiplex(frame(1, b"framed")) == "framed"


def test_logs_demultiplexes(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    body = frame(1, b"hello ") + frame(2, "wörld\n".encode()) + frame(1, b"\xff!")
    fake.on("GET", "/containers/c1/logs", Reply(200, body))
    assert engine.logs("c1", tail=50) == "hello wörld\n�!"
    (request,) = fake.requests
    assert request.query == "stdout=1&stderr=1&tail=50"


def test_address(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    info = {"NetworkSettings": {"Networks": {"net": {"IPAddress": "10.0.0.7"}}}}
    fake.on("GET", "/containers/c1/json", json_reply(200, info))
    assert engine.address("c1", "net") == "10.0.0.7"


@pytest.mark.parametrize(
    "info",
    [
        {"NetworkSettings": {"Networks": {}}},
        {"NetworkSettings": {"Networks": {"net": {"IPAddress": ""}}}},
        {},
    ],
)
def test_address_absent_raises(
    docker: tuple[FakeDocker, DockerEngine], info: dict[str, object]
) -> None:
    fake, engine = docker
    fake.on("GET", "/containers/c1/json", json_reply(200, info))
    with pytest.raises(EngineError):
        engine.address("c1", "net")


def test_remove_forces(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("DELETE", "/containers/c1", Reply(204))
    engine.remove("c1")
    (request,) = fake.requests
    assert request.query == "force=1"


def test_remove_missing_is_success(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("DELETE", "/containers/gone", json_reply(404, {"message": "No such container"}))
    engine.remove("gone")


def test_create_network_posts_the_body_it_is_given(
    docker: tuple[FakeDocker, DockerEngine],
) -> None:
    fake, engine = docker
    fake.on("POST", "/networks/create", json_reply(201, {"Id": "n1"}))
    body = {"Name": "footfall-r_x", "Internal": True, "Options": {"o": "1"}}
    engine.create_network(body)
    (request,) = fake.requests
    assert request.json() == body


def test_create_network_exists_raises(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/networks/create", json_reply(409, {"message": "network exists"}))
    with pytest.raises(EngineError, match="network exists"):
        engine.create_network({"Name": "n"})


def test_connect(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/networks/n1/connect", Reply(200))
    engine.connect("n1", "c1")
    assert fake.requests[0].json() == {"Container": "c1"}


def test_disconnect_forces(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/networks/n1/disconnect", Reply(200))
    engine.disconnect("n1", "c1")
    assert fake.requests[0].json() == {"Container": "c1", "Force": True}


def test_disconnect_missing_is_success(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/networks/n1/disconnect", json_reply(404, {"message": "no such network"}))
    engine.disconnect("n1", "c1")


@pytest.mark.parametrize("network", ["the network", "network"])
def test_disconnect_not_connected_is_success(
    docker: tuple[FakeDocker, DockerEngine], network: str
) -> None:
    # What dockerd 28 answers, forced or not, for a container already off the network, and
    # for a network that has gone (recorded by the isolation workflow).
    fake, engine = docker
    message = f"container 0a1b is not connected to {network} footfall-r_x"
    fake.on("POST", "/networks/n1/disconnect", json_reply(500, {"message": message}))
    engine.disconnect("n1", "c1")


def test_disconnect_other_server_errors_raise(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/networks/n1/disconnect", json_reply(500, {"message": "the disk is full"}))
    with pytest.raises(EngineError, match="the disk is full"):
        engine.disconnect("n1", "c1")


def test_remove_network(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("DELETE", "/networks/n1", Reply(204))
    engine.remove_network("n1")
    assert fake.seen("DELETE", "/networks/n1")


def test_remove_network_missing_is_success(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("DELETE", "/networks/n1", json_reply(404, {"message": "no such network"}))
    engine.remove_network("n1")


def test_containers_filters_by_label(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("GET", "/containers/json", json_reply(200, [{"Id": "a"}, {"Id": "b"}]))
    assert engine.containers("footfall.run=r_x") == ["a", "b"]
    (request,) = fake.requests
    params = parse_qs(request.query)
    assert params["all"] == ["1"]
    assert json.loads(params["filters"][0]) == {"label": ["footfall.run=r_x"]}


def test_path_segments_are_encoded(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/a%2Fb/start", Reply(204))
    engine.start("a/b")
    assert fake.requests[0].target == "/containers/a%2Fb/start"


def test_error_carries_docker_message(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/create", json_reply(409, {"message": "name already in use"}))
    with pytest.raises(EngineError, match="name already in use") as caught:
        engine.create("n", {})
    assert "409" in str(caught.value)


def test_error_without_json_body_still_raises(docker: tuple[FakeDocker, DockerEngine]) -> None:
    fake, engine = docker
    fake.on("POST", "/containers/c1/start", Reply(500, b"boom"))
    with pytest.raises(EngineError, match="500"):
        engine.start("c1")
