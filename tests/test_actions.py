"""Masking Footfall's generated secrets in a GitHub Actions log."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from footfall.actions import MaskError, mask
from tests.conftest import MaskOutput

IN_ACTIONS = {"GITHUB_ACTIONS": "true"}
# Not a real secret: the shape of a throwaway password, made up for the tests.
VALUE = "Qm9vdC1zdHJhcC1vbmx5LXRlc3Q"  # gitleaks:allow


@pytest.fixture
def output() -> Iterator[MaskOutput]:
    pipe = MaskOutput()
    try:
        yield pipe
    finally:
        pipe.close()


def test_in_actions_the_value_is_registered_on_a_line_of_its_own(output: MaskOutput) -> None:
    """The command starts a line of its own, whatever was written before it without a new
    line, such as the name of the test pytest -v is running. In the middle of a line it would
    be no command, and would print the value it was meant to hide."""
    os.write(output.writer, b"tests/test_x.py::test_y ")
    mask(VALUE, environ=IN_ACTIONS, fds=(output.writer,))
    lines = output.read().split("\n")
    assert lines == ["tests/test_x.py::test_y ", f"::add-mask::{VALUE}", ""]


@pytest.mark.parametrize("setting", [{}, {"GITHUB_ACTIONS": ""}, {"GITHUB_ACTIONS": "false"}])
def test_outside_actions_nothing_is_written(output: MaskOutput, setting: dict[str, str]) -> None:
    mask(VALUE, environ=setting, fds=(output.writer,))
    assert output.read() == ""


def test_a_percent_sign_is_escaped_so_the_value_itself_is_masked(output: MaskOutput) -> None:
    # The runner reads %0A in a command as a new line; the value must arrive as it is.
    mask("abc%0Adef", environ=IN_ACTIONS, fds=(output.writer,))
    assert output.read() == "\n::add-mask::abc%250Adef\n"


@pytest.mark.parametrize(
    "bad", ["", "   ", "two\nlines", "carriage\rreturn", "nul\0x", "esc\x1b[0m", "nel\x85x"]
)
def test_a_value_that_would_break_the_command_is_refused_unwritten(
    output: MaskOutput, bad: str
) -> None:
    with pytest.raises(ValueError) as caught:
        mask(bad, environ=IN_ACTIONS, fds=(output.writer,))
    assert output.read() == ""
    if bad.strip():
        assert bad not in str(caught.value)


def test_nothing_is_written_to_a_file(tmp_path: Path) -> None:
    """A step may send footfall test's output to a file, which may become an artefact, and
    no artefact is masked: there the command would be the value in clear."""
    with (tmp_path / "out.txt").open("wb") as out:
        mask(VALUE, environ=IN_ACTIONS, fds=(out.fileno(),))
    assert (tmp_path / "out.txt").read_bytes() == b""


def test_standard_error_is_used_when_standard_output_is_a_file(
    tmp_path: Path, output: MaskOutput
) -> None:
    with (tmp_path / "out.txt").open("wb") as out:
        mask(VALUE, environ=IN_ACTIONS, fds=(out.fileno(), output.writer))
    assert (tmp_path / "out.txt").read_bytes() == b""
    assert output.read() == f"\n::add-mask::{VALUE}\n"


def broken_pipe() -> tuple[int, int]:
    """A pipe whose reader has gone, as when whoever read a job's output has stopped."""
    reader, writer = os.pipe()
    os.close(reader)
    return reader, writer


def test_a_broken_standard_output_falls_back_to_standard_error(output: MaskOutput) -> None:
    _, broken = broken_pipe()
    try:
        mask(VALUE, environ=IN_ACTIONS, fds=(broken, output.writer))
    finally:
        os.close(broken)
    assert output.read() == f"\n::add-mask::{VALUE}\n"


def test_when_no_stream_a_job_reads_takes_the_mask_it_fails_closed() -> None:
    """The secret must not be used unmasked, so the caller stops before it is."""
    first, second = broken_pipe()[1], broken_pipe()[1]
    try:
        with pytest.raises(MaskError) as caught:
            mask(VALUE, environ=IN_ACTIONS, fds=(first, second))
    finally:
        os.close(first)
        os.close(second)
    assert VALUE not in str(caught.value)
    assert VALUE[:4] not in str(caught.value)


def test_files_on_both_streams_take_nothing_and_raise_nothing(tmp_path: Path) -> None:
    with (tmp_path / "out.txt").open("wb") as out, (tmp_path / "err.txt").open("wb") as err:
        mask(VALUE, environ=IN_ACTIONS, fds=(out.fileno(), err.fileno()))
    assert (tmp_path / "out.txt").read_bytes() == b""
    assert (tmp_path / "err.txt").read_bytes() == b""


def test_by_default_it_writes_to_standard_output_or_error_only_if_a_pipe_or_terminal(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest's capture makes both temporary files, so nothing is written to either.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    mask(VALUE)
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")
