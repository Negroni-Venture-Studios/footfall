"""A small Docker Engine API client.

Footfall's runner is the only process that talks to Docker, and it does so through this module.
It uses the standard library alone: `http.client` over the daemon's Unix socket, with unversioned
Engine API paths, so there is no Docker SDK to depend on. Each request opens its own connection,
which is simple and fast enough for the handful of calls a run makes.
"""

import contextlib
import http.client
import json
import re
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import quote, urlencode

DEFAULT_SOCKET: Final = Path("/var/run/docker.sock")
_TIMEOUT: Final = 30.0
_FRAME_HEADER: Final = 8
_NOT_CONNECTED: Final = re.compile(r"^container \S+ is not connected to (?:the )?network \S+$")


class EngineError(RuntimeError):
    """Docker answered with an error, or with something this client cannot use."""


class Engine(Protocol):
    """What the runner needs from Docker, so that tests and the runner can use a fake."""

    def create(self, name: str, config: dict[str, Any]) -> str: ...
    def start(self, container: str) -> None: ...
    def wait(self, container: str, timeout: float) -> int: ...
    def logs(self, container: str, tail: int = 200) -> str: ...
    def address(self, container: str, network: str) -> str: ...
    def has_stopped(self, container: str) -> bool: ...
    def remove(self, container: str) -> None: ...
    def create_network(self, config: Mapping[str, Any]) -> None: ...
    def connect(self, network: str, container: str) -> None: ...
    def disconnect(self, network: str, container: str) -> None: ...
    def remove_network(self, name: str) -> None: ...
    def containers(self, label: str) -> list[str]: ...
    def labelled(self, key: str) -> list[tuple[str, str]]: ...
    def labelled_networks(self, key: str) -> list[tuple[str, str]]: ...


class _UnixConnection(http.client.HTTPConnection):
    """An `HTTPConnection` whose socket is an `AF_UNIX` socket."""

    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self._socket_path))
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _seg(value: str) -> str:
    """Encode one path segment, slashes included."""
    return quote(value, safe="")


def _is_frame_header(data: bytes, offset: int) -> bool:
    """Whether a frame header starts at `offset`: a stream of 0, 1 or 2, then three zeros."""
    header = data[offset : offset + _FRAME_HEADER]
    return len(header) == _FRAME_HEADER and header[0] in (0, 1, 2) and header[1:4] == b"\0\0\0"


def _demultiplex(data: bytes) -> str:
    """Join the payloads of Docker's multiplexed log frames and decode them as UTF-8.

    A container with a terminal has its output sent as it is, with no frames. The runner
    never gives a container one, but output that does not start with a frame header is
    read as it comes all the same, rather than taken apart as frames it is not.
    """
    if data and not _is_frame_header(data, 0):
        return data.decode("utf-8", errors="replace")
    payloads: list[bytes] = []
    offset = 0
    while offset + _FRAME_HEADER <= len(data):
        size = int.from_bytes(data[offset + 4 : offset + _FRAME_HEADER], "big")
        start = offset + _FRAME_HEADER
        payloads.append(data[start : start + size])
        offset = start + size
    return b"".join(payloads).decode("utf-8", errors="replace")


