"""The step trace: one JSON line per action, kept so a run can be replayed without a model.

Each line begins with a `note` that its text is what a tester observed and wrote, so that a
line read on its own is labelled too (AGENTS.md rule 6). The note is not part of a step.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from footfall.tester.folder import FILE_MODE, Folder
from footfall.tester.report import OBSERVATIONS

# Appended to, and made if it is not there, but never through a link, even one to nothing.
_APPEND: Final = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW


class TraceError(ValueError):
    """A trace file has a line that cannot be read back."""


@dataclass(frozen=True)
class Step:
    number: int
    tool: str
    input: dict[str, Any]
    target_role: str | None
    target_name: str | None
    x: int | None
    y: int | None
    path_before: str
    path_after: str
    screenshot_before: str
    screenshot_after: str | None
    error: str | None
    # What the guard stopped or saw since the step before, as the tester was told it.
    events: list[str] = field(default_factory=list)


class Trace:
    """The steps of one persona's run, in the file at `path`.

    Given `folder`, the folder `path` is in, held open, the file is written only by its name
    in that folder, so that no link put in place of a folder above it can move the trace.
    """

    def __init__(self, path: Path, folder: Folder | None = None) -> None:
        self.path = path
        self._folder = folder

    def append(self, step: Step) -> None:
        try:
            fields = asdict(step)
        except RecursionError as error:
            raise TraceError(f"step {step.number}: a field is nested too deeply") from error
        for name, value in fields.items():
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise TraceError(
                    f"step {step.number}: field `{name}` is not JSON: {error}"
                ) from error
            except RecursionError as error:
                raise TraceError(
                    f"step {step.number}: field `{name}` is nested too deeply"
                ) from error
        # ASCII only: a lone surrogate from the model is escaped, not a write error.
        labelled = {"note": OBSERVATIONS, **fields}
        line = json.dumps(labelled, ensure_ascii=True, separators=(",", ":")) + "\n"
        if self._folder is not None:
            self._folder.append(self.path.name, line.encode("ascii"))
            return
        with os.fdopen(os.open(self.path, _APPEND, FILE_MODE), "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def steps(self) -> list[Step]:
        if not self.path.exists():
            return []
        steps: list[Step] = []
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                try:
                    fields = json.loads(line)
                    if isinstance(fields, dict):
                        fields.pop("note", None)
                    steps.append(Step(**fields))
                except (ValueError, TypeError) as error:
                    raise TraceError(
                        f"{self.path}: line {number} is not a step: {error}"
                    ) from error
                except RecursionError as error:
                    raise TraceError(
                        f"{self.path}: line {number} is nested too deeply to read"
                    ) from error
        return steps
