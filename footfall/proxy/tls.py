"""The little of TLS the egress proxy reads: record headers, server names and ServerHellos.

After a CONNECT, the proxy refuses the tunnel unless every ClientHello in it names the host the
CONNECT named. Otherwise an allowed name on a shared CDN would let a client ask the CDN for any
site it serves (domain fronting). The proxy follows the handshake in both directions until the
server's ServerHello settles it. A HelloRetryRequest does not settle it, because the client then
sends a second ClientHello in plain text, which must pass the same checks as the first. Once
the ServerHello has gone by, a server takes no further plain ClientHello, so the proxy relays
the rest untouched, including TLS 1.2's encrypted Finished, which travels as a handshake record.

A ClientHello that carries Encrypted Client Hello, in any of its draft or final code points,
or the older encrypted SNI, is refused outright: its server_name would name a front, and the
encrypted inner name could be any site.

The bytes come from code under test, so the parser trusts no length: every field is checked
against what is left before it is read, and a handshake record must hold one whole ClientHello
and nothing else. A client that splits a ClientHello across records is refused. A ClientHello
that repeats an extension, or names more than one host, is refused too, so that the proxy and
the server cannot read different names from it.

What this cannot stop: inside the encrypted connection, the HTTP request names its site again,
in its Host header or :authority. A server that routes by that rather than by the TLS name can
still be asked for another site it serves, and the proxy cannot see it without ending TLS
itself, which Footfall does not do. The check is therefore defence in depth; the main defence
is that an allow list names only the hosts a manifest asks for.
"""

from typing import Final

__all__ = [
    "APPLICATION_DATA",
    "HANDSHAKE",
    "HELLO_RETRY_RANDOM",
    "MAX_CIPHERTEXT",
    "MAX_RECORD",
    "RECORD_HEADER",
    "EncryptedClientHello",
    "HandshakeError",
    "ServerHellos",
    "record_header",
    "server_name",
]

RECORD_HEADER: Final = 5
MAX_RECORD: Final = 2**14  # the largest plaintext record TLS allows
MAX_CIPHERTEXT: Final = 2**14 + 2048  # the largest encrypted record, in TLS 1.2
CHANGE_CIPHER_SPEC: Final = 20
ALERT: Final = 21
HANDSHAKE: Final = 22
APPLICATION_DATA: Final = 23
# The ServerHello.random that marks a HelloRetryRequest (RFC 8446, section 4.1.3).
HELLO_RETRY_RANDOM: Final = bytes.fromhex(
    "CF21AD74E59A6111BE1D8C021E65B891C2A211167ABB8C5E079E09E2C8A8339C"
)

_CONTENT_TYPES: Final = frozenset({CHANGE_CIPHER_SPEC, ALERT, HANDSHAKE, APPLICATION_DATA})
_CLIENT_HELLO: Final = 0x01
_SERVER_HELLO: Final = 0x02
_SERVER_NAME: Final = 0x0000
# Extensions that hide the real server name: Encrypted Client Hello's drafts (0xfe08 to 0xfe0c,
# and 0xff02 from draft 7) and its final code point (0xfe0d), and the encrypted SNI that came
# before it (0xffce).
_ENCRYPTED_NAMES: Final = frozenset(
    {0xFE08, 0xFE09, 0xFE0A, 0xFE0B, 0xFE0C, 0xFE0D, 0xFF02, 0xFFCE}
)
_HOST_NAME: Final = 0x00
_MAX_SESSION_ID: Final = 32
_RANDOM: Final = 32
_MESSAGE_HEADER: Final = 4
_HELLO_PREFIX: Final = _MESSAGE_HEADER + 2 + _RANDOM  # up to the end of ServerHello.random


class HandshakeError(ValueError):
    """The bytes are not a TLS handshake the proxy can follow."""


class EncryptedClientHello(HandshakeError):
    """A ClientHello carries an extension that hides its real name (ECH or encrypted SNI)."""

    def __init__(self) -> None:
        super().__init__("encrypted client hello")


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
            raise HandshakeError("the ClientHello is truncated")
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
            raise HandshakeError(f"trailing bytes after the {what}")


def record_header(header: bytes) -> tuple[int, int]:
    """Check a TLS record header and return its content type and the length that follows.

    A plaintext record is at most 2**14 bytes and never empty; an application data record may
    be encrypted, so it may be up to 2**14 + 2048 bytes, or empty.
    """
    if len(header) != RECORD_HEADER:
        raise HandshakeError("a TLS record header is five bytes")
    content_type = header[0]
    if content_type not in _CONTENT_TYPES:
        raise HandshakeError("not a TLS record")
    if header[1] != 3 or header[2] > 4:
        raise HandshakeError("not a TLS record version the proxy knows")
    length = int.from_bytes(header[3:5], "big")
    if content_type == APPLICATION_DATA:
        if length > MAX_CIPHERTEXT:
            raise HandshakeError(f"a TLS record length must be at most {MAX_CIPHERTEXT}")
    elif not 0 < length <= MAX_RECORD:
        raise HandshakeError(f"a TLS record length must be from 1 to {MAX_RECORD}")
    return content_type, length


