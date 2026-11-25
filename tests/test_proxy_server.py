"""The egress proxy's HTTP and CONNECT handling, against an upstream on loopback.

The proxy's `resolve` is replaced, so test names lead to 127.0.0.1 and one name counts as
private, and its `open_upstream` sends every connection to the local upstream's port while
recording the address and port the proxy asked for. No test leaves loopback.
"""

import asyncio
import contextlib
import functools
import logging
import socket
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from footfall.hosts import HostError, resolve_public
from footfall.proxy.policy import Policy
from footfall.proxy.server import MAX_HEAD, ProxyServer
from tests.tls_hellos import (
    CHANGE_CIPHER_SPEC,
    HELLO_RETRY,
    SERVER_HELLO,
    application_data,
    hello,
    real_client_hello,
    record,
    sni,
    with_extension,
)

RUN = "r_abcdefghijklmnopqrstuvwxyz"
CLIENT = "127.0.0.1"
ALLOWED = "api.example.com"
PRIVATE = "private.example.com"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
TIMEOUT = 5.0


@dataclass
class Upstream:
    """A server on loopback that plays TLS server to a TLS record and answers anything else
    as HTTP.

    After a first TLS record it sends `greeting`, a ServerHello unless a test changes it, and
    then echoes what follows if `echo` is set. It records every byte each connection sent it,
    once that connection has ended.
    """

    response: bytes = RESPONSE
    greeting: bytes = SERVER_HELLO
    echo: bool = True
    half_close: bool = False  # after the greeting, as a server with nothing more to say
    received: list[bytes] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = bytearray()
        try:
            first = await reader.read(1)
            data += first
            if first == b"\x16":
                header = await reader.readexactly(4)
                data += header
                data += await reader.readexactly(int.from_bytes(header[2:4], "big"))
                writer.write(self.greeting)
                await writer.drain()
                if self.half_close:
                    writer.write_eof()
                while chunk := await reader.read(65536):
                    data += chunk
                    if self.echo:
                        writer.write(chunk)
                        await writer.drain()
                return
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                data += chunk
            head = bytes(data).split(b"\r\n\r\n", 1)[0].decode("latin-1").lower()
            length = 0
            for line in head.split("\r\n")[1:]:
                if line.startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            while len(data) < len(head) + 4 + length:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                data += chunk
            with contextlib.suppress(TimeoutError):  # catch anything sent after the request
                async with asyncio.timeout(0.05):
                    data += await reader.read(65536)
            writer.write(self.response)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.received.append(bytes(data))
            writer.close()

    async def wait_for(self, count: int) -> None:
        async with asyncio.timeout(TIMEOUT):
            while len(self.received) < count:
                await asyncio.sleep(0.01)


@dataclass
class Live:
    policy: Policy
    proxy: ProxyServer
    port: int
    upstream: Upstream
    connects: list[tuple[str, int]]
    resolved: list[tuple[str, int]]


type Resolve = Callable[[str, int], Awaitable[str]]


