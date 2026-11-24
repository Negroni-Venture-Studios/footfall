"""Reading the server name from a TLS ClientHello, against real and hostile bytes."""

import struct

import pytest

from footfall.proxy.tls import (
    HELLO_RETRY_RANDOM,
    MAX_CIPHERTEXT,
    MAX_RECORD,
    EncryptedClientHello,
    HandshakeError,
    ServerHellos,
    record_header,
    server_name,
)
from tests import tls_hellos
from tests.tls_hellos import (
    HELLO_RETRY,
    SERVER_HELLO,
    hello,
    real_client_hello,
    server_hello_message,
    sni,
    vector,
    with_extension,
)


def test_real_client_hello_names_its_server() -> None:
    record = real_client_hello("api.example.com")
    assert record[0] == 0x16
    assert record_header(record[:5]) == (22, len(record) - 5)
    assert server_name(record) == "api.example.com"


def test_real_client_hello_without_a_name() -> None:
    assert server_name(real_client_hello(None)) is None


def test_hand_built_hello_names_its_server() -> None:
    assert server_name(hello(sni(b"api.example.com"))) == "api.example.com"
    assert server_name(hello(sni(b"API.Example.com"))) == "API.Example.com"
    assert server_name(hello(b"")) is None
    assert server_name(hello(None)) is None  # no extensions at all, as in old clients
    before = struct.pack(">HH", 23, 0)  # extended_master_secret, empty
    after = struct.pack(">HH", 13, 0)  # another extension type, so that none repeats
    assert server_name(hello(before + sni(b"api.example.com") + after)) == "api.example.com"


def test_every_truncation_of_a_real_hello_is_refused() -> None:
    record = real_client_hello("api.example.com")
    for size in range(len(record)):
        with pytest.raises(HandshakeError):
            server_name(record[:size])


def test_every_inner_truncation_is_refused() -> None:
    """Cut the handshake body short at every point, with the outer lengths made to agree."""
    record = real_client_hello("api.example.com")
    body = record[9:]
    for size in range(len(body)):
        message = b"\x01" + vector(body[:size], 3)
        cut = b"\x16\x03\x01" + vector(message, 2)
        try:
            name = server_name(cut)
        except HandshakeError:
            continue
        # Only a cut exactly at the end of the compression methods leaves a valid hello.
        assert name is None, size


def test_corrupted_bytes_raise_only_client_hello_error() -> None:
    record = real_client_hello("api.example.com")
    for position in range(len(record)):
        for value in {0x00, 0xFF, (record[position] + 1) % 256}:
            mutated = record[:position] + bytes([value]) + record[position + 1 :]
            try:
                name = server_name(mutated)
            except HandshakeError:
                continue
            assert name is None or isinstance(name, str)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (b"", "truncated"),
        (hello(sni(b"api.example.com"), content_type=0x17), "not a TLS handshake"),
        (hello(sni(b"api.example.com"), content_type=0x18), "not a TLS record"),
        (hello(sni(b"api.example.com"), version=b"\x02\x00"), "version"),
        (hello(sni(b"api.example.com"), version=b"\x03\x05"), "version"),
        (b"\x16\x03\x01\x00\x00", "length"),
        (hello(sni(b"api.example.com"), handshake_type=2), "not a ClientHello"),
        (hello(sni(b"api.example.com"), trailing=b"\x00"), "trailing"),
        (hello(sni(b"api.example.com"), session=bytes(33)), "session"),
        (hello(sni(b"api.example.com"), ciphers=b""), "cipher"),
        (hello(sni(b"api.example.com"), ciphers=b"\x13"), "cipher"),
        (hello(sni(b"api.example.com"), compression=b""), "compression"),
        (hello(sni(b"api.example.com") + sni(b"evil.example.com")), "repeats"),
        (hello(sni(b"api.example.com", b"evil.example.com")), "more than one"),
        (hello(sni(b"api.example.com", name_type=1)), "name type"),
        (hello(sni()), "no host name"),
        (hello(sni(b"")), "empty"),
        (hello(sni("bücher.example".encode())), "ASCII"),
        (hello(struct.pack(">HH", 0, 9) + b"\x00\x07"), "truncated"),
        (hello(struct.pack(">HHH", 0, 4, 5) + b"\x00\x00"), "truncated"),
        (hello(sni(b"api.example.com") + b"\x00"), "truncated"),
        (hello(struct.pack(">HH", 0, 3) + b"\x00\x00\x00"), "trailing"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_malformed_hellos_are_refused(record: bytes, message: str) -> None:
    with pytest.raises(HandshakeError, match=message):
        server_name(record)


def test_extra_bytes_after_the_record_are_refused() -> None:
    record = hello(sni(b"api.example.com"))
    with pytest.raises(HandshakeError, match="trailing"):
        server_name(record + b"\x17")


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (b"\x16\x03\x01\x00", "five bytes"),
        (b"GET /", "not a TLS record"),
        (b"\x15\x02\x00\x00\x02", "version"),
        (b"\x16\x03\x01\x00\x00", "length"),
        (b"\x14\x03\x03\x00\x00", "length"),
        (b"\x16\x03\x01" + struct.pack(">H", MAX_RECORD + 1), "length"),
        (b"\x17\x03\x03" + struct.pack(">H", MAX_CIPHERTEXT + 1), "length"),
    ],
)
def test_record_header_refuses_bad_headers(header: bytes, message: str) -> None:
    with pytest.raises(HandshakeError, match=message):
        record_header(header)


