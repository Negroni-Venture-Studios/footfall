"""The egress proxy: plain HTTP to port 80 and CONNECT to port 443, for allowed hosts only.

Every byte a client sends comes from code under test, so the proxy reads requests strictly and
refuses anything it cannot read one way only. A request head is at most `MAX_HEAD` bytes, every
line ends with CRLF, header lines are never folded, and a request carrying Transfer-Encoding, or
more than one Content-Length, is refused, so that no request can be read two ways (smuggling).

A host must be a name, never an address, and must be on the allow list the runner gave the
client's address; it must also resolve to public addresses only, and the proxy connects to the
address it checked rather than looking the name up again. A client address with no allow list
may be a container that has just started, so its request waits up to `unknown_wait` seconds for
the runner's allow before it is refused. Every other refusal is immediate.

No allowed name may become a route to another site on the same server (domain fronting):

- Plain HTTP goes only to port 80. The proxy sends exactly one request upstream, in origin form,
  with the Host header set to the checked name, the client's Host and Proxy-* headers dropped,
  the body cut to its Content-Length, and `Connection: close`. It relays the response and
  closes.
- CONNECT goes only to port 443. After replying 200, the proxy reads the client's first TLS
  record before it connects upstream, and closes the connection unless the ClientHello in it
  names the CONNECT host and does not hide its real name with Encrypted Client Hello. It sends
  that record upstream first, then follows the records both ways until the server's
  ServerHello settles the handshake, holding any further ClientHello to the same checks, and
  then relays the rest untouched. `footfall.proxy.tls` says what this cannot stop.

The proxy listens only where the runner tells it: on its own address on each run's network,
for as long as that run lasts, so that no container on any other network can reach it. A
connection idle for `idle_timeout` seconds in both directions is closed, and connections are
capped in all and per client address. The proxy logs one line per decision, with the client's
address, the run, the host, the port and the outcome. It never logs a path, a query string, a
header value or a body.

Names are looked up on a pool of threads the proxy owns, not on asyncio's, which the process
shares. A lookup's thread cannot be stopped, and one whose name server is slow or silent holds
it for as long as the system resolver waits, so a client's lookups in flight are bounded, and
so are its run's. A lookup counts from before it is queued until its thread returns, even when
the request that asked for it has given up waiting, and a request over a bound is refused with
503. Requests from one client for a name already being looked up join that lookup, so that a
burst of connections to one host costs one thread.
"""

import asyncio
import contextlib
import functools
import http
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final

from footfall.hosts import HostError, pooled_resolver, resolve_public, validate_host_name
from footfall.proxy.policy import (
    INVALID_HOST,
    Decision,
    Policy,
    canonical_address,
    check_run_id,
)
from footfall.proxy.tls import (
    HANDSHAKE,
    RECORD_HEADER,
    EncryptedClientHello,
    HandshakeError,
    ServerHellos,
    record_header,
    server_name,
)

__all__ = [
    "DEFAULT_PORT",
    "ESTABLISHED",
    "MAX_HEAD",
    "MAX_HEADERS",
    "Opener",
    "ProxyServer",
    "Resolve",
]

logger = logging.getLogger(__name__)

type Resolve = Callable[[str, int], Awaitable[str]]
type Opener = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

DEFAULT_PORT: Final = 3128
MAX_HEAD: Final = 16 * 1024
MAX_HEADERS: Final = 100
ESTABLISHED: Final = b"HTTP/1.1 200 Connection established\r\n\r\n"