@contextlib.asynccontextmanager
async def running(
    resolve: Resolve | None = None, *, real_resolve: bool = False, **settings: Any
) -> AsyncIterator[Live]:
    """A proxy on loopback. With `real_resolve` it keeps its own resolver, so that a test can
    stub `socket.getaddrinfo` under it."""
    upstream = Upstream()
    upstream_server = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    connects: list[tuple[str, int]] = []
    resolved: list[tuple[str, int]] = []

    async def open_upstream(
        address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        connects.append((address, port))
        return await asyncio.open_connection(address, upstream_port)

    async def fake_resolve(host: str, port: int) -> str:
        resolved.append((host, port))
        if host == PRIVATE:
            raise HostError(f"{host} resolves to 10.0.0.1, which is not a public address")
        return "127.0.0.1"

    policy = Policy()
    settings.setdefault("open_upstream", open_upstream)
    if real_resolve:
        proxy = ProxyServer(policy, **settings)
    else:
        proxy = ProxyServer(policy, resolve or fake_resolve, **settings)
    server = await proxy.start("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield Live(policy, proxy, port, upstream, connects, resolved)
    finally:
        server.close()
        upstream_server.close()
        async with asyncio.timeout(TIMEOUT):
            while proxy.connections:
                await asyncio.sleep(0.01)
            await server.wait_closed()
            await upstream_server.wait_closed()
        await proxy.close()


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    async with running() as value:
        value.policy.allow(CLIENT, RUN, [ALLOWED, PRIVATE])
        yield value


async def exchange(port: int, data: bytes) -> bytes:
    """Send bytes to the proxy and return everything it sends back before it closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        async with asyncio.timeout(TIMEOUT):
            return await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@dataclass
class Tunnel:
    reply: bytes
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


@contextlib.asynccontextmanager
async def tunnel(port: int, authority: str) -> AsyncIterator[Tunnel]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    await writer.drain()
    try:
        async with asyncio.timeout(TIMEOUT):
            reply = await reader.readuntil(b"\r\n\r\n")
        value = Tunnel(reply, reader, writer)
        yield value
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def first_reply(port: int) -> bytes:
    """Connect, send nothing, and return what the proxy says before it closes.

    A connection over the caps is closed without being read, so a client that had sent a
    request might see the connection reset before it reads the reply.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        async with asyncio.timeout(TIMEOUT):
            return await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def read_to_end(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(TIMEOUT):
        return await reader.read()


def status(reply: bytes) -> int:
    return int(reply.split(b" ", 2)[1])


def get(target: str, *headers: str, method: str = "GET", body: bytes = b"") -> bytes:
    lines = [f"{method} {target} HTTP/1.1", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


# ---------------------------------------------------------------- CONNECT


async def test_connect_allowed_relays_bytes(live: Live) -> None:
    first = real_client_hello(ALLOWED)
    ping, pong = application_data(b"ping"), application_data(b"pong")
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        assert t.reply == b"HTTP/1.1 200 Connection established\r\n\r\n"
        t.writer.write(first + ping)
        await t.writer.drain()
        async with asyncio.timeout(TIMEOUT):
            answered = await t.reader.readexactly(len(SERVER_HELLO) + len(ping))
        assert answered == SERVER_HELLO + ping
        t.writer.write(pong)
        await t.writer.drain()
        async with asyncio.timeout(TIMEOUT):
            assert await t.reader.readexactly(len(pong)) == pong
    await live.upstream.wait_for(1)
    assert live.upstream.received == [first + ping + pong]
    assert live.connects == [("127.0.0.1", 443)]
    assert live.resolved == [(ALLOWED, 443)]
    assert live.policy.reached(RUN) == [ALLOWED]


async def test_connect_name_is_matched_without_case(live: Live) -> None:
    first = hello(sni(b"API.Example.COM"))
    async with tunnel(live.port, "Api.Example.com:443") as t:
        assert status(t.reply) == 200
        t.writer.write(first)
        await t.writer.drain()
        async with asyncio.timeout(TIMEOUT):
            assert await t.reader.readexactly(len(SERVER_HELLO)) == SERVER_HELLO
    assert live.connects == [("127.0.0.1", 443)]


async def test_connect_disallowed_host_403(live: Live) -> None:
    reply = await exchange(live.port, get("evil.example.com:443", method="CONNECT"))
    assert status(reply) == 403
    assert b"evil.example.com is not on this client's allow list" in reply
    assert live.connects == []
    assert live.resolved == []


async def test_private_resolution_403(live: Live) -> None:
    reply = await exchange(live.port, get(f"{PRIVATE}:443", method="CONNECT"))
    assert status(reply) == 403
    assert b"public" in reply
    assert b"10.0.0.1" not in reply
    reply = await exchange(live.port, get(f"http://{PRIVATE}/"))
    assert status(reply) == 403
    assert live.connects == []
    assert live.policy.reached(RUN) == []


async def test_metadata_address_refused_by_the_real_resolution() -> None:
    async def metadata(host: str, port: int) -> list[str]:
        return ["169.254.169.254"]

    async with running(functools.partial(resolve_public, resolver=metadata)) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        reply = await exchange(live.port, get(f"{ALLOWED}:443", method="CONNECT"))
        assert status(reply) == 403
        assert live.connects == []


@pytest.mark.parametrize(
    "authority",
    [f"{ALLOWED}:8080", f"{ALLOWED}:80", f"{ALLOWED}:22", f"{ALLOWED}:0", f"{ALLOWED}:99999"],
)
async def test_port_8080_403(live: Live, authority: str) -> None:
    reply = await exchange(live.port, get(authority, method="CONNECT"))
    assert status(reply) in (400, 403)
    assert live.connects == []
    assert live.resolved == []


@pytest.mark.parametrize(
    "authority",
    [
        "169.254.169.254:443",
        "127.0.0.1:443",
        "127.1:443",
        "[::1]:443",
        "[fd00::1]:443",
        "2130706433:443",
        f"user@{ALLOWED}:443",
        "bücher.example:443",
        f"{ALLOWED}.:443",
        "localhost:443",
    ],
)
async def test_connect_to_addresses_and_bad_names_403(live: Live, authority: str) -> None:
    reply = await exchange(live.port, get(authority, method="CONNECT").decode("latin-1").encode())
    assert status(reply) == 403
    assert b"not a valid host name" in reply
    assert live.resolved == []


async def test_connect_sni_mismatch_is_closed(live: Live) -> None:
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        assert status(t.reply) == 200
        t.writer.write(real_client_hello("evil.example.com"))
        await t.writer.drain()
        assert await read_to_end(t.reader) == b""
    assert live.connects == []
    assert live.policy.reached(RUN) == []


async def test_connect_without_sni_is_closed(live: Live) -> None:
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(real_client_hello(None))
        await t.writer.drain()
        assert await read_to_end(t.reader) == b""
    assert live.connects == []


@pytest.mark.parametrize(
    "data",
    [
        b"GET / HTTP/1.1\r\nHost: evil.example.com\r\n\r\n",
        b"\x16\x03\x01\x40\x01" + b"\x01" * 100,  # a record longer than TLS allows
        hello(sni(b"api.example.com") + sni(b"evil.example.com")),
        hello(sni(b"api.example.com", b"evil.example.com")),
        hello(sni(b"api.example.com"))[:-1],
    ],
    ids=["plain-http", "oversized-record", "repeated-sni", "two-names", "short"],
)
async def test_connect_without_a_matching_client_hello_is_closed(live: Live, data: bytes) -> None:
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(data)
        await t.writer.drain()
        if data == hello(sni(b"api.example.com"))[:-1]:
            t.writer.write_eof()  # the proxy is still waiting for the last byte
        assert await read_to_end(t.reader) == b""
    assert live.connects == []


async def test_connect_waits_only_the_idle_timeout_for_the_hello() -> None:
    async with running(idle_timeout=0.2) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            assert status(t.reply) == 200
            t.writer.write(b"\x16\x03")  # a hello that never finishes
            await t.writer.drain()
            started = time.monotonic()
            assert await read_to_end(t.reader) == b""
            assert time.monotonic() - started < 2
        assert live.connects == []


async def test_connect_with_a_body_refused(live: Live) -> None:
    reply = await exchange(
        live.port, get(f"{ALLOWED}:443", "Content-Length: 5", method="CONNECT", body=b"hello")
    )
    assert status(reply) == 400
    assert live.connects == []


async def test_encrypted_client_hello_is_closed(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(with_extension(real_client_hello(ALLOWED), 0xFE0D, bytes(40)))
        await t.writer.drain()
        assert await read_to_end(t.reader) == b""
    assert live.connects == []
    lines = [r.getMessage() for r in caplog.records if r.name == "footfall.proxy.server"]
    assert len(lines) == 1
    assert lines[0].endswith("refused: encrypted client hello")


# ---------------------------------------------------------------- records after the first


def split_hello(name: str) -> bytes:
    """A ClientHello for `name`, split across two handshake records."""
    message = real_client_hello(name)[5:]
    return record(22, message[:40]) + record(22, message[40:])


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        (real_client_hello("evil.example.com"), "the TLS server name is not the CONNECT host"),
        (real_client_hello(None), "the TLS ClientHello names no server"),
        (with_extension(real_client_hello(ALLOWED), 0xFE0D, bytes(40)), "encrypted client hello"),
        (hello(sni(b"api.example.com", b"evil.example.com")), "more than one host"),
        (split_hello(ALLOWED), "truncated"),
    ],
    ids=["other-name", "no-name", "encrypted", "two-names", "split"],
)
async def test_a_second_client_hello_must_name_the_host(
    caplog: pytest.LogCaptureFixture, second: bytes, reason: str
) -> None:
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = HELLO_RETRY + CHANGE_CIPHER_SPEC
        live.upstream.echo = False
        first = real_client_hello(ALLOWED)
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first)
            await t.writer.drain()
            async with asyncio.timeout(TIMEOUT):
                await t.reader.readexactly(len(live.upstream.greeting))
            t.writer.write(CHANGE_CIPHER_SPEC + second)
            await t.writer.drain()
            assert await read_to_end(t.reader) == b""
        await live.upstream.wait_for(1)
        assert live.upstream.received == [first + CHANGE_CIPHER_SPEC]
    lines = [r.getMessage() for r in caplog.records if r.name == "footfall.proxy.server"]
    assert len(lines) == 2
    assert lines[0].endswith("allowed")
    assert "refused" in lines[1] and reason in lines[1]


async def test_a_second_client_hello_naming_the_host_is_relayed() -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = HELLO_RETRY
        live.upstream.echo = False
        first, second = real_client_hello(ALLOWED), real_client_hello(ALLOWED.upper())
        data = application_data(b"after the handshake")
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first)
            await t.writer.drain()
            async with asyncio.timeout(TIMEOUT):
                await t.reader.readexactly(len(HELLO_RETRY))
            t.writer.write(CHANGE_CIPHER_SPEC + second + data)
            await t.writer.drain()
        await live.upstream.wait_for(1)
        assert live.upstream.received == [first + CHANGE_CIPHER_SPEC + second + data]


async def test_a_client_hello_before_any_answer_is_checked() -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = b""  # the server has not answered yet
        live.upstream.echo = False
        first = real_client_hello(ALLOWED)
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first + real_client_hello("evil.example.com"))
            await t.writer.drain()
            assert await read_to_end(t.reader) == b""
        await live.upstream.wait_for(1)
        assert live.upstream.received == [first]


