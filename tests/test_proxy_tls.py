"""Reading the server name from a TLS ClientHello, against real and hostile bytes."""

import struct

import pytest

from footfall.proxy.tls import MAX_RECORD, ClientHelloError, record_length, server_name
from tests.tls_hellos import hello, real_client_hello, sni, vector


def test_real_client_hello_names_its_server() -> None:
    record = real_client_hello("api.example.com")
    assert record[0] == 0x16
    assert record_length(record[:5]) == len(record) - 5
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
        with pytest.raises(ClientHelloError):
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
        except ClientHelloError:
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
            except ClientHelloError:
                continue
            assert name is None or isinstance(name, str)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (b"", "truncated"),
        (hello(sni(b"api.example.com"), content_type=0x17), "not a TLS handshake"),
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
    with pytest.raises(ClientHelloError, match=message):
        server_name(record)


def test_extra_bytes_after_the_record_are_refused() -> None:
    record = hello(sni(b"api.example.com"))
    with pytest.raises(ClientHelloError, match="trailing"):
        server_name(record + b"\x17")


@pytest.mark.parametrize(
    ("header", "message"),
    [
        (b"\x16\x03\x01\x00", "header"),
        (b"GET /", "not a TLS handshake"),
        (b"\x16\x03\x01\x00\x00", "length"),
        (b"\x16\x03\x01" + struct.pack(">H", MAX_RECORD + 1), "length"),
    ],
)
def test_record_length_refuses_bad_headers(header: bytes, message: str) -> None:
    with pytest.raises(ClientHelloError, match=message):
        record_length(header)


def test_record_length_reads_the_largest_record() -> None:
    assert MAX_RECORD == 16384
    assert record_length(b"\x16\x03\x03" + struct.pack(">H", MAX_RECORD)) == MAX_RECORD