_HTTP_PORT: Final = 80
_TLS_PORT: Final = 443
_CHUNK: Final = 16 * 1024
_LINGER: Final = 1.0
_LINGER_BYTES: Final = 64 * 1024
_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"})
_VERSIONS: Final = frozenset({"HTTP/1.0", "HTTP/1.1"})
_BODILESS: Final = frozenset({"GET", "HEAD"})
_TOKEN: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_TARGET: Final = re.compile(r"[\x21-\x7e]+")
_HOST_CHARACTERS: Final = re.compile(r"[A-Za-z0-9.-]+")
_PORT: Final = re.compile(r"[0-9]{1,5}")
_LENGTH: Final = re.compile(r"[0-9]{1,15}")
_CONTROL: Final = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_TOO_LARGE: Final = f"request headers are larger than {MAX_HEAD // 1024} KiB"
# Never passed upstream: hop-by-hop headers, the client's Host, and Content-Length, which the
# proxy writes itself from the length it checked.
_DROPPED: Final = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class _Refusal(Exception):
    """A request the proxy refuses, with the status and one-line reason to send back."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    host: str  # checked, in lower case
    port: int
    target: str  # origin form; empty for CONNECT
    version: str
    headers: tuple[tuple[str, str], ...]
    length: int | None  # the Content-Length, if the request has one


# ---------------------------------------------------------------- reading a request


def _host(text: str) -> str:
    """A host name from a request, checked; anything else, an address included, is refused."""
    if not _HOST_CHARACTERS.fullmatch(text):
        raise _Refusal(403, INVALID_HOST)
    try:
        return validate_host_name(text)
    except HostError:
        raise _Refusal(403, INVALID_HOST) from None


def _port(text: str) -> int:
    if not _PORT.fullmatch(text) or int(text) > 65535:
        raise _Refusal(400, "malformed port")
    return int(text)


def _headers(lines: list[str]) -> tuple[tuple[str, str], ...]:
    if len(lines) > MAX_HEADERS:
        raise _Refusal(431, f"more than {MAX_HEADERS} header lines")
    headers: list[tuple[str, str]] = []
    for line in lines:
        if line[:1] in (" ", "\t"):
            raise _Refusal(400, "folded header lines are not allowed")
        name, colon, value = line.partition(":")
        if not colon or not _TOKEN.fullmatch(name):
            raise _Refusal(400, "malformed header line")
        value = value.strip(" \t")
        if _CONTROL.search(value):
            raise _Refusal(400, "control character in a header value")
        headers.append((name, value))
    return tuple(headers)


def _canonical(name: str) -> str:
    """A header name as a server might read it: in lower case, with `_` as `-`, as CGI-style
    servers read it. Every check of a header name uses this form."""
    return name.lower().replace("_", "-")


def _content_length(method: str, headers: Iterable[tuple[str, str]]) -> int | None:
    """The request's Content-Length, refusing any framing that could be read two ways.

    A GET or HEAD with a body is refused too: a server that ignores such a body would read it
    as the next request (a CL.0 desync).
    """
    names = [_canonical(name) for name, _ in headers]
    lengths = [value for name, value in headers if _canonical(name) == "content-length"]
    if method in _BODILESS and ("transfer-encoding" in names or any(v != "0" for v in lengths)):
        raise _Refusal(400, f"a {method} request must not carry a body")
    if "transfer-encoding" in names:
        if lengths:
            raise _Refusal(
                400, "a request must not carry both Content-Length and Transfer-Encoding"
            )
        raise _Refusal(411, "Transfer-Encoding is not supported; send Content-Length")
    if not lengths:
        return None
    if len(lengths) > 1:
        raise _Refusal(400, "more than one Content-Length")
    if not _LENGTH.fullmatch(lengths[0]):
        raise _Refusal(400, "Content-Length must be a whole number")
    return int(lengths[0])


def _authority(target: str) -> tuple[str, int]:
    """The host and port of a CONNECT request's `host:port`."""
    host, colon, port = target.rpartition(":")
    if not colon:
        raise _Refusal(400, "CONNECT needs host:port")
    name = _host(host)
    return name, _port(port)