async def test_early_data_does_not_end_the_checks() -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = HELLO_RETRY
        live.upstream.echo = False
        first, early = real_client_hello(ALLOWED), application_data(b"0-RTT data")
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first + early)
            await t.writer.drain()
            async with asyncio.timeout(TIMEOUT):
                await t.reader.readexactly(len(HELLO_RETRY))
            t.writer.write(real_client_hello("evil.example.com"))
            await t.writer.drain()
            assert await read_to_end(t.reader) == b""
        await live.upstream.wait_for(1)
        assert live.upstream.received == [first + early]


async def test_after_the_server_hello_records_pass_untouched() -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.echo = False
        first = real_client_hello(ALLOWED)
        # As in TLS 1.2: a key exchange, then an encrypted Finished in a handshake record.
        rest = (
            record(22, b"\x10\x00\x00\x02\x00\x00")
            + CHANGE_CIPHER_SPEC
            + record(22, bytes(range(40)))
            + application_data(b"request")
        )
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first)
            await t.writer.drain()
            async with asyncio.timeout(TIMEOUT):
                await t.reader.readexactly(len(SERVER_HELLO))
            t.writer.write(rest)
            await t.writer.drain()
        await live.upstream.wait_for(1)
        assert live.upstream.received == [first + rest]


@pytest.mark.parametrize(
    "data",
    [
        b"GET / HTTP/1.1\r\nHost: evil.example.com\r\n\r\n",
        b"\x17\x03\x03\x48\x01" + bytes(100),  # longer than any TLS record
        record(24, b"heartbeat"),
        b"\x16\x03\x03\x00",  # a header cut short, then the client stops sending
    ],
    ids=["plain-http", "oversized", "unknown-type", "cut-short"],
)
async def test_bad_records_after_the_hello_close_the_tunnel(
    caplog: pytest.LogCaptureFixture, data: bytes
) -> None:
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = b""
        live.upstream.echo = False
        first = real_client_hello(ALLOWED)
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first + data)
            await t.writer.drain()
            if data == b"\x16\x03\x03\x00":
                t.writer.write_eof()
            assert await read_to_end(t.reader) == b""
        await live.upstream.wait_for(1)
        assert live.upstream.received == [first]
    lines = [r.getMessage() for r in caplog.records if r.name == "footfall.proxy.server"]
    assert (
        lines[-1]
        .split(" ", 4)[4]
        .startswith(
            (
                "refused: the client sent a record the proxy cannot read (",
                "refused: the client stopped",
            )
        )
    )


async def test_a_client_half_close_still_gets_the_reply(live: Live) -> None:
    data = application_data(b"request")
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(real_client_hello(ALLOWED))
        await t.writer.drain()
        async with asyncio.timeout(TIMEOUT):
            await t.reader.readexactly(len(SERVER_HELLO))
        t.writer.write(data)
        await t.writer.drain()
        t.writer.write_eof()  # the client is done sending, not done reading
        assert await read_to_end(t.reader) == data


async def test_a_server_half_close_still_takes_the_request() -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.echo = False
        live.upstream.half_close = True
        first, data = real_client_hello(ALLOWED), application_data(b"late request")
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(first)
            await t.writer.drain()
            assert await read_to_end(t.reader) == SERVER_HELLO  # then the server's half-close
            t.writer.write(data)
            await t.writer.drain()
            t.writer.write_eof()
            await live.upstream.wait_for(1)
        assert live.upstream.received == [first + data]


