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
