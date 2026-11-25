"""Masking Footfall's generated secrets in a GitHub Actions log."""

import os
from collections.abc import Iterator

import pytest

from footfall.actions import mask

IN_ACTIONS = {"GITHUB_ACTIONS": "true"}
# Not a real secret: the shape of a throwaway password, made up for the tests.
VALUE = "Qm9vdC1zdHJhcC1vbmx5LXRlc3Q"  # gitleaks:allow


@pytest.fixture
def pipe() -> Iterator[tuple[int, int]]:
    reader, writer = os.pipe()
    try:
        yield reader, writer
    finally:
        os.close(reader)
        os.close(writer)


def written(reader: int, writer: int) -> str:
    os.write(writer, b"END")
    data = b""
    while not data.endswith(b"END"):
        data += os.read(reader, 65536)
    return data[:-3].decode()


def test_in_actions_the_value_is_registered_on_a_line_of_its_own(
    pipe: tuple[int, int],
) -> None:
    """The command starts a line of its own, whatever was written before it without a new
    line, such as the name of the test pytest -v is running. In the middle of a line it would
    be no command, and would print the value it was meant to hide."""
    os.write(pipe[1], b"tests/test_x.py::test_y ")
    mask(VALUE, environ=IN_ACTIONS, fd=pipe[1])
    lines = written(*pipe).split("\n")
    assert lines == ["tests/test_x.py::test_y ", f"::add-mask::{VALUE}", ""]


@pytest.mark.parametrize("setting", [{}, {"GITHUB_ACTIONS": ""}, {"GITHUB_ACTIONS": "false"}])
def test_outside_actions_nothing_is_written(pipe: tuple[int, int], setting: dict[str, str]) -> None:
    mask(VALUE, environ=setting, fd=pipe[1])
    assert written(*pipe) == ""


def test_a_percent_sign_is_escaped_so_the_value_itself_is_masked(pipe: tuple[int, int]) -> None:
    # The runner reads %0A in a command as a new line; the value must arrive as it is.
    mask("abc%0Adef", environ=IN_ACTIONS, fd=pipe[1])
    assert written(*pipe) == "\n::add-mask::abc%250Adef\n"


@pytest.mark.parametrize(
    "bad", ["", "   ", "two\nlines", "carriage\rreturn", "nul\0x", "esc\x1b[0m", "nel\x85x"]
)
def test_a_value_that_would_break_the_command_is_refused_unwritten(
    pipe: tuple[int, int], bad: str
) -> None:
    with pytest.raises(ValueError) as caught:
        mask(bad, environ=IN_ACTIONS, fd=pipe[1])
    assert written(*pipe) == ""
    if bad.strip():
        assert bad not in str(caught.value)


def test_the_command_goes_to_standard_output_by_default(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    mask(VALUE)
    assert capfd.readouterr().out == f"\n::add-mask::{VALUE}\n"