async def test_a_record_cut_after_its_header_is_not_a_clean_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = b""
        live.upstream.echo = False
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(real_client_hello(ALLOWED) + b"\x17\x03\x03\x00\x05")
            await t.writer.drain()
            t.writer.write_eof()
            assert await read_to_end(t.reader) == b""
    lines = [r.getMessage() for r in caplog.records if r.name == "footfall.proxy.server"]
    assert lines[-1].endswith("refused: the client stopped inside a TLS record")


async def test_an_unexpected_failure_in_a_tunnel_is_logged(
    live: Live, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Any, payload: bytes) -> None:
        raise RuntimeError("a bug in the proxy")

    monkeypatch.setattr("footfall.proxy.server.ServerHellos.feed", broken)
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    first = real_client_hello(ALLOWED)
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(first)
        await t.writer.drain()
        assert await read_to_end(t.reader) == b""
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "unexpectedly" in errors[0].getMessage()
    assert errors[0].exc_info is not None
    assert isinstance(errors[0].exc_info[1], RuntimeError)
    assert first.hex() not in caplog.text and repr(first) not in caplog.text


@pytest.mark.parametrize(
    ("greeting", "relayed"),
    [
        (HELLO_RETRY + HELLO_RETRY, HELLO_RETRY),
        (b"HTTP/1.1 400 Bad Request\r\n\r\n", b""),
        (record(22, b"\x0b\x00\x00\x22" + bytes(34)), b""),
    ],
    ids=["two-retries", "not-tls", "not-a-server-hello"],
)
async def test_a_server_handshake_that_cannot_be_followed_closes_the_tunnel(
    greeting: bytes, relayed: bytes
) -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.greeting = greeting
        live.upstream.echo = False
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(real_client_hello(ALLOWED))
            await t.writer.drain()
            assert await read_to_end(t.reader) == relayed


# ---------------------------------------------------------------- plain HTTP