def _absolute(target: str) -> tuple[str, int, str]:
    """The host, port and origin form of an absolute `http://` request target."""
    if not _TARGET.fullmatch(target):
        raise _Refusal(400, "malformed request target")
    if target[:7].lower() != "http://":
        raise _Refusal(400, "only absolute http:// addresses are proxied; use CONNECT for https")
    rest = target[7:]
    if "#" in rest:
        raise _Refusal(400, "a request target must not carry a fragment")
    ends = [index for index in (rest.find("/"), rest.find("?")) if index != -1]
    end = min(ends, default=len(rest))
    authority, origin = rest[:end], rest[end:]
    if "@" in authority:
        raise _Refusal(400, "user information in the address is not allowed")
    if "[" in authority or "]" in authority:
        raise _Refusal(403, INVALID_HOST)
    host, colon, port = authority.partition(":")
    number = _port(port) if colon else _HTTP_PORT
    name = _host(host)
    # One leading slash: `//host/path` would read as another host to code that resolves the
    # path against a base address.
    origin = "/" + origin.lstrip("/")
    return name, number, origin


def _parse(head: bytes) -> _Request:
    """Read a request head, which ends with an empty line, or raise _Refusal."""
    if len(head) > MAX_HEAD:
        raise _Refusal(431, _TOO_LARGE)
    lines = head.decode("latin-1").removesuffix("\r\n\r\n").split("\r\n")
    if any("\r" in line or "\n" in line for line in lines):
        raise _Refusal(400, "every line must end with CRLF")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not all(parts):
        raise _Refusal(400, "malformed request line")
    method, target, version = parts
    if method not in _METHODS:
        raise _Refusal(405, "method not allowed")
    if version not in _VERSIONS:
        raise _Refusal(400, "only HTTP/1.0 and HTTP/1.1 are spoken")
    headers = _headers(lines[1:])
    length = _content_length(method, headers)
    if method == "CONNECT":
        if length is not None:
            raise _Refusal(400, "CONNECT must not carry a body")
        host, port = _authority(target)
        return _Request(method, host, port, "", version, headers, None)
    host, port, origin = _absolute(target)
    return _Request(method, host, port, origin, version, headers, length)


