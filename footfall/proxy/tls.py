"""The server name in a TLS ClientHello, which is all of TLS the egress proxy reads.

After a CONNECT, the proxy reads the client's first record and refuses the tunnel unless the
ClientHello in it names the host the CONNECT named. Otherwise an allowed name on a shared CDN
would let a client ask the CDN for any site it serves (domain fronting).

The bytes come from code under test, so the parser trusts no length: every field is checked
against what is left before it is read, and the record must hold one ClientHello and nothing
else. The ClientHello must fit in one record; a client that splits it is refused. A ClientHello
that repeats an extension, or names more than one host, is refused too, so that the proxy and
the server cannot read different names from it.
"""

from typing import Final

__all__ = ["MAX_RECORD", "RECORD_HEADER", "ClientHelloError", "record_length", "server_name"]

RECORD_HEADER: Final = 5
MAX_RECORD: Final = 2**14  # the largest plaintext record TLS allows

_HANDSHAKE: Final = 0x16
_CLIENT_HELLO: Final = 0x01
_SERVER_NAME: Final = 0x0000
_HOST_NAME: Final = 0x00
_MAX_SESSION_ID: Final = 32
_RANDOM: Final = 32


class ClientHelloError(ValueError):
    """The bytes are not one well-formed ClientHello that the proxy can read a name from."""


class _Cursor:
    """Reads a byte string from the front, refusing any read longer than what is left."""

    def __init__(self, data: bytes | memoryview) -> None:
        self._data = memoryview(data)
        self._at = 0

    @property
    def done(self) -> bool:
        return self._at == len(self._data)

    def take(self, size: int) -> memoryview:
        if size > len(self._data) - self._at:
            raise ClientHelloError("the ClientHello is truncated")
        piece = self._data[self._at : self._at + size]
        self._at += size
        return piece

    def number(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")

    def vector(self, size: int) -> "_Cursor":
        """A length-prefixed field, with a length of `size` bytes, as a cursor of its own."""
        return _Cursor(self.take(self.number(size)))

    def rest(self) -> bytes:
        return bytes(self.take(len(self._data) - self._at))

    def finish(self, what: str) -> None:
        if not self.done:
            raise ClientHelloError(f"trailing bytes after the {what}")


def record_length(header: bytes) -> int:
    """Check a TLS record header and return the length of the handshake record it starts."""
    if len(header) != RECORD_HEADER:
        raise ClientHelloError("a TLS record header is five bytes")
    if header[0] != _HANDSHAKE:
        raise ClientHelloError("not a TLS handshake record")
    if header[1] != 3 or header[2] > 4:
        raise ClientHelloError("not a TLS record version the proxy knows")
    length = int.from_bytes(header[3:5], "big")
    if not 0 < length <= MAX_RECORD:
        raise ClientHelloError(f"a TLS record length must be from 1 to {MAX_RECORD}")
    return length


def _host_name(extension: _Cursor) -> str:
    """The one host name in a server_name extension."""
    names = extension.vector(2)
    extension.finish("server name list")
    host: str | None = None
    while not names.done:
        name_type = names.number(1)
        value = names.vector(2).rest()
        if name_type != _HOST_NAME:
            raise ClientHelloError(f"unknown server name type {name_type}")
        if host is not None:
            raise ClientHelloError("the ClientHello names more than one host")
        if not value:
            raise ClientHelloError("the ClientHello names an empty host")
        if not value.isascii():
            raise ClientHelloError("the ClientHello's host name is not ASCII")
        host = value.decode("ascii")
    if host is None:
        raise ClientHelloError("the server_name extension has no host name")
    return host


def server_name(record: bytes) -> str | None:
    """The host name a ClientHello asks for, or None if it has no server_name extension.

    `record` is exactly one TLS record, header included. Raises ClientHelloError unless it
    holds exactly one well-formed ClientHello.
    """
    cursor = _Cursor(record)
    length = record_length(bytes(cursor.take(RECORD_HEADER)))
    handshake = _Cursor(cursor.take(length))
    cursor.finish("TLS record")
    if handshake.number(1) != _CLIENT_HELLO:
        raise ClientHelloError("the first handshake message is not a ClientHello")
    body = handshake.vector(3)
    handshake.finish("ClientHello")

    body.take(2)  # legacy_version
    body.take(_RANDOM)
    if len(body.vector(1).rest()) > _MAX_SESSION_ID:
        raise ClientHelloError("the session id is longer than 32 bytes")
    ciphers = len(body.vector(2).rest())
    if ciphers == 0 or ciphers % 2:
        raise ClientHelloError("the cipher suite list is empty or uneven")
    if not body.vector(1).rest():
        raise ClientHelloError("the compression method list is empty")
    if body.done:
        return None  # no extensions at all
    extensions = body.vector(2)
    body.finish("extensions")

    seen: set[int] = set()
    host: str | None = None
    while not extensions.done:
        kind = extensions.number(2)
        data = extensions.vector(2)
        if kind in seen:
            raise ClientHelloError(f"the ClientHello repeats extension {kind}")
        seen.add(kind)
        if kind == _SERVER_NAME:
            host = _host_name(data)
    return host