async def test_http_absolute_form_forwarded(live: Live, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    reply = await exchange(
        live.port,
        get(
            f"http://{ALLOWED}/path/to?q=private-query",
            f"Host: {ALLOWED}",
            "User-Agent: footfall-test-agent",
            "Accept: */*",
            "Proxy-Authorization: Basic bm90LWEtcmVhbC1zZWNyZXQ=",  # gitleaks:allow
            "Proxy-Connection: keep-alive",
            "Connection: keep-alive, Upgrade",
            "Upgrade: websocket",
            "Keep-Alive: timeout=5",
            "TE: trailers",
        ),
    )
    assert reply == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received == [
        b"GET /path/to?q=private-query HTTP/1.1\r\n"
        b"Host: api.example.com\r\n"
        b"User-Agent: footfall-test-agent\r\n"
        b"Accept: */*\r\n"
        b"Connection: close\r\n\r\n"
    ]
    assert live.connects == [("127.0.0.1", 80)]
    assert live.resolved == [(ALLOWED, 80)]
    # One line for the decision, with no header value and no query string.
    lines = [r.getMessage() for r in caplog.records if r.name == "footfall.proxy.server"]
    assert len(lines) == 1
    assert all(part in lines[0] for part in (CLIENT, RUN, ALLOWED, "80", "allowed"))
    assert "private-query" not in caplog.text
    assert "footfall-test-agent" not in caplog.text
    assert "bm90" not in caplog.text


@pytest.mark.parametrize(
    ("target", "origin"),
    [
        (f"http://{ALLOWED}", b"/"),
        (f"HTTP://{ALLOWED.upper()}:80/a", b"/a"),
        (f"http://{ALLOWED}?x=1", b"/?x=1"),
        (f"http://{ALLOWED}//evil.example.com/x", b"/evil.example.com/x"),
        (f"http://{ALLOWED}///x?y=//z", b"/x?y=//z"),
    ],
)
async def test_http_targets_become_origin_form(live: Live, target: str, origin: bytes) -> None:
    assert await exchange(live.port, get(target)) == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received[0].startswith(b"GET " + origin + b" HTTP/1.1\r\n")
    assert b"\r\nHost: api.example.com\r\n" in live.upstream.received[0]


async def test_http_client_host_header_is_replaced(live: Live) -> None:
    reply = await exchange(
        live.port,
        get(f"http://{ALLOWED}/", "Host: evil.example.com", "host: evil2.example.com"),
    )
    assert reply == RESPONSE
    await live.upstream.wait_for(1)
    sent = live.upstream.received[0]
    assert sent == b"GET / HTTP/1.1\r\nHost: api.example.com\r\nConnection: close\r\n\r\n"


async def test_http_headers_that_name_another_host_are_dropped(live: Live) -> None:
    # A server that routes by one of these would serve another site behind the checked name.
    reply = await exchange(
        live.port,
        get(
            f"http://{ALLOWED}/",
            "X-Forwarded-Host: evil.example.com",
            "forwarded: for=10.0.0.1;host=evil.example.com",
            "X-Host: evil.example.com",
            "X_Forwarded_Host: evil.example.com",
            "X-Forwarded-Server: evil.example.com",
            "X-Original-Host: evil.example.com",
            "X-HTTP-Host-Override: evil.example.com",
            "X-Forwarded-For: 10.0.0.1",
            "X-Kept: yes",
        ),
    )
    assert reply == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received == [
        b"GET / HTTP/1.1\r\nHost: api.example.com\r\n"
        b"X-Forwarded-For: 10.0.0.1\r\nX-Kept: yes\r\nConnection: close\r\n\r\n"
    ]


async def test_http_body_forwarded_by_its_content_length(live: Live) -> None:
    request = get(f"http://{ALLOWED}/form", "Content-Length: 5", method="POST", body=b"hello")
    assert await exchange(live.port, request) == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received == [
        b"POST /form HTTP/1.1\r\nHost: api.example.com\r\n"
        b"Content-Length: 5\r\nConnection: close\r\n\r\nhello"
    ]


async def test_underscores_in_header_names_count_as_hyphens(live: Live) -> None:
    # Some servers read Content_Length as Content-Length, so the proxy does too, and writes
    # the one length it checked.
    request = get(
        f"http://{ALLOWED}/form",
        "Content_Length: 5",
        "Proxy_Authorization: Basic bm90LWEtcmVhbC1zZWNyZXQ=",  # gitleaks:allow
        "Keep_Alive: timeout=5",
        "X_Custom: kept",
        method="POST",
        body=b"hello",
    )
    assert await exchange(live.port, request) == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received == [
        b"POST /form HTTP/1.1\r\nHost: api.example.com\r\nX_Custom: kept\r\n"
        b"Content-Length: 5\r\nConnection: close\r\n\r\nhello"
    ]


@pytest.mark.parametrize(
    "request_bytes",
    [
        get(f"http://{ALLOWED}/", "Content-Length: 5", body=b"hello"),
        get(f"http://{ALLOWED}/", "Content-Length: 1", method="HEAD", body=b"x"),
        get(f"http://{ALLOWED}/", "Transfer-Encoding: chunked", body=b"0\r\n\r\n"),
        get(f"http://{ALLOWED}/", "Transfer-Encoding: chunked", method="HEAD"),
    ],
    ids=["get-length", "head-length", "get-chunked", "head-chunked"],
)
async def test_get_or_head_with_a_body_refused(live: Live, request_bytes: bytes) -> None:
    reply = await exchange(live.port, request_bytes)
    assert status(reply) == 400
    assert b"must not carry a body" in reply
    assert live.connects == []


async def test_get_with_an_empty_body_is_forwarded(live: Live) -> None:
    assert await exchange(live.port, get(f"http://{ALLOWED}/", "Content-Length: 0")) == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received == [
        b"GET / HTTP/1.1\r\nHost: api.example.com\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    ]


async def test_http_second_request_is_not_forwarded(live: Live) -> None:
    first = get(f"http://{ALLOWED}/one", "Content-Length: 5", method="POST", body=b"hello")
    second = get("http://evil.example.com/two", "Host: evil.example.com")
    smuggled = get("/three", "Host: evil.example.com")
    reply = await exchange(live.port, first + second + smuggled)
    assert reply == RESPONSE
    await live.upstream.wait_for(1)
    assert live.upstream.received == [
        b"POST /one HTTP/1.1\r\nHost: api.example.com\r\n"
        b"Content-Length: 5\r\nConnection: close\r\n\r\nhello"
    ]
    assert live.connects == [("127.0.0.1", 80)]


async def test_http_large_response_is_relayed() -> None:
    async with running() as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        body = bytes(range(256)) * 4096  # 1 MiB
        live.upstream.response = b"HTTP/1.1 200 OK\r\nContent-Length: 1048576\r\n\r\n" + body
        reply = await exchange(live.port, get(f"http://{ALLOWED}/big"))
        assert reply == live.upstream.response


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        (("Transfer-Encoding: chunked",), 411),
        (("Transfer_Encoding: chunked", "Content-Length: 5"), 400),
        (("transfer_encoding: chunked",), 411),
        (("Content_Length: 5", "Content-Length: 5"), 400),
        (("Transfer-Encoding: chunked", "Content-Length: 5"), 400),
        (("Content-Length: 5", "Content-Length: 5"), 400),
        (("Content-Length: 5, 5",), 400),
        (("Content-Length: -1",), 400),
        (("Content-Length: +5",), 400),
        (("Content-Length: 0x5",), 400),
        (("Content-Length: " + "9" * 16,), 400),
        (("Content-Length:",), 400),
        (("Bad Header: x",), 400),
        (("Bad-Header : x",), 400),
        (("NoColon",), 400),
        (("X-Folded: a", " b"), 400),
        (("X-Control: a\x00b",), 400),
        (("X-Control: a\x7fb",), 400),
        # C1 control bytes, which a server that reads the head as Latin-1 may take for a line
        # break (0x85 is NEL) or a terminal control.
        (("X-Control: a\x80b",), 400),
        (("X-Control: a\x85b",), 400),
        (("X-Control: a\x9fb",), 400),
    ],
)
async def test_ambiguous_or_malformed_headers_refused(
    live: Live, headers: tuple[str, ...], code: int
) -> None:
    request = get(f"http://{ALLOWED}/", *headers, method="POST", body=b"hello")
    reply = await exchange(live.port, request)
    assert status(reply) == code
    assert live.connects == []


async def test_other_high_bytes_in_a_header_value_are_forwarded(live: Live) -> None:
    # Bytes from 0xA0 up are obs-text, which HTTP still allows in a value.
    assert (
        await exchange(live.port, get(f"http://{ALLOWED}/", "X-Text: \xa0caf\xe9\xff")) == RESPONSE
    )
    await live.upstream.wait_for(1)
    assert b"\r\nX-Text: \xa0caf\xe9\xff\r\n" in live.upstream.received[0]