def _upstream_head(request: _Request) -> bytes:
    """The one request sent upstream: origin form, the checked Host, and `Connection: close`."""
    lines = [f"{request.method} {request.target} {request.version}", f"Host: {request.host}"]
    for name, value in request.headers:
        canonical = _canonical(name)
        if canonical not in _DROPPED and not canonical.startswith("proxy-"):
            lines.append(f"{name}: {value}")
    if request.length is not None:
        lines.append(f"Content-Length: {request.length}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _response(status: int, reason: str) -> bytes:
    body = f"footfall egress proxy: {reason}\n".encode()
    phrase = http.HTTPStatus(status).phrase
    head = (
        f"HTTP/1.1 {status} {phrase}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + body


def _log(client: str, run_id: str | None, host: str, port: int | None, outcome: str) -> None:
    """One line per decision. `host` is a checked name or "-", never a client's raw text."""
    port_text = "-" if port is None else str(port)
    logger.info("%s run=%s host=%s port=%s %s", client, run_id or "-", host, port_text, outcome)


# ---------------------------------------------------------------- moving bytes


class _Activity:
    """When a connection last moved a byte in either direction, and waits bounded by that.

    A tunnel that streams in one direction only is not idle, so each wait is cut short only
    once neither direction has moved for the idle timeout.
    """

    def __init__(self, idle: float) -> None:
        self._idle = idle
        self._loop = asyncio.get_running_loop()
        self._last = self._loop.time()

    def touch(self) -> None:
        self._last = self._loop.time()

    async def within[T](self, step: Callable[[], Awaitable[T]]) -> T:
        while True:
            remaining = self._last + self._idle - self._loop.time()
            if remaining <= 0:
                raise TimeoutError("the connection was idle for too long")
            scope = asyncio.timeout(remaining)
            try:
                async with scope:
                    return await step()
            except TimeoutError:
                if not scope.expired():
                    raise
                # The other direction may have moved meanwhile, so look again.


async def _pump(
    source: asyncio.StreamReader,
    sink: asyncio.StreamWriter,
    activity: _Activity,
    limit: int | None = None,
) -> None:
    """Copy bytes until the source ends, or until `limit` bytes if one is given.

    When the source ends, the sink is half-closed, so the other side learns that nothing more
    is coming and can still answer.
    """
    remaining = limit
    while remaining is None or remaining > 0:
        size = _CHUNK if remaining is None else min(remaining, _CHUNK)
        data = await activity.within(functools.partial(source.read, size))
        if not data:
            _pass_end(sink)
            return
        activity.touch()
        if remaining is not None:
            remaining -= len(data)
        sink.write(data)
        await activity.within(sink.drain)
        activity.touch()


def _pass_end(sink: asyncio.StreamWriter) -> None:
    """Half-close `sink`, as its source did."""
    if sink.can_write_eof() and not sink.is_closing():
        sink.write_eof()


class _TunnelRefused(Exception):
    """A tunnel closed for something it carried; the message is the reason to log."""


# How a relay is expected to end, besides finishing: cancelled, a peer gone or quiet, or a
# refusal that the tunnel logs itself.
_EXPECTED_ENDS: Final = (
    asyncio.CancelledError,
    OSError,
    TimeoutError,
    asyncio.IncompleteReadError,
    _TunnelRefused,
)


async def _finish(client: str, tasks: Iterable[asyncio.Task[None]]) -> list[object]:
    """Cancel tasks and collect their outcomes, so that none is left unretrieved.

    An outcome nobody expected is a bug in the proxy, so it is logged with its traceback, as
    on the path that serves requests; the bytes relayed never are.
    """
    tasks = list(tasks)
    for task in tasks:
        task.cancel()
    outcomes: list[object] = await asyncio.gather(*tasks, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, _EXPECTED_ENDS):
            logger.error("the proxy failed unexpectedly relaying for %s", client, exc_info=outcome)
    return outcomes


def _hello_refusal(record: bytes, host: str) -> str | None:
    """Why a ClientHello record may not pass for the CONNECT host `host`; None if it may."""
    try:
        name = server_name(record)
    except EncryptedClientHello as error:
        return str(error)
    except HandshakeError as error:
        return f"not a TLS ClientHello the proxy can read ({error})"
    if name is None:
        return "the TLS ClientHello names no server"
    if name.lower() != host:
        return "the TLS server name is not the CONNECT host"
    return None


async def _read_record(
    reader: asyncio.StreamReader, activity: _Activity, side: str
) -> tuple[int, bytes] | None:
    """One whole TLS record, with its content type, or None if the stream ended cleanly.

    Raises _TunnelRefused for anything that is not a record the proxy can read.
    """
    stopped = f"the {side} stopped inside a TLS record"
    try:
        header = await activity.within(functools.partial(reader.readexactly, RECORD_HEADER))
    except asyncio.IncompleteReadError as error:
        if error.partial:
            raise _TunnelRefused(stopped) from None
        return None  # the end, between records
    try:
        content_type, length = record_header(header)
    except HandshakeError as error:
        raise _TunnelRefused(f"the {side} sent a record the proxy cannot read ({error})") from None
    try:
        payload = await activity.within(functools.partial(reader.readexactly, length))
    except asyncio.IncompleteReadError:
        raise _TunnelRefused(stopped) from None
    activity.touch()
    return content_type, header + payload


async def _check_client(
    reader: asyncio.StreamReader,
    sink: asyncio.StreamWriter,
    host: str,
    hellos: ServerHellos,
    activity: _Activity,
) -> None:
    """Relay the client's records, holding each ClientHello to the checks of the first, until
    the server's ServerHello settles the handshake; then relay the rest untouched."""
    while not hellos.settled:
        read = await _read_record(reader, activity, "client")
        if read is None:
            _pass_end(sink)
            return
        content_type, record = read
        if content_type == HANDSHAKE and not hellos.settled:
            reason = _hello_refusal(record, host)
            if reason is not None:
                raise _TunnelRefused(reason)
        sink.write(record)
        await activity.within(sink.drain)
        activity.touch()
    await _pump(reader, sink, activity)


async def _follow_server(
    reader: asyncio.StreamReader,
    sink: asyncio.StreamWriter,
    hellos: ServerHellos,
    activity: _Activity,
) -> None:
    """Relay the server's records, following its handshake until a ServerHello settles it;
    then relay the rest untouched. Each record is judged before it is relayed, so the client
    cannot answer a ServerHello the proxy has not yet seen."""
    while not hellos.settled:
        read = await _read_record(reader, activity, "server")
        if read is None:
            _pass_end(sink)
            return
        content_type, record = read
        if content_type == HANDSHAKE:
            try:
                hellos.feed(record[RECORD_HEADER:])
            except HandshakeError as error:
                raise _TunnelRefused(
                    f"the server's handshake cannot be followed ({error})"
                ) from None
        sink.write(record)
        await activity.within(sink.drain)
        activity.touch()
    await _pump(reader, sink, activity)


async def _linger(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Half-close, then read and drop what the client still sends, briefly and boundedly.

    Closing a socket that holds unread bytes resets the connection, which can destroy the
    reply before the client reads it.
    """
    with contextlib.suppress(OSError, RuntimeError, TimeoutError):
        if writer.can_write_eof() and not writer.is_closing():
            writer.write_eof()
        async with asyncio.timeout(_LINGER):
            left = _LINGER_BYTES
            while left > 0:
                chunk = await reader.read(min(left, _CHUNK))
                if not chunk:
                    return
                left -= len(chunk)


async def _open_upstream(
    address: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port, limit=_CHUNK)


# ---------------------------------------------------------------- the server


def _add(counts: dict[str, int], key: str, change: int) -> None:
    """Change a count, keeping only counts above zero."""
    count = counts.get(key, 0) + change
    if count > 0:
        counts[key] = count
    else:
        counts.pop(key, None)


class ProxyServer:
    """Serves the egress proxy for the allow lists held in `policy`.

    `resolve` turns a checked name into the one public address to connect to; it raises
    HostError for a name that does not resolve to public addresses only. By default it is
    `resolve_public` on the system resolver, run on a pool of `lookup_threads` threads that the
    proxy owns. `open_upstream` connects to an address and port. Tests replace both, so that
    they stay on loopback. `port` is the port each run's listener opens, on the address
    `listen` is given.

    The lookup bounds: a client may have `max_lookups_per_client` names being looked up at
    once, and all of a run's clients `max_lookups_per_run`. An allow list names a few hosts,
    and requests for a name already being looked up join that lookup, so 8 is well above what
    an app or a browser loading a page needs. 16 per run leaves most of the pool of 64 to other
    runs even when one run's every lookup is stuck; a thread blocked in a lookup costs little.
    """

    def __init__(
        self,
        policy: Policy,
        resolve: Resolve | None = None,
        *,
        port: int = DEFAULT_PORT,
        idle_timeout: float = 60.0,
        unknown_wait: float = 3.0,
        unknown_poll: float = 0.1,
        max_connections: int = 512,
        max_per_client: int = 128,
        max_lookups_per_client: int = 8,
        max_lookups_per_run: int = 16,
        lookup_threads: int = 64,
        open_upstream: Opener = _open_upstream,
    ) -> None:
        if idle_timeout <= 0 or unknown_poll <= 0 or unknown_wait < 0:
            raise ValueError(
                "idle_timeout and unknown_poll must be positive, unknown_wait not less"
            )
        if max_connections < 1 or max_per_client < 1:
            raise ValueError("max_connections and max_per_client must be at least 1")
        if max_lookups_per_client < 1 or max_lookups_per_run < 1 or lookup_threads < 1:
            raise ValueError("the lookup bounds and lookup_threads must be at least 1")
        if not 0 <= port <= 65535:
            raise ValueError("port must be from 0 to 65535")
        self._policy = policy
        self._port = port
        self._listeners: dict[str, tuple[str, asyncio.Server]] = {}  # address -> run, server
        self._listening = asyncio.Lock()
        self._lookup_pool: ThreadPoolExecutor | None = None
        if resolve is None:
            self._lookup_pool = ThreadPoolExecutor(
                lookup_threads, thread_name_prefix="footfall-lookup"
            )
            resolve = functools.partial(resolve_public, resolver=pooled_resolver(self._lookup_pool))
        self._resolve = resolve
        self._max_lookups_per_client = max_lookups_per_client
        self._max_lookups_per_run = max_lookups_per_run
        # Lookups in flight, by client, name and port, and how many each client and run holds.
        self._lookups: dict[tuple[str, str, int], asyncio.Task[str]] = {}
        self._lookups_by_client: dict[str, int] = {}
        self._lookups_by_run: dict[str, int] = {}
        self._idle = idle_timeout
        self._unknown_wait = unknown_wait
        self._unknown_poll = unknown_poll
        self._max_connections = max_connections
        self._max_per_client = max_per_client
        self._open_upstream = open_upstream
        self._open = 0
        self._per_client: dict[str, int] = {}

    @property
    def policy(self) -> Policy:
        """The allow lists this proxy serves."""
        return self._policy

    @property
    def connections(self) -> int:
        """How many client connections are being served."""
        return self._open

    async def start(self, host: str, port: int) -> asyncio.Server:
        """Listen on `host` and `port`; the caller serves and closes the server it returns.

        Tests use it to listen on loopback. The proxy itself listens through `listen` alone.
        """
        return await asyncio.start_server(self._handle, host, port, limit=MAX_HEAD)

    # ------------------------------------------------------------ listeners

    async def listen(self, ip: str, run_id: str) -> None:
        """Accept connections at `ip`, on the proxy's port, for the run `run_id`.

        `ip` is the proxy's own address on the run's network, so that only that network's
        containers can reach the listener. Listening again where the same run listens is
        harmless. Raises ValueError, opening nothing, for a bad address or run id, for an
        address that means every interface, for an address another run listens on, and for an
        address the proxy cannot listen on, with the operating system's reason.
        """
        address = canonical_address(ip)
        check_run_id(run_id)
        parsed = ipaddress.ip_address(address)
        if parsed.is_unspecified:
            raise ValueError(f"{address} means every interface; name the run's own address")
        async with self._listening:
            held = self._listeners.get(address)
            if held is not None:
                if held[0] != run_id:
                    raise ValueError(f"another run already listens on {address}")
                return
            # Bound here rather than by asyncio, which drops the system's reason for an address
            # this host does not have.
            family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, self._port))
                server = await asyncio.start_server(self._handle, sock=sock, limit=MAX_HEAD)
            except OSError as error:
                sock.close()
                reason = error.strerror or str(error)
                raise ValueError(
                    f"could not listen on {address} port {self._port}: {reason}"
                ) from None
            self._listeners[address] = (run_id, server)

    async def close_run(self, run_id: str) -> int:
        """Stop listening wherever a run listens, and return how many listeners closed."""
        async with self._listening:
            addresses = [key for key, (owner, _) in self._listeners.items() if owner == run_id]
            for address in addresses:
                _, server = self._listeners.pop(address)
                server.close()  # frees the address at once; connections are left to their run
        return len(addresses)

    async def close(self) -> None:
        """Stop listening everywhere, as the proxy shuts down."""
        async with self._listening:
            for _, server in self._listeners.values():
                server.close()
            self._listeners.clear()
        if self._lookup_pool is not None:
            # Threads blocked in a lookup cannot be stopped; they end when the resolver gives up.
            self._lookup_pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ connections

    def _admit(self, client: str) -> bool:
        count = self._per_client.get(client, 0)
        if self._open >= self._max_connections or count >= self._max_per_client:
            return False
        self._open += 1
        self._per_client[client] = count + 1
        return True

    def _release(self, client: str) -> None:
        self._open -= 1
        count = self._per_client[client] - 1
        if count:
            self._per_client[client] = count
        else:
            del self._per_client[client]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = str(peer[0]) if isinstance(peer, tuple) and peer else ""
        if not self._admit(client):
            # Closed without reading or lingering, so that a flood holds nothing open.
            _log(client, None, "-", None, "refused (503): too many connections")
            writer.write(_response(503, "too many connections"))
            writer.close()
            return
        try:
            await self._serve(client, reader, writer)
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass  # the client or the upstream went away, or went quiet
        except Exception:
            # Nothing from the request is logged: it may carry a secret in a header or body.
            logger.exception("the proxy failed unexpectedly serving %s", client)
        finally:
            try:
                task = asyncio.current_task()
                if task is None or not task.cancelling():
                    await _linger(reader, writer)
            finally:
                writer.close()
                self._release(client)

    async def _serve(
        self, client: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        run_id: str | None = None
        host, port = "-", None
        try:
            try:
                async with asyncio.timeout(self._idle):
                    head = await reader.readuntil(b"\r\n\r\n")
            except asyncio.LimitOverrunError:
                raise _Refusal(431, _TOO_LARGE) from None
            except asyncio.IncompleteReadError:
                return  # the client left before it finished a request
            except TimeoutError:
                raise _Refusal(408, "no request arrived in time") from None
            request = _parse(head)
            host, port = request.host, request.port
            if request.method == "CONNECT" and request.port != _TLS_PORT:
                raise _Refusal(403, "CONNECT is allowed only to port 443")
            if request.method != "CONNECT" and request.port != _HTTP_PORT:
                raise _Refusal(403, "plain HTTP is allowed only to port 80")
            decision = await self._decide(client, request.host, request.port)
            run_id = decision.run_id
            if not decision.allowed or run_id is None:
                raise _Refusal(403, decision.reason)
            address = await self._resolved(client, run_id, request.host, request.port)
        except _Refusal as refusal:
            _log(client, run_id, host, port, f"refused ({refusal.status}): {refusal.reason}")
            writer.write(_response(refusal.status, refusal.reason))
            async with asyncio.timeout(self._idle):
                await writer.drain()
            return
        if request.method == "CONNECT":
            await self._tunnel(client, run_id, request, address, reader, writer)
        else:
            _log(client, run_id, host, port, "allowed")
            await self._forward(client, run_id, request, address, reader, writer)

    async def _decide(self, client: str, host: str, port: int) -> Decision:
        """The policy's decision, waiting a little for the allow of a client not yet known."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._unknown_wait
        decision = self._policy.decide(client, host, port)
        while decision.run_id is None and loop.time() < deadline:
            await asyncio.sleep(min(self._unknown_poll, deadline - loop.time()))
            decision = self._policy.decide(client, host, port)
        return decision

    async def _resolved(self, client: str, run_id: str, host: str, port: int) -> str:
        lookup = self._lookup(client, run_id, host, port)
        try:
            async with asyncio.timeout(self._idle):
                # Shielded, so that giving up the wait leaves the lookup counted until it ends.
                return await asyncio.shield(lookup)
        except HostError:
            raise _Refusal(403, f"{host} does not resolve to public addresses only") from None
        except TimeoutError:
            raise _Refusal(504, f"{host} did not resolve in time") from None

    def _lookup(self, client: str, run_id: str, host: str, port: int) -> asyncio.Task[str]:
        """The client's lookup of `host` in flight, or a new one if the bounds allow it.

        A new lookup is counted against the client and its run before it is queued, and the
        count is given back only when the lookup ends, which for the system resolver is when its
        thread returns.
        """
        key = (client, host, port)
        lookup = self._lookups.get(key)
        if lookup is not None:
            return lookup
        if self._lookups_by_client.get(client, 0) >= self._max_lookups_per_client:
            raise _Refusal(503, "too many name lookups in flight for this client")
        if self._lookups_by_run.get(run_id, 0) >= self._max_lookups_per_run:
            raise _Refusal(503, "too many name lookups in flight for this run")
        _add(self._lookups_by_client, client, 1)
        _add(self._lookups_by_run, run_id, 1)
        lookup = asyncio.create_task(self._look_up(host, port))
        self._lookups[key] = lookup
        lookup.add_done_callback(functools.partial(self._looked_up, key, run_id))
        return lookup

    async def _look_up(self, host: str, port: int) -> str:
        return await self._resolve(host, port)

    def _looked_up(self, key: tuple[str, str, int], run_id: str, lookup: asyncio.Task[str]) -> None:
        del self._lookups[key]
        _add(self._lookups_by_client, key[0], -1)
        _add(self._lookups_by_run, run_id, -1)
        if not lookup.cancelled():
            lookup.exception()  # retrieved here, as every waiter may have given up

    async def _connect(
        self, client: str, host: str, address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            async with asyncio.timeout(self._idle):
                return await self._open_upstream(address, port)
        except (OSError, TimeoutError) as error:
            reason = type(error).__name__
            logger.info("%s could not reach %s port %d: %s", client, host, port, reason)
            return None

    # ------------------------------------------------------------ plain HTTP

    async def _forward(
        self,
        client: str,
        run_id: str,
        request: _Request,
        address: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream = await self._connect(client, request.host, address, request.port)
        if upstream is None:
            writer.write(_response(502, f"could not reach {request.host}"))
            async with asyncio.timeout(self._idle):
                await writer.drain()
            return
        self._policy.record(run_id, request.host)
        upstream_reader, upstream_writer = upstream
        try:
            activity = _Activity(self._idle)
            upstream_writer.write(_upstream_head(request))
            await activity.within(upstream_writer.drain)
            # The body goes up while the response comes down, so that a server that answers
            # `Expect: 100-continue`, or answers early, is relayed at once. Nothing the client
            # sends after its Content-Length is ever read.
            upload = asyncio.create_task(
                _pump(reader, upstream_writer, activity, request.length or 0)
            )
            try:
                await _pump(upstream_reader, writer, activity)
            finally:
                await _finish(client, [upload])
        finally:
            upstream_writer.close()

    # ------------------------------------------------------------ CONNECT

    async def _tunnel(
        self,
        client: str,
        run_id: str,
        request: _Request,
        address: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.write(ESTABLISHED)
        try:
            async with asyncio.timeout(self._idle):
                await writer.drain()
                header = await reader.readexactly(RECORD_HEADER)
                _, length = record_header(header)
                record = header + await reader.readexactly(length)
            refusal = _hello_refusal(record, request.host)
        except HandshakeError as error:
            refusal = f"not a TLS ClientHello the proxy can read ({error})"
        except (asyncio.IncompleteReadError, TimeoutError):
            refusal = "no TLS ClientHello arrived"
        if refusal is not None:
            _log(client, run_id, request.host, request.port, f"refused: {refusal}")
            return
        _log(client, run_id, request.host, request.port, "allowed")
        upstream = await self._connect(client, request.host, address, request.port)
        if upstream is None:
            return
        self._policy.record(run_id, request.host)
        upstream_reader, upstream_writer = upstream
        try:
            activity = _Activity(self._idle)
            upstream_writer.write(record)
            await activity.within(upstream_writer.drain)
            hellos = ServerHellos()
            tasks = [
                asyncio.create_task(
                    _check_client(reader, upstream_writer, request.host, hellos, activity)
                ),
                asyncio.create_task(_follow_server(upstream_reader, writer, hellos, activity)),
            ]
            # A side that ends cleanly has half-closed the other, which may still answer, so the
            # tunnel lasts until both have ended. A refusal, a failure or the idle timeout ends
            # both at once.
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            finally:
                outcomes = await _finish(client, tasks)
            for outcome in outcomes:
                if isinstance(outcome, _TunnelRefused):
                    _log(client, run_id, request.host, request.port, f"refused: {outcome}")
        finally:
            upstream_writer.close()
