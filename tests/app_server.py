"""A small web server on 127.0.0.1 for the browser tests.

It serves the fixture pages in `tests/fixtures/pages/`, and any page, reply or redirect a
test adds, and it records every request it is sent. A test runs two of them: the app, and
a second server that stands in for every other host, so that a test can prove a request
never arrived as well as that the guard said it was blocked.
"""

import base64
import hashlib
import mimetypes
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

PAGES = Path(__file__).parent / "fixtures" / "pages"
_FILE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass(frozen=True)
class Hit:
    """One request the server was sent."""

    method: str
    host: str
    path: str
    upgrade: bool = False


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0  # seconds to wait before answering, as a slow app would


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output quiet."""

    def _record(self) -> None:
        upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
        hit = Hit(self.command, self.headers.get("Host", ""), self.path, upgrade)
        with self.server.lock:
            self.server.hits.append(hit)

    def _answer(self) -> None:
        self._record()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 1_000_000))
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._accept_websocket()
            return
        path = urlsplit(self.path).path
        with self.server.lock:
            reply = self.server.routes.get(path)
        if reply is None:
            reply = self._file(path)
        if reply.delay:
            time.sleep(reply.delay)
        self.send_response(reply.status)
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(reply.body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(reply.body)

    def _file(self, path: str) -> Reply:
        name = path.lstrip("/")
        file = self.server.root / name
        if not _FILE_NAME.fullmatch(name) or not file.is_file():
            return Reply(404, b"not found", {"Content-Type": "text/plain"})
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if kind.startswith("text/"):
            kind += "; charset=utf-8"
        return Reply(200, file.read_bytes(), {"Content-Type": kind})

    def _accept_websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode(), usedforsecurity=False).digest()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(digest).decode())
        self.end_headers()
        self.close_connection = True

    do_GET = _answer
    do_POST = _answer
    do_HEAD = _answer


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    root: Path
    lock: threading.Lock
    hits: list[Hit]
    routes: dict[str, Reply]


class AppServer:
    """A threaded HTTP server on 127.0.0.1 and a free port, for one test."""

    def __init__(self, root: Path = PAGES) -> None:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.root = root
        self._server.lock = threading.Lock()
        self._server.hits = []
        self._server.routes = {}
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        port: int = self._server.server_address[1]
        return port

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    def reply(
        self,
        path: str,
        body: str | bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        delay: float = 0.0,
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        with self._server.lock:
            self._server.routes[path] = Reply(status, data, dict(headers or {}), delay)

    def page(self, path: str, html: str) -> None:
        self.reply(path, html, headers={"Content-Type": "text/html; charset=utf-8"})

    def redirect(self, path: str, location: str, status: int = 302) -> None:
        self.reply(path, status=status, headers={"Location": location})

    @property
    def hits(self) -> list[Hit]:
        with self._server.lock:
            return list(self._server.hits)

    def paths(self) -> list[str]:
        return [hit.path for hit in self.hits]