@pytest.mark.parametrize(
    "data",
    [
        b"GET http://api.example.com/ HTTP/1.1\nHost: api.example.com\r\n\r\n",
        b"GET http://api.example.com/ HTTP/1.1\r\nHost: api.example.com\n\r\n\r\n",
        b"GET http://api.example.com/ HTTP/1.1\r\nX-A: 1\rX-B: 2\r\n\r\n",
        b"GET http://api.example.com/ HTTP/1.1\r\nX-A: 1\n\n"
        b"GET http://evil.example.com/ HTTP/1.1\r\n\r\n",
        b"\r\nGET http://api.example.com/ HTTP/1.1\r\n\r\n",
    ],
    ids=["lf-request-line", "lf-header", "bare-cr", "lf-smuggle", "leading-blank"],
)
async def test_bare_line_endings_refused(live: Live, data: bytes) -> None:
    reply = await exchange(live.port, data)
    assert status(reply) == 400
    # Refused for its line endings, not for whatever the stray CR or LF did to a field.
    assert b"CRLF" in reply or data.startswith(b"\r\n")
    assert live.connects == []


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("/relative", 400),
        (f"https://{ALLOWED}/", 400),
        (f"ftp://{ALLOWED}/", 400),
        (f"http://{ALLOWED}/#fragment", 400),
        (f"http://user:pw@{ALLOWED}/", 400),
        (f"http://{ALLOWED}@evil.example.com/", 400),
        (f"http://{ALLOWED}:/", 400),
        (f"http://{ALLOWED}:443/", 403),
        (f"http://{ALLOWED}:8080/", 403),
        ("http://169.254.169.254/latest/meta-data/", 403),
        ("http://[::1]/", 403),
        ("http://127.1/", 403),
        ("http://0x7f.1/", 403),
        ("http://localhost/", 403),
        ("http://evil.example.com/", 403),
        ("http:///nohost", 403),
    ],
)
async def test_bad_http_targets_refused(live: Live, target: str, code: int) -> None:
    reply = await exchange(live.port, get(target))
    assert status(reply) == code
    assert live.connects == []


@pytest.mark.parametrize(
    "line",
    [
        "TRACE http://api.example.com/ HTTP/1.1",
        "get http://api.example.com/ HTTP/1.1",
        "GET http://api.example.com/ HTTP/2.0",
        "GET http://api.example.com/  HTTP/1.1",
        "GET http://api.example.com/ HTTP/1.1 extra",
        "GET http://api.example.com/\x01 HTTP/1.1",
        "GET http://api.example.com/\xe9 HTTP/1.1",
    ],
)
async def test_bad_request_lines_refused(live: Live, line: str) -> None:
    reply = await exchange(live.port, (line + "\r\n\r\n").encode("latin-1"))
    assert status(reply) in (400, 405)
    assert live.connects == []


async def test_oversized_headers_refused(live: Live) -> None:
    padding = "X-Pad: " + "p" * MAX_HEAD
    reply = await exchange(live.port, get(f"http://{ALLOWED}/", padding))
    assert status(reply) == 431
    assert live.connects == []
    assert MAX_HEAD == 16 * 1024


async def test_headers_just_within_the_limit_are_read(live: Live) -> None:
    request = get(f"http://{ALLOWED}/", "X-Pad: ")
    padding = "p" * (MAX_HEAD - len(request))
    request = get(f"http://{ALLOWED}/", "X-Pad: " + padding)
    assert len(request) == MAX_HEAD
    assert await exchange(live.port, request) == RESPONSE


@pytest.mark.parametrize("extra", [1, 4])
async def test_headers_just_over_the_limit_are_refused(live: Live, extra: int) -> None:
    request = get(f"http://{ALLOWED}/", "X-Pad: ")
    padding = "p" * (MAX_HEAD + extra - len(request))
    request = get(f"http://{ALLOWED}/", "X-Pad: " + padding)
    assert len(request) == MAX_HEAD + extra
    reply = await exchange(live.port, request)
    assert status(reply) == 431
    assert live.connects == []


async def test_too_many_headers_refused(live: Live) -> None:
    headers = [f"X-{n}: 1" for n in range(101)]
    reply = await exchange(live.port, get(f"http://{ALLOWED}/", *headers))
    assert status(reply) == 431


async def test_upstream_that_cannot_be_reached_is_502() -> None:
    async def nowhere(address: str, port: int) -> tuple[asyncio.StreamReader, Any]:
        raise ConnectionRefusedError(61, "Connection refused")

    async with running(open_upstream=nowhere) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        reply = await exchange(live.port, get(f"http://{ALLOWED}/"))
        assert status(reply) == 502
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(real_client_hello(ALLOWED))
            await t.writer.drain()
            assert await read_to_end(t.reader) == b""
        # Nothing was reached, so the run's page lists nothing.
        assert live.policy.reached(RUN) == []


# ---------------------------------------------------------------- unknown clients (R17)


async def test_unknown_client_403() -> None:
    async with running(unknown_wait=0.3, unknown_poll=0.05) as live:
        started = time.monotonic()
        reply = await exchange(live.port, get(f"{ALLOWED}:443", method="CONNECT"))
        waited = time.monotonic() - started
        assert status(reply) == 403
        assert b"unknown client" in reply
        assert 0.3 <= waited < 2
        assert live.resolved == []


async def test_client_allowed_during_the_wait_is_served() -> None:
    async with running(unknown_wait=3.0, unknown_poll=0.05) as live:
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, live.policy.allow, CLIENT, RUN, [ALLOWED])
        started = time.monotonic()
        reply = await exchange(live.port, get(f"http://{ALLOWED}/"))
        assert reply == RESPONSE
        assert 0.3 <= time.monotonic() - started < 2


async def test_only_an_unknown_client_waits() -> None:
    async with running(unknown_wait=10.0, unknown_poll=0.05) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        started = time.monotonic()
        for request in (
            get("evil.example.com:443", method="CONNECT"),
            get(f"{ALLOWED}:8443", method="CONNECT"),
            get("http://evil.example.com/"),
            get("http://169.254.169.254/"),
        ):
            assert status(await exchange(live.port, request)) == 403
        assert time.monotonic() - started < 2


async def test_an_unknown_client_asking_for_an_address_is_refused_at_once() -> None:
    async with running(unknown_wait=10.0, unknown_poll=0.05) as live:
        started = time.monotonic()
        reply = await exchange(live.port, get("169.254.169.254:443", method="CONNECT"))
        assert status(reply) == 403
        assert time.monotonic() - started < 2


# ---------------------------------------------------------------- limits


