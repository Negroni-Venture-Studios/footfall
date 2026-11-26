"""TLS ClientHello records for the proxy's tests: real ones from Python's own TLS client, and
ones built by hand so that each field can be broken on its own."""

import ssl
import struct


def real_client_hello(server_hostname: str | None) -> bytes:
    """The first record Python's own TLS client sends, made without any network."""
    context = ssl.create_default_context()
    if server_hostname is None:
        context.check_hostname = False  # needed to send no name; verification stays on
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname=server_hostname)
    try:
        client.do_handshake()
    except ssl.SSLWantReadError:
        return outgoing.read()
    raise AssertionError("the handshake cannot finish without a server")


def vector(data: bytes, size: int) -> bytes:
    return len(data).to_bytes(size, "big") + data


def sni(*names: bytes, name_type: int = 0) -> bytes:
    """A server_name extension carrying `names`."""
    entries = b"".join(bytes([name_type]) + vector(name, 2) for name in names)
    return struct.pack(">H", 0) + vector(vector(entries, 2), 2)


def hello(
    extensions: bytes | None,
    *,
    session: bytes = b"",
    ciphers: bytes = b"\x13\x01",
    compression: bytes = b"\x00",
    trailing: bytes = b"",
    handshake_type: int = 1,
    content_type: int = 0x16,
    version: bytes = b"\x03\x01",
) -> bytes:
    """A ClientHello record built by hand, so that each field can be broken on its own."""
    body = b"\x03\x03" + bytes(32) + vector(session, 1) + vector(ciphers, 2)
    body += vector(compression, 1)
    if extensions is not None:
        body += vector(extensions, 2)
    message = bytes([handshake_type]) + vector(body, 3) + trailing
    return bytes([content_type]) + version + vector(message, 2)


def record(content_type: int, payload: bytes, version: bytes = b"\x03\x03") -> bytes:
    return bytes([content_type]) + version + vector(payload, 2)


def with_extension(client_hello: bytes, kind: int, data: bytes) -> bytes:
    """A copy of a ClientHello record with one more extension at the end, its lengths fixed."""
    message = client_hello[5:]
    body = message[4:]
    at = 2 + 32
    at += 1 + body[at]  # session id
    at += 2 + int.from_bytes(body[at : at + 2], "big")  # cipher suites
    at += 1 + body[at]  # compression methods
    size = int.from_bytes(body[at : at + 2], "big")
    extensions = body[at + 2 : at + 2 + size] + struct.pack(">HH", kind, len(data)) + data
    return record(22, message[:1] + vector(body[:at] + vector(extensions, 2), 3), b"\x03\x01")


def server_hello_message(random: bytes) -> bytes:
    """A ServerHello handshake message; a HelloRetryRequest when `random` is the special value."""
    body = b"\x03\x03" + random + vector(b"", 1) + b"\x13\x01" + b"\x00" + vector(b"", 2)
    return b"\x02" + vector(body, 3)


HELLO_RETRY_RANDOM = bytes.fromhex(
    "CF21AD74E59A6111BE1D8C021E65B891C2A211167ABB8C5E079E09E2C8A8339C"
)
SERVER_HELLO = record(22, server_hello_message(bytes(range(32))))
HELLO_RETRY = record(22, server_hello_message(HELLO_RETRY_RANDOM))
CHANGE_CIPHER_SPEC = record(20, b"\x01")


def application_data(payload: bytes) -> bytes:
    return record(23, payload)
