"""A fake Docker Engine on a Unix socket, for testing the client without a daemon.

It records every request and answers from a table of canned responses keyed by method and path
(the query string is ignored when matching, but kept in the record).
"""

import json
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Reply:
    """One canned response. A `gate` makes the handler wait until it is set before answering."""

    status: int = 200
    body: bytes = b""
    gate: threading.Event | None = None


@dataclass(frozen=True, slots=True)
class Recorded:
    method: str
    target: str  # path and query string, as sent
    body: bytes

    @property
    def path(self) -> str:
        return urlsplit(self.target).path

    @property
    def query(self) -> str:
        return urlsplit(self.target).query

    def json(self) -> Any:
        return json.loads(self.body)


def json_reply(status: int, payload: Any) -> Reply:
    return Reply(status, json.dumps(payload).encode())


def frame(stream: int, payload: bytes) -> bytes:
    """One frame of Docker's multiplexed log stream."""
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


@dataclass
class FakeDocker:
    socket_path: Path
    routes: dict[tuple[str, str], Reply] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)

    def on(self, method: str, path: str, reply: Reply) -> None:
        self.routes[(method, path)] = reply

    def seen(self, method: str, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path == path]


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    address_family = 1  # AF_UNIX
    daemon_threads = True
    fake: FakeDocker

    def server_bind(self) -> None:
        # HTTPServer.server_bind unpacks a (host, port) pair; a Unix socket has neither.
        socketserver.TCPServer.server_bind(self)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Stay quiet: a client that gives up (the timeout test) leaves a broken pipe behind."""

    def get_request(self) -> tuple[Any, Any]:
        request, _ = super().get_request()
        return request, ("fake", 0)


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        """Stay quiet."""

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        fake = self.server.fake
        record = Recorded(self.command, self.path, body)
        fake.requests.append(record)
        reply = fake.routes.get(
            (record.method, record.path), Reply(404, b'{"message": "no route"}')
        )
        if reply.gate is not None:
            reply.gate.wait(timeout=10)
        self.send_response(reply.status)
        self.send_header("Content-Length", str(len(reply.body)))
        self.end_headers()
        self.wfile.write(reply.body)

    do_GET = do_POST = do_DELETE = _handle


@contextmanager
def serve(socket_path: Path) -> Iterator[FakeDocker]:
    fake = FakeDocker(socket_path)
    # A Unix socket address is a path, not the (host, port) pair the stubs expect.
    server = _Server(str(socket_path), _Handler)  # type: ignore[arg-type]
    server.fake = fake
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