async def test_idle_client_is_closed() -> None:
    async with running(idle_timeout=0.2) as live:
        reader, writer = await asyncio.open_connection("127.0.0.1", live.port)
        try:
            started = time.monotonic()
            writer.write(b"GET http://api.example.com/ HTTP/1.1\r\n")  # never finished
            await writer.drain()
            reply = await read_to_end(reader)
            assert reply == b"" or status(reply) == 408
            assert time.monotonic() - started < 2
        finally:
            writer.close()
            await writer.wait_closed()


async def test_idle_tunnel_is_closed() -> None:
    async with running(idle_timeout=0.3) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(real_client_hello(ALLOWED))
            await t.writer.drain()
            started = time.monotonic()
            assert await read_to_end(t.reader) == SERVER_HELLO  # the answer, then the close
            assert 0.3 <= time.monotonic() - started < 2


async def test_a_half_closed_tunnel_still_ends_at_the_idle_timeout() -> None:
    """One side has finished and the other has gone quiet: the tunnel must not wait forever."""
    async with running(idle_timeout=0.3) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        live.upstream.echo = False
        live.upstream.half_close = True  # the server is done sending, but still reading
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(real_client_hello(ALLOWED))
            await t.writer.drain()
            assert await read_to_end(t.reader) == SERVER_HELLO  # then the server's half-close
            started = time.monotonic()
            # The client says nothing more and never half-closes; only the idle timeout ends it.
            await live.upstream.wait_for(1)
            assert 0.3 <= time.monotonic() - started < 2
            async with asyncio.timeout(TIMEOUT):
                while live.proxy.connections:
                    await asyncio.sleep(0.01)


async def test_a_busy_tunnel_is_not_idle() -> None:
    async with running(idle_timeout=0.3) as live:
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        data = application_data(b"x")
        async with tunnel(live.port, f"{ALLOWED}:443") as t:
            t.writer.write(real_client_hello(ALLOWED))
            await t.writer.drain()
            async with asyncio.timeout(TIMEOUT):
                await t.reader.readexactly(len(SERVER_HELLO))
                for _ in range(8):  # 0.8 s in all, longer than the idle timeout
                    await asyncio.sleep(0.1)
                    t.writer.write(data)
                    await t.writer.drain()
                    assert await t.reader.readexactly(len(data)) == data


async def test_connections_per_client_are_capped() -> None:
    async with running(max_per_client=2, unknown_wait=5.0) as live:
        held = [await asyncio.open_connection("127.0.0.1", live.port) for _ in range(2)]
        try:
            async with asyncio.timeout(TIMEOUT):
                while live.proxy.connections < 2:
                    await asyncio.sleep(0.01)
            started = time.monotonic()
            reply = await first_reply(live.port)
            assert status(reply) == 503
            assert b"too many connections" in reply
            assert time.monotonic() - started < 2
        finally:
            for _, writer in held:
                writer.close()
                await writer.wait_closed()
        live.policy.allow(CLIENT, RUN, [ALLOWED])
        async with asyncio.timeout(TIMEOUT):
            while live.proxy.connections:
                await asyncio.sleep(0.01)
        assert await exchange(live.port, get(f"http://{ALLOWED}/")) == RESPONSE


async def test_connections_in_all_are_capped() -> None:
    async with running(max_connections=1) as live:
        _, writer = await asyncio.open_connection("127.0.0.1", live.port)
        try:
            async with asyncio.timeout(TIMEOUT):
                while live.proxy.connections < 1:
                    await asyncio.sleep(0.01)
            assert status(await first_reply(live.port)) == 503
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    "settings",
    [
        {"idle_timeout": 0},
        {"unknown_wait": -1},
        {"unknown_poll": 0},
        {"max_connections": 0},
        {"max_per_client": 0},
        {"port": -1},
        {"port": 65536},
        {"max_lookups_per_client": 0},
        {"max_lookups_per_run": 0},
        {"lookup_threads": 0},
    ],
)
def test_bad_settings_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"."):
        ProxyServer(Policy(), **settings)


# ---------------------------------------------------------------- name lookups (F-04)

OTHER_RUN = "r_" + "q" * 26
SECOND = "::1"  # a second client address on loopback, so that two clients can be told apart


class Lookups:
    """Stands in for `socket.getaddrinfo` under the proxy's own resolver.

    A name that starts with `slow` blocks its thread until `released` is set, as a lookup
    whose name server never answers does; every other name answers at once with a public
    address. It counts the lookups of each name, and how many threads are blocked.
    """

    def __init__(self) -> None:
        self.released = threading.Event()
        self.calls: Counter[str] = Counter()
        self.blocked = 0
        self._lock = threading.Lock()

    def __call__(self, host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        with self._lock:
            self.calls[host] += 1
        if host.startswith("slow"):
            with self._lock:
                self.blocked += 1
            try:
                self.released.wait(TIMEOUT)
            finally:
                with self._lock:
                    self.blocked -= 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]

    async def wait_blocked(self, count: int) -> None:
        async with asyncio.timeout(TIMEOUT):
            while self.blocked < count:
                await asyncio.sleep(0.01)


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> Iterator[Lookups]:
    stub = Lookups()
    monkeypatch.setattr(socket, "getaddrinfo", stub)
    try:
        yield stub
    finally:
        stub.released.set()


