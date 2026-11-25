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
"""

import os
from collections.abc import Mapping

__all__ = ["mask"]

# The workflow command's own escapes, as GitHub's toolkit writes them, so that the runner
# masks the value itself and not what an escape in it would turn into.
_ESCAPES = str.maketrans({"%": "%25"})


def mask(value: str, *, environ: Mapping[str, str] | None = None, fd: int = 1) -> None:
    """Ask GitHub Actions to hide `value` in the job's log, when running in Actions.

    Outside GitHub Actions it does nothing. Raises ValueError, without the value, for an
    empty value or one holding a control character, such as a new line, which would end the
    command early and print the rest.
    """
    env = os.environ if environ is None else environ
    if env.get("GITHUB_ACTIONS") != "true":
        return
    if not value.strip() or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise ValueError("a value to mask must be one line of printable text")
    line = f"\n::add-mask::{value.translate(_ESCAPES)}\n".encode()
    while line:
        line = line[os.write(fd, line) :]
