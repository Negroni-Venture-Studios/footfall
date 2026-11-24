"""What a tester reports: findings, the per-persona report, and the parsing of `finish`.

Everything the model puts in a `finish` call is hostile input. It may echo text the app
displayed, so it is type-checked, stripped of control characters and cut to a length
before it goes anywhere (AGENTS.md rule 6).
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

Severity = Literal["blocker", "major", "minor", "cosmetic"]
GoalReached = Literal["yes", "partly", "no"]
Status = Literal["finished", "incomplete", "failed"]

SEVERITIES: Final = frozenset(get_args(Severity))
GOALS: Final = frozenset(get_args(GoalReached))

CAPS: Final = {
    "title": 120,
    "step": 300,
    "steps": 12,
    "expected": 600,
    "actual": 600,
    "summary": 1500,
    "findings": 25,
}
# A page is a path on the app; this is not in CAPS because it is a guard, not a report field.
PAGE_CAP: Final = 300
ELLIPSIS: Final = "…"


class ToolError(ValueError):
    """A tool input the model must correct. Its message goes back to the model."""


@dataclass(frozen=True)
class Finding:
    title: str
    severity: Severity
    page: str
    steps: tuple[str, ...]
    expected: str
    actual: str
    screenshots: tuple[str, ...]
    source: str
    device: str


@dataclass(frozen=True)
class PersonaReport:
    persona: str
    device: str
    status: Status
    reason: str
    score: int | None
    goal_reached: GoalReached | None
    summary: str
    findings: list[Finding]
    actions: int
    cost_usd: float
    seconds: float

    def to_json(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "device": self.device,
            "status": self.status,
            "reason": self.reason,
            "score": self.score,
            "goal_reached": self.goal_reached,
            "summary": self.summary,
            "findings": [
                {
                    "title": f.title,
                    "severity": f.severity,
                    "page": f.page,
                    "steps": list(f.steps),
                    "expected": f.expected,
                    "actual": f.actual,
                    "screenshots": list(f.screenshots),
                    "source": f.source,
                    "device": f.device,
                }
                for f in self.findings
            ],
            "actions": self.actions,
            "cost_usd": self.cost_usd,
            "seconds": self.seconds,
        }


def _clean(text: str, cap: int) -> str:
    """Drop control characters other than newline and tab, then cut to `cap` characters."""
    kept = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    if len(kept) <= cap:
        return kept
    return kept[: cap - 1] + ELLIPSIS


def _get(data: Mapping[str, Any], field: str, where: str) -> Any:
    if field not in data:
        raise ToolError(f"{where}missing field `{field}`")
    return data[field]


def _text(data: Mapping[str, Any], field: str, cap: int, where: str, *, required: bool) -> str:
    value = _get(data, field, where)
    if not isinstance(value, str):
        raise ToolError(f"{where}`{field}` must be a string")
    cleaned = _clean(value, cap)
    if required and not cleaned.strip():
        raise ToolError(f"{where}`{field}` must not be empty")
    return cleaned


def _strings(data: Mapping[str, Any], field: str, where: str) -> list[str]:
    value = _get(data, field, where)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"{where}`{field}` must be a list of strings")
    return value


def _finding(
    item: object,
    index: int,
    *,
    source: str,
    device: str,
    screenshots: set[str],
) -> Finding:
    where = f"finding {index + 1}: "
    if not isinstance(item, Mapping):
        raise ToolError(f"{where}each entry in `findings` must be an object")
    severity = _get(item, "severity", where)
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise ToolError(f"{where}`severity` must be one of {', '.join(sorted(SEVERITIES))}")
    steps = _strings(item, "steps", where)[: CAPS["steps"]]
    shots = _strings(item, "screenshots", where)
    for shot in shots:
        if shot not in screenshots:
            raise ToolError(
                f"{where}`screenshots` names {_clean(shot, 40)!r}, which was never taken"
            )
    return Finding(
        title=_text(item, "title", CAPS["title"], where, required=True),
        severity=severity,  # type: ignore[arg-type]  # checked against SEVERITIES above
        page=_text(item, "page", PAGE_CAP, where, required=False),
        steps=tuple(_clean(s, CAPS["step"]) for s in steps),
        expected=_text(item, "expected", CAPS["expected"], where, required=False),
        actual=_text(item, "actual", CAPS["actual"], where, required=False),
        screenshots=tuple(shots),
        source=source,
        device=device,
    )


def parse_finish(
    data: Mapping[str, Any], *, source: str, device: str, screenshots: set[str]
) -> tuple[str, int, str, list[Finding]]:
    """Check a `finish` input and return the summary, score, goal reached and findings."""
    summary = _text(data, "summary", CAPS["summary"], "", required=True)
    score = _get(data, "score", "")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
        raise ToolError("`score` must be a whole number from 0 to 10")
    reached = _get(data, "goal_reached", "")
    if not isinstance(reached, str) or reached not in GOALS:
        raise ToolError("`goal_reached` must be one of no, partly, yes")
    raw = _get(data, "findings", "")
    if not isinstance(raw, list):
        raise ToolError("`findings` must be a list")
    findings = [
        _finding(item, i, source=source, device=device, screenshots=screenshots)
        for i, item in enumerate(raw[: CAPS["findings"]])
    ]
    return summary, score, reached, findings