@contextlib.asynccontextmanager
async def second_listener(live: Live) -> AsyncIterator[int]:
    """The proxy listening on ::1 too; returns the port."""
    server = await live.proxy.start(SECOND, 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


@contextlib.asynccontextmanager
async def connecting(
    host: str, port: int, authorities: list[str]
) -> AsyncIterator[list[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]:
    """Send a CONNECT for each authority, each on its own connection, without waiting."""
    opened: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    try:
        for authority in authorities:
            reader, writer = await asyncio.open_connection(host, port)
            opened.append((reader, writer))
            writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
            await writer.drain()
        yield opened
    finally:
        for _, writer in opened:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def head(reader: asyncio.StreamReader, timeout: float = TIMEOUT) -> bytes:
    async with asyncio.timeout(timeout):
        return await reader.readuntil(b"\r\n\r\n")


async def test_one_clients_slow_lookups_do_not_delay_another_clients(lookups: Lookups) -> None:
    slow = [f"slow{n}.example.com" for n in range(40)]
    async with running(real_resolve=True) as live, second_listener(live) as port:
        live.policy.allow(CLIENT, RUN, slow)
        live.policy.allow(SECOND, OTHER_RUN, [ALLOWED])
        async with connecting(CLIENT, live.port, [f"{name}:443" for name in slow]):
            await lookups.wait_blocked(4)
            await asyncio.sleep(0.2)  # every lookup the first client could start has started
            started = time.monotonic()
            async with connecting(SECOND, port, [f"{ALLOWED}:443"]) as [(reader, _)]:
                reply = await head(reader, timeout=2.0)
            assert reply.startswith(b"HTTP/1.1 200")
            assert time.monotonic() - started < 1.0
            lookups.released.set()


async def test_a_client_at_its_lookup_bound_is_refused(lookups: Lookups) -> None:
    slow = ["slow0.example.com", "slow1.example.com", "slow2.example.com"]
    async with running(real_resolve=True, max_lookups_per_client=2) as live:
        live.policy.allow(CLIENT, RUN, slow)
        async with connecting(CLIENT, live.port, [f"{slow[0]}:443", f"{slow[1]}:443"]) as held:
            await lookups.wait_blocked(2)
            async with connecting(CLIENT, live.port, [f"{slow[2]}:443"]) as [(reader, _)]:
                reply = await head(reader)
            assert reply.startswith(b"HTTP/1.1 503")
            assert b"name lookups" in reply + await read_to_end(reader)
            # A request for a name already being looked up joins that lookup instead.
            async with connecting(CLIENT, live.port, [f"{slow[0]}:443"]) as [(joined, _)]:
                await asyncio.sleep(0.1)
                assert lookups.calls[slow[0]] == 1
                lookups.released.set()
                assert (await head(joined)).startswith(b"HTTP/1.1 200")
            for reader, _ in held:
                assert (await head(reader)).startswith(b"HTTP/1.1 200")
        assert lookups.calls[slow[2]] == 0


async def test_a_runs_lookups_are_bounded_across_its_clients(lookups: Lookups) -> None:
    slow = [f"slow{n}.example.com" for n in range(4)]
    settings: dict[str, Any] = {"max_lookups_per_client": 2, "max_lookups_per_run": 3}
    async with running(real_resolve=True, **settings) as live, second_listener(live) as port:
        live.policy.allow(CLIENT, RUN, slow)
        live.policy.allow(SECOND, RUN, slow)
        async with (
            connecting(CLIENT, live.port, [f"{slow[0]}:443", f"{slow[1]}:443"]),
            connecting(SECOND, port, [f"{slow[2]}:443"]),
        ):
            await lookups.wait_blocked(3)
            async with connecting(SECOND, port, [f"{slow[3]}:443"]) as [(reader, _)]:
                reply = await head(reader) + await read_to_end(reader)
            assert reply.startswith(b"HTTP/1.1 503")
            assert b"this run" in reply
            lookups.released.set()


async def test_an_abandoned_lookup_counts_until_its_thread_returns(lookups: Lookups) -> None:
    settings: dict[str, Any] = {"max_lookups_per_client": 1, "idle_timeout": 0.3}
    async with running(real_resolve=True, **settings) as live:
        live.policy.allow(CLIENT, RUN, ["slow0.example.com", ALLOWED])
        async with connecting(CLIENT, live.port, ["slow0.example.com:443"]) as [(reader, _)]:
            reply = await head(reader)
        assert reply.startswith(b"HTTP/1.1 504")  # the wait gave up; the thread did not
        assert lookups.blocked == 1
        async with connecting(CLIENT, live.port, [f"{ALLOWED}:443"]) as [(reader, _)]:
            assert (await head(reader)).startswith(b"HTTP/1.1 503")
        lookups.released.set()
        async with asyncio.timeout(TIMEOUT):
            while True:
                async with connecting(CLIENT, live.port, [f"{ALLOWED}:443"]) as [(reader, _)]:
                    if (await head(reader)).startswith(b"HTTP/1.1 200"):
                        break
                await asyncio.sleep(0.05)


# ---------------------------------------------------------------- what was reached


async def test_reached_recorded(live: Live) -> None:
    live.policy.allow(CLIENT, RUN, [ALLOWED, "tiles.example.org"])
    assert status(await exchange(live.port, get("http://evil.example.com/"))) == 403
    assert live.policy.reached(RUN) == []
    assert await exchange(live.port, get("http://tiles.example.org/a.png")) == RESPONSE
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(real_client_hello(ALLOWED))
        await t.writer.drain()
        async with asyncio.timeout(TIMEOUT):
            await t.reader.readexactly(len(SERVER_HELLO))
    assert live.policy.reached(RUN) == [ALLOWED, "tiles.example.org"]


async def test_refusals_are_logged_once_without_request_text(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="footfall.proxy.server")
    await exchange(live.port, get("http://evil.example.com/?token=private-query"))
    await exchange(live.port, get("http://bad\x01name.example/", "X-Secret: header-value"))
    async with tunnel(live.port, f"{ALLOWED}:443") as t:
        t.writer.write(real_client_hello("evil.example.com"))
        await t.writer.drain()
        await read_to_end(t.reader)
    lines = [r.getMessage() for r in caplog.records if r.name == "footfall.proxy.server"]
    assert len(lines) == 3
    fields = dict(part.split("=", 1) for part in lines[0].split() if "=" in part)
    assert fields["host"] == "evil.example.com"
    assert fields["run"] == RUN
    assert "refused" in lines[0]
    assert "refused" in lines[1]
    assert "\x01" not in caplog.text
    assert "refused" in lines[2] and "server name" in lines[2]
    assert "private-query" not in caplog.text
    assert "header-value" not in caplog.text
