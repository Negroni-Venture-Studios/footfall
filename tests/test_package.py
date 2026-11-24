import socket

import pytest


def test_version_is_set() -> None:
    import footfall

    assert footfall.__version__ == "0.1.0"


def test_network_is_blocked() -> None:
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
