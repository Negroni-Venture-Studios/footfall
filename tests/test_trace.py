import errno
import json
import stat
from pathlib import Path

import pytest

from footfall.tester.folder import Folder
from footfall.tester.report import OBSERVATIONS
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


def test_every_line_says_its_text_is_observations(tmp_path: Path) -> None:
    trace = Trace(tmp_path / "trace.jsonl")
    trace.append(step(1))
    trace.append(step(2))
    for line in trace.path.read_text(encoding="utf-8").splitlines():
        assert next(iter(json.loads(line).items())) == ("note", OBSERVATIONS)
    assert trace.steps() == [step(1), step(2)]


def test_a_line_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('"a step"\n', encoding="utf-8")
    with pytest.raises(TraceError, match="line 1"):
        Trace(path).steps()


def test_a_trace_is_never_written_through_a_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "planted-by-the-branch.jsonl"
    target.parent.mkdir()
    (tmp_path / "trace.jsonl").symlink_to(target)
    with pytest.raises(OSError) as refused:
        Trace(tmp_path / "trace.jsonl").append(step(1))
    assert refused.value.errno == errno.ELOOP
    assert not target.exists()


def test_a_trace_in_a_folder_held_open_stays_there_when_a_link_replaces_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "elsewhere" / "planted-by-the-branch"
    target.mkdir(parents=True)
    (tmp_path / "grace").mkdir()
    folder = Folder.at(tmp_path / "grace")
    try:
        trace = Trace(tmp_path / "grace" / "trace.jsonl", folder)
        trace.append(step(1))
        (tmp_path / "grace").rename(tmp_path / "moved")
        (tmp_path / "grace").symlink_to(target)
        trace.append(step(2))
    finally:
        folder.close()
    assert list(target.iterdir()) == []
    assert [s.number for s in Trace(tmp_path / "moved" / "trace.jsonl").steps()] == [1, 2]


@pytest.mark.usefixtures("no_umask")
def test_a_trace_is_made_for_its_owner_alone(tmp_path: Path) -> None:
    Trace(tmp_path / "trace.jsonl").append(step(1))
    assert stat.S_IMODE((tmp_path / "trace.jsonl").stat().st_mode) == 0o600


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


def test_lone_surrogate_in_input_is_written_as_ascii_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    s = step(1, input={"text": "a\ud800b", "more": "café"})
    Trace(path).append(s)
    raw = path.read_bytes()
    assert raw.isascii()
    assert Trace(path).steps() == [s]


def test_non_json_input_is_a_trace_error_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    trace = Trace(path)
    trace.append(step(1))
    before = path.read_bytes()
    with pytest.raises(TraceError, match=r"step 2.*input"):
        trace.append(step(2, input={"bad": object()}))
    with pytest.raises(TraceError, match=r"step 3.*input"):
        trace.append(step(3, input={"bad": float("nan")}))
    assert path.read_bytes() == before


def test_guard_events_round_trip_and_default_to_none(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with_events = step(1, events=['the app redirected the browser to "evil.test"'])
    Trace(path).append(with_events)
    # A line written before steps carried events reads back with none.
    old = {k: v for k, v in step(2).__dict__.items() if k != "events"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old) + "\n")
    assert Trace(path).steps() == [with_events, step(2)]
    assert step(2).events == []


def nested(depth: int) -> dict[str, object]:
    value: dict[str, object] = {}
    for _ in range(depth):
        value = {"a": value}
    return value


def test_deeply_nested_input_is_a_trace_error_and_writes_nothing(tmp_path: Path) -> None:
    trace = Trace(tmp_path / "trace.jsonl")
    with pytest.raises(TraceError, match="step 1"):
        trace.append(step(1, input=nested(100_000)))
    assert trace.steps() == []


def test_a_deeply_nested_line_is_a_trace_error(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"input": ' + "[" * 100_000 + "]" * 100_000 + "}\n")
    with pytest.raises(TraceError, match="line 1"):
        Trace(path).steps()