def _field(value: Any, *keys: str) -> Any:
    """The value at `keys` inside nested JSON objects, or None where one is missing or not one."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _message(body: bytes) -> str:
    try:
        decoded = json.loads(body)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("message"), str):
        return str(decoded["message"])
    return body.decode("utf-8", errors="replace").strip() or "no message"


class DockerEngine:
    """The Docker Engine API over a Unix socket."""

    def __init__(self, socket_path: Path = DEFAULT_SOCKET) -> None:
        self._socket_path = socket_path

    def _request(
        self,
        method: str,
        target: str,
        body: Mapping[str, Any] | None = None,
        *,
        ok: frozenset[int] = frozenset(),
        timeout: float = _TIMEOUT,
    ) -> tuple[int, bytes]:
        """Send one request. Any non-2xx status not listed in `ok` raises `EngineError`."""
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection = _UnixConnection(self._socket_path, timeout)
        try:
            connection.request(method, target, body=payload, headers=headers)
            response = connection.getresponse()
            status, data = response.status, response.read()
        finally:
            connection.close()
        if not 200 <= status < 300 and status not in ok:
            raise EngineError(
                f"{method} {target.split('?')[0]} failed ({status}): {_message(data)}"
            )
        return status, data

    def _json(self, data: bytes) -> Any:
        try:
            return json.loads(data)
        except ValueError as error:
            raise EngineError("Docker sent a reply that is not JSON") from error

    def _object(self, data: bytes) -> dict[str, Any]:
        """A reply that must be a JSON object; anything else is an `EngineError`."""
        decoded = self._json(data)
        if not isinstance(decoded, dict):
            raise EngineError("Docker sent a reply that is not a JSON object")
        return decoded

    def create(self, name: str, config: dict[str, Any]) -> str:
        """Create a container called `name` from `config` and return its id."""
        _, data = self._request(
            "POST", f"/containers/create?{urlencode({'name': name}, quote_via=quote)}", config
        )
        container_id = self._object(data).get("Id")
        if not isinstance(container_id, str):
            raise EngineError("Docker did not return a container id")
        return container_id

    def start(self, container: str) -> None:
        """Start a container; one that is already running counts as started."""
        self._request("POST", f"/containers/{_seg(container)}/start", ok=frozenset({304}))

    def wait(self, container: str, timeout: float) -> int:
        """Block until the container exits and return its exit code.

        If it is still running after `timeout` seconds it is killed and `TimeoutError` is raised.
        """
        try:
            _, data = self._request("POST", f"/containers/{_seg(container)}/wait", timeout=timeout)
        except TimeoutError:
            # The container may have exited just now, or the daemon may not answer the kill;
            # the timeout is the news either way.
            with contextlib.suppress(EngineError, OSError):
                self._request("POST", f"/containers/{_seg(container)}/kill")
            raise TimeoutError(f"container {container} still running after {timeout}s") from None
        code = self._object(data).get("StatusCode")
        if not isinstance(code, int):
            raise EngineError("Docker did not return an exit code")
        return code

    def logs(self, container: str, tail: int = 200) -> str:
        """Return the last `tail` lines of the container's output, stdout and stderr together."""
        query = urlencode({"stdout": 1, "stderr": 1, "tail": tail})
        _, data = self._request("GET", f"/containers/{_seg(container)}/logs?{query}")
        return _demultiplex(data)

    def address(self, container: str, network: str) -> str:
        """Return the container's IP address on `network`."""
        _, data = self._request("GET", f"/containers/{_seg(container)}/json")
        address = _field(self._object(data), "NetworkSettings", "Networks", network, "IPAddress")
        if not address or not isinstance(address, str):
            raise EngineError(f"container {container} has no address on network {network}")
        return address

    def has_stopped(self, container: str) -> bool:
        """Whether the container has run and stopped."""
        _, data = self._request("GET", f"/containers/{_seg(container)}/json")
        running = _field(self._object(data), "State", "Running")
        if not isinstance(running, bool):
            raise EngineError(f"Docker did not say whether container {container} is running")
        return not running

    def remove(self, container: str) -> None:
        """Remove a container by force; one that is already gone counts as removed."""
        self._request("DELETE", f"/containers/{_seg(container)}?force=1", ok=frozenset({404}))

    def create_network(self, config: Mapping[str, Any]) -> None:
        """Create a network from `config`, which `spec.network_config` makes; an existing one
        of that name is an error."""
        self._request("POST", "/networks/create", config)

    def connect(self, network: str, container: str) -> None:
        """Attach a container to a network."""
        self._request("POST", f"/networks/{_seg(network)}/connect", {"Container": container})

    def disconnect(self, network: str, container: str) -> None:
        """Detach a container from a network; one already detached or gone counts as done.

        dockerd answers 500 for a container that is not on the network, and for a network
        that has gone, so that answer counts as done too, by its message alone.
        """
        status, data = self._request(
            "POST",
            f"/networks/{_seg(network)}/disconnect",
            {"Container": container, "Force": True},
            ok=frozenset({404, 500}),
        )
        if status == 500 and not _NOT_CONNECTED.search(_message(data)):
            raise EngineError(f"POST /networks/{network}/disconnect failed (500): {_message(data)}")

    def remove_network(self, name: str) -> None:
        """Remove a network; one that is already gone counts as removed."""
        self._request("DELETE", f"/networks/{_seg(name)}", ok=frozenset({404}))

    def containers(self, label: str) -> list[str]:
        """Return the ids of all containers, running or not, carrying `label`."""
        query = urlencode({"all": 1, "filters": json.dumps({"label": [label]})}, quote_via=quote)
        _, data = self._request("GET", f"/containers/json?{query}")
        items = self._json(data)
        if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("Id"), str) for item in items
        ):
            raise EngineError("Docker sent a list of containers of the wrong shape")
        return [item["Id"] for item in items]

    def labelled(self, key: str) -> list[tuple[str, str]]:
        """Return (id, the label's value) for every container, running or not, carrying the
        label `key` with any value."""
        query = urlencode({"all": 1, "filters": json.dumps({"label": [key]})}, quote_via=quote)
        _, data = self._request("GET", f"/containers/json?{query}")
        return _labelled(self._json(data), "Id", key, "containers")

    def labelled_networks(self, key: str) -> list[tuple[str, str]]:
        """Return (name, the label's value) for every network carrying the label `key`."""
        query = urlencode({"filters": json.dumps({"label": [key]})}, quote_via=quote)
        _, data = self._request("GET", f"/networks?{query}")
        return _labelled(self._json(data), "Name", key, "networks")


def _labelled(items: Any, name: str, key: str, what: str) -> list[tuple[str, str]]:
    """(name, label value) from a list of Docker's objects, each with `name` and `Labels`."""
    if not isinstance(items, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get(name), str)
        and isinstance(item.get("Labels"), dict)
        for item in items
    ):
        raise EngineError(f"Docker sent a list of {what} of the wrong shape")
    return [(item[name], str(item["Labels"].get(key, ""))) for item in items]
