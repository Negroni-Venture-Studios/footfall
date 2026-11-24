import json
from typing import Any

import pytest

from footfall.tester.report import (
    CAPS,
    Finding,
    PersonaReport,
    ToolError,
    map_report_text,
    parse_finish,
    strip_invisible,
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
    kept = got.steps[:-1]
    assert all(len(s) == CAPS["step"] and s.endswith("…") for s in kept)
    assert got.steps[-1] == "…and 19 more steps"


def test_text_at_the_cap_is_untouched() -> None:
    data = good()
    data["findings"][0]["title"] = "T" * CAPS["title"]
    _, _, _, findings = parse(data)
    assert findings[0].title == "T" * CAPS["title"]


def many(severities: list[str]) -> dict[str, Any]:
    data = good()
    data["findings"] = []
    for i, sev in enumerate(severities):
        item = good()["findings"][0]
        item["title"] = f"finding {i}"
        item["severity"] = sev
        data["findings"].append(item)
    return data


def test_findings_over_the_cap_keep_the_most_severe_in_order() -> None:
    sevs = ["minor"] * 10 + ["cosmetic"] * 10 + ["major"] * 5 + ["minor"] * 6 + ["blocker"]
    assert len(sevs) == 32
    data = many(sevs)
    data["summary"] = "Short."
    summary, _, _, findings = parse(data)
    titles = [f.title for f in findings]
    assert len(titles) == CAPS["findings"]
    assert titles[0] == "finding 31"  # the blocker at position 32
    assert titles[1:6] == [f"finding {i}" for i in range(20, 25)]  # the majors, in order
    minors = list(range(10)) + list(range(25, 31))
    assert titles[6:] == [f"finding {i}" for i in [*minors, 10, 11, 12]]
    assert summary == "Short. (7 further findings were dropped.)"


def test_blocker_at_position_26_survives() -> None:
    data = many(["cosmetic"] * 25 + ["blocker"])
    _, _, _, findings = parse(data)
    assert findings[0].title == "finding 25"
    assert findings[0].severity == "blocker"
    assert len(findings) == 25


def test_dropped_note_fits_inside_the_summary_cap() -> None:
    data = many(["minor"] * 30)
    data["summary"] = "M" * 5000
    summary, _, _, _ = parse(data)
    assert len(summary) == CAPS["summary"]
    assert summary.endswith(" (5 further findings were dropped.)")
    assert "…" in summary


def test_one_dropped_finding_is_still_counted() -> None:
    data = many(["minor"] * 26)
    assert parse(data)[0].endswith("(1 further finding was dropped.)")


def test_every_finding_is_validated_even_if_dropped() -> None:
    data = many(["minor"] * 30)
    data["findings"][29]["severity"] = "dreadful"
    with pytest.raises(ToolError, match="severity"):
        parse(data)
    data = many(["minor"] * 30)
    data["findings"][28]["screenshots"] = ["s404"]
    with pytest.raises(ToolError, match="s404"):
        parse(data)


def test_screenshots_are_deduplicated_and_capped() -> None:
    shots = {f"s{i}" for i in range(30)}
    data = good()
    ids = ["s5", "s1", "s5", "s2", "s1"] + [f"s{i}" for i in range(10, 30)]
    data["findings"][0]["screenshots"] = ids
    _, _, _, findings = parse_finish(data, source="a", device="d", screenshots=shots)
    assert findings[0].screenshots == ("s5", "s1", "s2", *tuple(f"s{i}" for i in range(10, 19)))
    assert len(findings[0].screenshots) == 12


def test_twelve_steps_are_kept_whole() -> None:
    data = good()
    data["findings"][0]["steps"] = [f"step {i}" for i in range(12)]
    assert len(parse(data)[3][0].steps) == 12
    assert parse(data)[3][0].steps[-1] == "step 11"


def test_single_line_fields_collapse_whitespace_and_multiline_keep_newlines() -> None:
    data = good()
    f = data["findings"][0]
    f["title"] = "  Two\n\nlines\t and   gaps\u00a0here \n"
    f["page"] = "/a\n/b"
    f["steps"] = ["Open\nthe\r\n cart", "ok"]
    f["expected"] = "line one\nline two"
    f["actual"] = "a\n\nb"
    data["summary"] = "x\ny"
    summary, _, _, findings = parse(data)
    got = findings[0]
    assert got.title == "Two lines and gaps here"
    assert got.page == "/a /b"
    assert got.steps == ("Open the cart", "ok")
    assert got.expected == "line one\nline two" and got.actual == "a\n\nb"
    assert summary == "x\ny"


@pytest.mark.parametrize(
    "char",
    [
        "\ud800",
        "\udfff",
        "\u202e",
        "\u2066",
        "\u2069",
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\U000e0041",
        "\U000e0000",
        "\U000e007f",
    ],
)
def test_surrogates_bidi_and_tag_characters_are_stripped(char: str) -> None:
    data = good()
    data["summary"] = f"a{char}b"
    data["findings"][0]["title"] = f"t{char}u"
    summary, _, _, findings = parse(data)
    assert summary == "ab" and findings[0].title == "tu"
    summary.encode("utf-8")


def test_zwj_emoji_survive() -> None:
    family = "\U0001f469\u200d\U0001f469\u200d\U0001f467"
    data = good()
    data["summary"] = f"Saw {family} on the page"
    assert parse(data)[0] == f"Saw {family} on the page"


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


def test_map_report_text_changes_free_text_and_nothing_else() -> None:
    data = {
        "summary": "s",
        "score": 5,
        "goal_reached": "partly",
        "findings": [
            {
                "title": "t",
                "severity": "minor",
                "page": "/p",
                "steps": ["a", 3],
                "expected": "e",
                "actual": "x",
                "screenshots": ["01"],
            },
            "not a finding",
        ],
    }
    before = repr(data)
    changed = map_report_text(data, str.upper)
    assert repr(data) == before  # a copy; the input is untouched
    assert changed["summary"] == "S"
    assert (changed["score"], changed["goal_reached"]) == (5, "partly")
    finding, odd = changed["findings"]
    assert odd == "not a finding"
    assert finding == {
        "title": "T",
        "severity": "minor",
        "page": "/P",
        "steps": ["A", 3],
        "expected": "E",
        "actual": "X",
        "screenshots": ["01"],
    }
    assert map_report_text({"summary": 1, "findings": "none"}, str.upper) == {
        "summary": 1,
        "findings": "none",
    }


def test_strip_invisible_removes_only_what_shows_nothing() -> None:
    hidden = "a\u200bb\u00adc\u034fd\ufe0fe\U000e0100f\u2060g\ufeffh"
    assert strip_invisible(hidden) == "abcdefgh"
    shown = "Tab\tnew line\n caf\u00e9 \u65e5\u672c \U0001f600 [password]"
    assert strip_invisible(shown) == shown