def test_record_header_reads_the_largest_records() -> None:
    assert MAX_RECORD == 16384
    assert MAX_CIPHERTEXT == 16384 + 2048
    assert record_header(b"\x16\x03\x03" + struct.pack(">H", MAX_RECORD)) == (22, MAX_RECORD)
    assert record_header(b"\x14\x03\x03\x00\x01") == (20, 1)
    assert record_header(b"\x15\x03\x03\x00\x02") == (21, 2)
    assert record_header(b"\x17\x03\x03" + struct.pack(">H", MAX_CIPHERTEXT)) == (
        23,
        MAX_CIPHERTEXT,
    )
    assert record_header(b"\x17\x03\x03\x00\x00") == (23, 0)


# ---------------------------------------------------------------- encrypted client hello


def test_encrypted_client_hello_is_refused() -> None:
    real = real_client_hello("api.example.com")
    # The helper keeps a ClientHello valid: another extension changes nothing.
    assert server_name(with_extension(real, 0x4469, b"\x00")) == "api.example.com"
    with pytest.raises(EncryptedClientHello, match="encrypted client hello"):
        server_name(with_extension(real, 0xFE0D, b"\x00" + bytes(40)))
    with pytest.raises(EncryptedClientHello):
        server_name(hello(struct.pack(">HH", 0xFE0D, 0) + sni(b"api.example.com")))
    assert issubclass(EncryptedClientHello, HandshakeError)


# ---------------------------------------------------------------- the server's side


def test_the_hello_retry_value_is_rfc_8446s() -> None:
    assert tls_hellos.HELLO_RETRY_RANDOM == HELLO_RETRY_RANDOM
    assert HELLO_RETRY_RANDOM.hex().startswith("cf21ad74")


def test_a_server_hello_settles_the_handshake() -> None:
    assert not ServerHellos().settled
    hellos = ServerHellos()
    hellos.feed(SERVER_HELLO[5:] + b"\x0b\x00\x00\x01")  # with the start of a Certificate
    hellos.feed(b"anything at all")  # no longer read
    assert hellos.settled


def test_a_hello_retry_request_does_not_settle_it() -> None:
    hellos = ServerHellos()
    hellos.feed(HELLO_RETRY[5:])
    assert not hellos.settled
    hellos.feed(SERVER_HELLO[5:])
    assert hellos.settled


def test_a_second_hello_retry_request_is_refused() -> None:
    hellos = ServerHellos()
    hellos.feed(HELLO_RETRY[5:])
    with pytest.raises(HandshakeError, match="second HelloRetryRequest"):
        hellos.feed(HELLO_RETRY[5:])


@pytest.mark.parametrize("size", [1, 3, 37, 38, 39])
def test_hellos_split_across_records_are_followed(size: int) -> None:
    hellos = ServerHellos()
    stream = HELLO_RETRY[5:] + SERVER_HELLO[5:]
    settles = len(HELLO_RETRY) - 5 + 38  # the end of the ServerHello's random
    for at in range(0, len(stream), size):
        hellos.feed(stream[at : at + size])
        assert hellos.settled == (at + size >= settles), at


def test_a_hello_retry_request_with_extensions_is_skipped_whole() -> None:
    message = server_hello_message(HELLO_RETRY_RANDOM)
    padded = message[:1] + vector(message[4:] + bytes(200), 3)
    hellos = ServerHellos()
    hellos.feed(padded[:100])
    hellos.feed(padded[100:] + SERVER_HELLO[5:])
    assert hellos.settled


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\x01" + SERVER_HELLO[6:], "does not start with a ServerHello"),
        (b"\x0b" + SERVER_HELLO[6:], "does not start with a ServerHello"),
        (b"\x02\x00\x00\x10" + bytes(34), "too short"),
    ],
)
def test_handshakes_that_cannot_be_followed_are_refused(payload: bytes, message: str) -> None:
    with pytest.raises(HandshakeError, match=message):
        ServerHellos().feed(payload)
