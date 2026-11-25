"""A results folder held open, in which everything is made by name and never through a link.

The results may lie inside the folder under test, whose branch decides what is there. So once
a results folder is open, nothing in it is reached by path: each folder and file is made or
opened by its name relative to the open folder's descriptor, with O_NOFOLLOW, and a new file
with O_EXCL. A link put in its place, before Footfall looked or after, cannot send a write
anywhere else, and whatever the path names by then does not matter.
"""

import os
import stat
from pathlib import Path
from typing import Final, Self

__all__ = ["FILE_MODE", "FOLDER_MODE", "Folder"]

# Results hold a run's tester text and screenshots, and belong to the person who ran it, so
# every results file and folder Footfall makes is theirs alone.
FILE_MODE: Final = 0o600
FOLDER_MODE: Final = 0o700

_FOLDER: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_NEW: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_APPEND: Final = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW


class Folder:
    """A folder held open by its descriptor. `path` is where it was when it was opened, for
    messages and for reading back; nothing is written through it."""

    def __init__(self, fd: int, path: Path) -> None:
        self.fd = fd
        self.path = path

    @classmethod
    def at(cls, path: Path) -> Self:
        """Open the folder at `path`, which must not be a link itself."""
        return cls(os.open(path, _FOLDER), path)

    def kind(self, name: str) -> int | None:
        """The file type bits of `name` here, never following a link; None when it is not
        there."""
        try:
            return stat.S_IFMT(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            return None

    def make(self, name: str) -> "Folder":
        """Make the folder `name` here and open it. Anything already there by that name, a
        link to nothing included, raises FileExistsError."""
        os.mkdir(name, FOLDER_MODE, dir_fd=self.fd)
        return Folder(os.open(name, _FOLDER, dir_fd=self.fd), self.path / name)

    def write(self, name: str, data: bytes) -> None:
        """Write the new file `name` here. Anything already there by that name, a link to
        nothing included, raises FileExistsError."""
        with os.fdopen(os.open(name, _NEW, FILE_MODE, dir_fd=self.fd), "wb") as handle:
            handle.write(data)

    def append(self, name: str, data: bytes) -> None:
        """Add `data` to the end of the file `name` here, made if it is not there, and wait
        until it is on the disk. A link there raises OSError."""
        with os.fdopen(os.open(name, _APPEND, FILE_MODE, dir_fd=self.fd), "ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def remove(self, name: str) -> None:
        """Remove the empty folder `name` here; OSError if it is anything else."""
        os.rmdir(name, dir_fd=self.fd)

    def close(self) -> None:
        os.close(self.fd)