def _host_name(extension: _Cursor) -> str:
    """The one host name in a server_name extension."""
    names = extension.vector(2)
    extension.finish("server name list")
    host: str | None = None
    while not names.done:
        name_type = names.number(1)
        value = names.vector(2).rest()
        if name_type != _HOST_NAME:
            raise HandshakeError(f"unknown server name type {name_type}")
        if host is not None:
            raise HandshakeError("the ClientHello names more than one host")
        if not value:
            raise HandshakeError("the ClientHello names an empty host")
        if not value.isascii():
            raise HandshakeError("the ClientHello's host name is not ASCII")
        host = value.decode("ascii")
    if host is None:
        raise HandshakeError("the server_name extension has no host name")
    return host


def server_name(record: bytes) -> str | None:
    """The host name a ClientHello asks for, or None if it has no server_name extension.

    `record` is exactly one TLS record, header included. Raises HandshakeError unless it holds
    exactly one well-formed ClientHello, and EncryptedClientHello if that ClientHello carries an
    extension that hides its real name.
    """
    cursor = _Cursor(record)
    content_type, length = record_header(bytes(cursor.take(RECORD_HEADER)))
    if content_type != HANDSHAKE:
        raise HandshakeError("not a TLS handshake record")
    handshake = _Cursor(cursor.take(length))
    cursor.finish("TLS record")
    if handshake.number(1) != _CLIENT_HELLO:
        raise HandshakeError("the handshake message is not a ClientHello")
    body = handshake.vector(3)
    handshake.finish("ClientHello")

    body.take(2)  # legacy_version
    body.take(_RANDOM)
    if len(body.vector(1).rest()) > _MAX_SESSION_ID:
        raise HandshakeError("the session id is longer than 32 bytes")
    ciphers = len(body.vector(2).rest())
    if ciphers == 0 or ciphers % 2:
        raise HandshakeError("the cipher suite list is empty or uneven")
    if not body.vector(1).rest():
        raise HandshakeError("the compression method list is empty")
    if body.done:
        return None  # no extensions at all
    extensions = body.vector(2)
    body.finish("extensions")

    seen: set[int] = set()
    host: str | None = None
    while not extensions.done:
        kind = extensions.number(2)
        data = extensions.vector(2)
        if kind in _ENCRYPTED_NAMES:
            raise EncryptedClientHello
        if kind in seen:
            raise HandshakeError(f"the ClientHello repeats extension {kind}")
        seen.add(kind)
        if kind == _SERVER_NAME:
            host = _host_name(data)
    return host


class ServerHellos:
    """Follows the server's handshake records until a ServerHello settles the handshake.

    Feed it the payload of each handshake record the server sends, in order. A
    HelloRetryRequest leaves the handshake unsettled, since the client must then send a second
    ClientHello; TLS allows one at most. The first ServerHello that is not one settles it. The
    proxy keeps fewer than 38 bytes of a ServerHello split across records.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._skip = 0  # what is left of a HelloRetryRequest
        self._retried = False
        self.settled = False

    def feed(self, payload: bytes) -> None:
        """Take one handshake record's payload; raise HandshakeError if it cannot be followed."""
        if self.settled:
            return
        data = memoryview(payload)
        while data and not self.settled:
            if self._skip:
                step = min(self._skip, len(data))
                self._skip -= step
                data = data[step:]
                continue
            take = min(_HELLO_PREFIX - len(self._pending), len(data))
            self._pending += data[:take]
            data = data[take:]
            if len(self._pending) < _HELLO_PREFIX:
                return
            self._hello(bytes(self._pending))
            self._pending.clear()

    def _hello(self, prefix: bytes) -> None:
        """Judge a ServerHello by its first 38 bytes."""
        if prefix[0] != _SERVER_HELLO:
            raise HandshakeError("the server's handshake does not start with a ServerHello")
        length = int.from_bytes(prefix[1:_MESSAGE_HEADER], "big")
        if length < _HELLO_PREFIX - _MESSAGE_HEADER:
            raise HandshakeError("the server's ServerHello is too short")
        if prefix[_MESSAGE_HEADER + 2 :] != HELLO_RETRY_RANDOM:
            self.settled = True
            return
        if self._retried:
            raise HandshakeError("the server sent a second HelloRetryRequest")
        self._retried = True
        self._skip = _MESSAGE_HEADER + length - _HELLO_PREFIX
