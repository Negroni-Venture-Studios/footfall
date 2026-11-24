"""The step trace: one JSON line per action, kept so a run can be replayed without a model."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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


class Trace:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, step: Step) -> None:
        fields = asdict(step)
        for name, value in fields.items():
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise TraceError(
                    f"step {step.number}: field `{name}` is not JSON: {error}"
                ) from error
        # ASCII only: a lone surrogate from the model is escaped, not a write error.
        line = json.dumps(fields, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
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
                    steps.append(Step(**json.loads(line)))
                except (ValueError, TypeError) as error:
                    raise TraceError(
                        f"{self.path}: line {number} is not a step: {error}"
                    ) from error
        return steps
