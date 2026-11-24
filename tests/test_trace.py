from pathlib import Path

import pytest

from footfall.tester.trace import Step, Trace, TraceError


def step(n: int, **over: object) -> Step:
    base: dict[str, object] = {
        "number": n,
        "tool": "tap",
        "input": {"element": 4 + n, "note": "naïve «x»"},
        "target_role": "button",
        "target_name": "Pay now",
        "x": 211,
        "y": 637,
        "path_before": "/cart",
        "path_after": "/pay",
        "screenshot_before": "s1",
        "screenshot_after": "s2",
        "error": None,
    }
    base.update(over)
    return Step(**base)  # type: ignore[arg-type]


def test_trace_round_trip(tmp_path: Path) -> None:
    trace = Trace(tmp_path / "trace.jsonl")
    steps = [
        step(1),
        step(
            2,
            target_role=None,
            target_name=None,
            x=None,
            y=None,
            screenshot_after=None,
            error="no such element",
        ),
    ]
    for s in steps:
        trace.append(s)
    assert trace.steps() == steps
    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").split("\n")
    assert len(lines) == 3 and lines[2] == ""


def test_empty_or_missing_trace_has_no_steps(tmp_path: Path) -> None:
    assert Trace(tmp_path / "none.jsonl").steps() == []


def test_append_keeps_earlier_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    Trace(path).append(step(1))
    Trace(path).append(step(2))
    assert [s.number for s in Trace(path).steps()] == [1, 2]


def test_corrupt_line_names_its_number(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    trace = Trace(path)
    trace.append(step(1))
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    trace.append(step(3))
    with pytest.raises(TraceError, match="line 2"):
        trace.steps()


def test_wrong_fields_name_the_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"number": 1}\n', encoding="utf-8")
    with pytest.raises(TraceError, match="line 1"):
        Trace(path).steps()


def test_blank_line_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    Trace(path).append(step(1))
    with path.open("a", encoding="utf-8") as f:
        f.write("\n")
    with pytest.raises(TraceError, match="line 2"):
        Trace(path).steps()
