"""Hiding Footfall's generated secrets from a GitHub Actions log.

When Footfall runs in a GitHub Actions job, every secret it makes, such as a run's throwaway
password or the path of a run's browser server, is registered with the `::add-mask::`
workflow command as soon as it exists. The runner then prints `***` in its place wherever
the job's log would show it, so a slip in a later test or log line is hidden rather than
published.

The command goes straight to file descriptor 1, unbuffered, so it reaches the log before
anything written after it, and it reaches it even while pytest captures `sys.stdout`. A
command the runner did not read as one would print the value it was meant to hide. The
runner reads a command only at the start of a line, so the command is written after a new
line of its own, whatever was written before it, such as the name of the test pytest -v is
running. A value that could break the command's line, or that the runner would read
differently, is refused rather than written.

The command goes only to a pipe or a terminal, which is what a job gives a step, trying
standard output and then standard error. A step may send Footfall's output to a file, which
may become an artefact, and no artefact is masked, so there the command would be the value
in clear. A failure to write, such as a pipe whose reader has gone, is not Footfall's to
report: the value then reaches no log through this stream either.
"""

import contextlib
import os
import stat
from collections.abc import Mapping, Sequence

__all__ = ["mask"]

# The workflow command's own escapes, as GitHub's toolkit writes them, so that the runner
# masks the value itself and not what an escape in it would turn into.
_ESCAPES = str.maketrans({"%": "%25"})
# Standard output, then standard error.
_STREAMS: tuple[int, ...] = (1, 2)


def _a_pipe_or_terminal(fd: int) -> bool:
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISCHR(mode)


def mask(
    value: str, *, environ: Mapping[str, str] | None = None, fds: Sequence[int] | None = None
) -> None:
    """Ask GitHub Actions to hide `value` in the job's log, when running in Actions.

    Outside GitHub Actions it does nothing. The command goes to the first of `fds`, standard
    output and standard error unless given, that is a pipe or a terminal, and nowhere if none
    is. Raises ValueError, without the value, for an empty value or one holding a control
    character, such as a new line, which would end the command early and print the rest.
    """
    env = os.environ if environ is None else environ
    if env.get("GITHUB_ACTIONS") != "true":
        return
    if not value.strip() or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise ValueError("a value to mask must be one line of printable text")
    stream = next(
        (fd for fd in (_STREAMS if fds is None else fds) if _a_pipe_or_terminal(fd)), None
    )
    if stream is None:
        return
    line = f"\n::add-mask::{value.translate(_ESCAPES)}\n".encode()
    with contextlib.suppress(OSError):
        while line:
            line = line[os.write(stream, line) :]
