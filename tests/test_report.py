import json
from typing import Any

import pytest

from footfall.tester.report import (
    CAPS,
    Finding,
    PersonaReport,
    ToolError,
    parse_finish,
)

SHOTS = {"s3", "s7"}


def good() -> dict[str, Any]:
    return {
        "summary": "Checkout works but the coupon box is clipped.",
        "score": 6,
        "goal_reached": "partly",
        "findings": [
            {
                "title": "Coupon box clipped",
                "severity": "major",
                "page": "/cart",
                "steps": ["Open /cart", "Tap the coupon box"],
                "expected": "The whole box shows",
                "actual": "The right edge is cut off",
                "screenshots": ["s7"],
            }
        ],
    }


def parse(data: dict[str, Any]) -> tuple[str, int, str, list[Finding]]:
    return parse_finish(data, source="ada", device="Pixel 7", screenshots=SHOTS)


def test_parse_finish_valid() -> None:
    summary, score, reached, findings = parse(good())
    assert (summary, score, reached) == (
        "Checkout works but the coupon box is clipped.",
        6,
        "partly",
    )
    assert findings == [
        Finding(
            title="Coupon box clipped",
            severity="major",
            page="/cart",
            steps=("Open /cart", "Tap the coupon box"),
            expected="The whole box shows",
            actual="The right edge is cut off",
            screenshots=("s7",),
            source="ada",
            device="Pixel 7",
        )
    ]


def mutate(path: str, value: Any) -> dict[str, Any]:
    data = good()
    if path.startswith("f."):
        data["findings"][0][path[2:]] = value
    else:
        data[path] = value
    return data


def drop(path: str) -> dict[str, Any]:
    data = good()
    if path.startswith("f."):
        del data["findings"][0][path[2:]]
    else:
        del data[path]
    return data


@pytest.mark.parametrize(
    ("data", "named"),
    [
        (drop("summary"), "summary"),
        (drop("score"), "score"),
        (drop("goal_reached"), "goal_reached"),
        (drop("findings"), "findings"),
        (drop("f.title"), "title"),
        (drop("f.severity"), "severity"),
        (drop("f.page"), "page"),
        (drop("f.steps"), "steps"),
        (drop("f.expected"), "expected"),
        (drop("f.actual"), "actual"),
        (drop("f.screenshots"), "screenshots"),
        (mutate("f.severity", "catastrophic"), "severity"),
        (mutate("score", 11), "score"),
        (mutate("score", -1), "score"),
        (mutate("score", True), "score"),
        (mutate("score", "7"), "score"),
        (mutate("score", 6.5), "score"),
        (mutate("goal_reached", "maybe"), "goal_reached"),
        (mutate("f.screenshots", ["s99"]), "s99"),
        (mutate("f.steps", "Open /cart"), "steps"),
        (mutate("f.steps", ["ok", 4]), "steps"),
        (mutate("f.screenshots", "s7"), "screenshots"),
        (mutate("f.title", ["a"]), "title"),
        (mutate("f.title", "   "), "title"),
        (mutate("summary", 5), "summary"),
        (mutate("findings", {"a": 1}), "findings"),
        (mutate("findings", ["text"]), "findings"),
    ],
)
def test_parse_finish_errors(data: dict[str, Any], named: str) -> None:
    with pytest.raises(ToolError, match=named):
        parse(data)


def test_caps_truncate_with_ellipsis() -> None:
    data = good()
    finding = data["findings"][0]
    finding["title"] = "T" * 500
    finding["expected"] = "E" * 5000
    finding["actual"] = "A" * 5000
    finding["steps"] = ["S" * 1000] * 30
    data["summary"] = "M" * 9999
    summary, _, _, findings = parse(data)
    assert len(summary) == CAPS["summary"]
    assert summary.endswith("…")
    assert summary[:-1] == "M" * (CAPS["summary"] - 1)
    got = findings[0]
    assert len(got.title) == CAPS["title"] and got.title.endswith("…")
    assert len(got.expected) == CAPS["expected"] and got.expected.endswith("…")
    assert len(got.actual) == CAPS["actual"] and got.actual.endswith("…")
    assert len(got.steps) == CAPS["steps"]
    assert all(len(s) == CAPS["step"] and s.endswith("…") for s in got.steps)


def test_text_at_the_cap_is_untouched() -> None:
    data = good()
    data["findings"][0]["title"] = "T" * CAPS["title"]
    _, _, _, findings = parse(data)
    assert findings[0].title == "T" * CAPS["title"]


def test_findings_over_the_cap_keep_the_first() -> None:
    data = good()
    data["findings"] = []
    for i in range(CAPS["findings"] + 7):
        item = good()["findings"][0]
        item["title"] = f"finding {i}"
        data["findings"].append(item)
    _, _, _, findings = parse(data)
    assert [f.title for f in findings] == [f"finding {i}" for i in range(CAPS["findings"])]


def test_control_characters_are_stripped_but_newline_and_tab_stay() -> None:
    data = good()
    data["summary"] = "a\x00b\x1b[31mred\x07\nline\ttab\x7f"
    data["findings"][0]["title"] = "x\x00" * 200
    summary, _, _, findings = parse(data)
    assert summary == "ab[31mred\nline\ttab"
    assert findings[0].title == "x" * (CAPS["title"] - 1) + "…"


def test_no_findings_is_allowed() -> None:
    data = good()
    data["findings"] = []
    assert parse(data)[3] == []


def test_report_to_json_is_plain_json() -> None:
    _, _, _, findings = parse(good())
    report = PersonaReport(
        persona="ada",
        device="Pixel 7",
        status="finished",
        reason="done",
        score=6,
        goal_reached="partly",
        summary="s",
        findings=findings,
        actions=17,
        cost_usd=0.41,
        seconds=93.5,
    )
    out = report.to_json()
    assert json.loads(json.dumps(out)) == out
    assert out["findings"][0]["steps"] == ["Open /cart", "Tap the coupon box"]
    assert out["findings"][0]["screenshots"] == ["s7"]
    assert out["actions"] == 17 and out["cost_usd"] == 0.41 and out["score"] == 6
