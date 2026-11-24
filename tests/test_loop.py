"""One persona's run: the loop between the model and the executor, on a real local Chromium."""

import base64
import copy
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, BrowserContext, ViewportSize

from footfall.manifest import Brief
from footfall.personas import DEVICES, PERSONAS, Device, Persona, load_devices, load_personas
from footfall.tester.guard import Guard
from footfall.tester.loop import (
    BEGIN,
    DECLINED,
    NUDGE,
    PAGE_STOPPED,
    REMINDER,
    Budget,
    run_persona,
)
from footfall.tester.model import NO_USAGE, AnthropicModel, FakeModel, ModelClient, ModelTurn
from footfall.tester.pricing import Price, Usage, cost_usd
from footfall.tester.report import PersonaReport
from footfall.tester.tools import TOOLS, Executor
from footfall.tester.trace import Trace
from tests.app_server import AppServer
from tests.fake_api import KEY, Api, message

SCRIPTS = Path(__file__).parent / "fixtures" / "scripts"
PHONE: ViewportSize = {"width": 390, "height": 664}
SECRET = 'Zq"7\\Wx9\\"kJ-v4Qp8'
BRIEF = Brief(name="explore", text="Look around the app.", start="signed-out", max_actions=None)
HOME = (
    "<!doctype html><title>Home</title><h1>Home</h1>"
    '<a href="/second" style="font-size:30px">Second</a>'
)
SECOND = "<!doctype html><title>Second</title><h1>Second page</h1>"

_DEVICES = load_devices(DEVICES)
_PERSONAS = load_personas(_DEVICES, PERSONAS)
PERSONA: Persona = _PERSONAS["sven"]
DEVICE: Device = _DEVICES[PERSONA.device]


@dataclass
class World:
    executor: Executor
    trace: Trace
    shots: Path


MakeWorld = Callable[..., Awaitable[World]]


@pytest.fixture
async def world(
    loopback_browser: Browser, app_server: AppServer, tmp_path: Path
) -> AsyncIterator[MakeWorld]:
    app_server.page("/", HOME)
    app_server.page("/second", SECOND)
    contexts: list[BrowserContext] = []

    async def make(path: str = "/", *, redact: frozenset[str] = frozenset()) -> World:
        context = await loopback_browser.new_context(service_workers="block", viewport=PHONE)
        contexts.append(context)
        guard = Guard(app_server.origin, frozenset())
        await guard.install(context)
        page = await context.new_page()
        await page.goto(app_server.origin + path)
        trace = Trace(tmp_path / f"trace-{len(contexts)}.jsonl")
        shots = tmp_path / f"shots-{len(contexts)}"
        executor = Executor(page, guard, trace, shots, touch=False, redact=redact)
        return World(executor, trace, shots)

    try:
        yield make
    finally:
        for context in contexts:
            await context.close()


async def run(
    w: World,
    model: ModelClient,
    *,
    budget: Budget | None = None,
    brief: Brief = BRIEF,
    **options: Any,
) -> PersonaReport:
    return await run_persona(
        persona=PERSONA,
        device=DEVICE,
        executor=w.executor,
        model=model,
        brief=brief,
        focus=None,
        app_name="Test app",
        budget=budget or Budget(),
        **options,
    )


def tool(name: str, **data: Any) -> dict[str, Any]:
    return {"tool": name, "input": data}


LOOK = [tool("look")]
TEXT = [{"text": "I am thinking about what to do."}]


def finish(*, summary: str = "All fine.", findings: list[dict[str, Any]] | None = None) -> Any:
    return [
        tool(
            "finish",
            summary=summary,
            score=8,
            goal_reached="yes",
            findings=findings or [],
        )
    ]


class Turns:
    """A model that gives the turns it was made with, and keeps every list it is sent."""

    def __init__(self, *turns: ModelTurn | Exception) -> None:
        self.turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []

    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        self.calls.append(copy.deepcopy(messages))
        answer = self.turns.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(
    *blocks: dict[str, Any],
    stop: str = "tool_use",
    usage: Usage = NO_USAGE,
    model: str = "claude-sonnet-5-5",
) -> ModelTurn:
    return ModelTurn(list(blocks), stop, usage, model)


def use(name: str, ident: str = "toolu_1", **data: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": ident, "name": name, "input": data}


FINISH_BLOCK = use("finish", "toolu_f", summary="Done.", score=5, goal_reached="no", findings=[])


def systems(messages: list[dict[str, Any]]) -> list[int]:
    return [i for i, m in enumerate(messages) if m["role"] == "system"]


def results(message: dict[str, Any]) -> list[dict[str, Any]]:
    assert message["role"] == "user"
    return [b for b in message["content"] if b["type"] == "tool_result"]


def free(model: str, usage: Usage) -> float:
    return 0.0


# ------------------------------------------------------------------ a whole run


async def test_finishes_with_findings(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel.from_file(SCRIPTS / "finishes.json")
    report = await run(w, model)
    assert report.status == "finished"
    assert report.reason == "the tester called finish"
    assert (report.persona, report.device) == (PERSONA.id, DEVICE.name)
    assert (report.score, report.goal_reached) == (6, "partly")
    assert report.summary == "Both pages load, but the second page has no way back."
    (finding,) = report.findings
    assert finding.title == "No way back from the second page"
    assert (finding.source, finding.device) == (PERSONA.id, DEVICE.name)
    assert finding.screenshots == ("02",)
    assert report.actions == 1  # the go; finish is not an action
    assert report.cost_usd == 0.0
    assert report.seconds >= 0
    assert [s.tool for s in w.trace.steps()] == ["go", "finish"]
    assert sorted(p.name for p in w.shots.iterdir()) == ["01.jpg", "02.jpg"]


async def test_the_first_message_is_the_first_look(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([finish()])
    await run(w, model)
    (first,) = model.calls[0]
    assert first["role"] == "user"
    screen, image, begin = first["content"]
    assert screen["type"] == "text"
    assert "Screenshot id: 01" in screen["text"]
    assert "Home" in screen["text"]
    assert image == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode((w.shots / "01.jpg").read_bytes()).decode("ascii"),
        },
    }
    assert begin == {"type": "text", "text": BEGIN}
    assert [s.tool for s in w.trace.steps()] == ["finish"]  # the first look is not traced


async def test_history_is_append_only(world: MakeWorld) -> None:
    w = await world()
    script = [
        [{"text": "Going on."}, tool("go", to="/second")],
        TEXT,
        [tool("tap", element="one", x=None, y=None)],
        [tool("look"), tool("look")],
        LOOK,
        finish(),
    ]
    model = FakeModel(script)
    report = await run(w, model, budget=Budget(max_actions=3))
    assert report.status == "finished"
    assert len(model.calls) == len(script)
    assert model.calls[-1][-1] == {"role": "system", "content": NUDGE}
    for before, after in zip(model.calls, model.calls[1:], strict=False):
        assert len(after) > len(before)
        for index, earlier in enumerate(before):
            assert after[index] == earlier, index
    # Every assistant message is the content the model gave, exactly.
    final = model.calls[-1]
    replies = [m["content"] for m in final if m["role"] == "assistant"]
    assert replies[0] == [
        {"type": "text", "text": "Going on."},
        {"type": "tool_use", "id": "toolu_fake_001_1", "name": "go", "input": {"to": "/second"}},
    ]
    assert len(replies) == len(script) - 1


async def test_a_tool_result_holds_the_text_and_the_screenshot(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([[tool("go", to="/second")], finish()])
    await run(w, model)
    (result,) = results(model.calls[1][-1])
    assert result["tool_use_id"] == "toolu_fake_001_0"
    assert "is_error" not in result
    text, image = result["content"]
    assert text["type"] == "text"
    assert "Went to " in text["text"]
    assert "Screenshot id: 02" in text["text"]
    assert image["source"]["media_type"] == "image/jpeg"
    jpeg = base64.b64decode(image["source"]["data"])
    assert jpeg == (w.shots / "02.jpg").read_bytes()


async def test_invalid_tool_input_returns_error_and_continues(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([[tool("tap", element="one", x=None, y=None)], finish()])
    report = await run(w, model)
    assert report.status == "finished"
    (result,) = results(model.calls[1][-1])
    assert result["is_error"] is True
    assert result["content"] == [
        {"type": "text", "text": "`element` must be a whole number or null"}
    ]
    assert report.actions == 1  # a call that went wrong still counts
    assert w.trace.steps()[0].error is not None


async def test_an_unknown_tool_gets_an_error_and_runs_nothing(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([[tool("click", at="the button")], finish()])
    report = await run(w, model)
    assert report.status == "finished"
    (result,) = results(model.calls[1][-1])
    assert result["tool_use_id"] == "toolu_fake_001_0"
    assert result["is_error"] is True
    assert 'There is no tool called "click"' in result["content"][0]["text"]
    assert report.actions == 0
    assert [s.tool for s in w.trace.steps()] == ["finish"]


async def test_two_tool_uses_in_one_turn_get_an_error_each(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([[tool("look"), tool("go", to="/second")], finish()])
    report = await run(w, model)
    assert report.status == "finished"
    first, second = results(model.calls[1][-1])
    assert (first["tool_use_id"], second["tool_use_id"]) == ("toolu_fake_001_0", "toolu_fake_001_1")
    assert first["is_error"] is second["is_error"] is True
    assert "one tool at a time" in first["content"][0]["text"]
    assert report.actions == 0
    assert [s.tool for s in w.trace.steps()] == ["finish"]


async def test_a_text_only_turn_gets_a_reminder(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([TEXT, finish()])
    report = await run(w, model)
    assert report.status == "finished"
    assert model.calls[1][-1] == {"role": "user", "content": [{"type": "text", "text": REMINDER}]}


async def test_an_empty_turn_is_not_appended(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([[], finish()])
    report = await run(w, model)
    assert report.status == "finished"
    roles = [m["role"] for m in model.calls[1]]
    assert roles == ["user", "user"]
    assert model.calls[1][-1]["content"] == [{"type": "text", "text": REMINDER}]


async def test_three_turns_in_a_row_without_a_tool_are_incomplete(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([TEXT, LOOK, TEXT, [tool("click")], TEXT, finish()])
    report = await run(w, model)
    # The look starts the count again; a call that was not run does not.
    assert report.status == "incomplete"
    assert report.reason == "the tester stopped using its tools"
    assert len(model.calls) == 5
    assert report.actions == 1


async def test_max_tokens_mid_tool_is_a_failed_turn(world: MakeWorld) -> None:
    w = await world()
    cut = answer({"type": "text", "text": "I will tap"}, use("tap", "toolu_cut"), stop="max_tokens")
    model = Turns(cut, answer(FINISH_BLOCK))
    report = await run(w, model)
    assert report.status == "finished"
    (result,) = results(model.calls[1][-1])
    assert result["tool_use_id"] == "toolu_cut"
    assert result["is_error"] is True
    assert "length limit" in result["content"][0]["text"]
    assert report.actions == 0
    assert [s.tool for s in w.trace.steps()] == ["finish"]


async def test_max_tokens_with_no_tool_gets_a_reminder(world: MakeWorld) -> None:
    w = await world()
    model = Turns(answer({"type": "text", "text": "Hmm"}, stop="max_tokens"), answer(FINISH_BLOCK))
    report = await run(w, model)
    assert report.status == "finished"
    assert model.calls[1][-1]["content"] == [{"type": "text", "text": REMINDER}]


# ------------------------------------------------------------------ the budget


async def test_action_budget_sends_system_nudge_then_incomplete(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel.from_file(SCRIPTS / "wanders.json")
    report = await run(w, model, budget=Budget(max_actions=3, grace_turns=2))
    assert report.status == "incomplete"
    assert report.reason == "the budget of 3 actions ran out before the tester called finish"
    assert report.actions == 3
    assert len(model.calls) == 5  # three actions, then two grace turns
    # The nudge follows the tool results of the third action, once.
    assert systems(model.calls[2]) == []
    assert model.calls[3][-1] == {"role": "system", "content": NUDGE}
    assert model.calls[3][-2]["role"] == "user"
    assert len(systems(model.calls[4])) == 1
    # Grace turns still run their tools.
    assert len(w.trace.steps()) == 5


async def test_finish_inside_grace_is_finished(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([LOOK, LOOK, LOOK, finish()])
    report = await run(w, model, budget=Budget(max_actions=2, grace_turns=2))
    assert report.status == "finished"
    assert report.actions == 2  # the look in the grace turns is not counted
    assert len(w.trace.steps()) == 4


async def test_text_turns_inside_grace_use_it_up(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([LOOK, TEXT, TEXT, finish()])
    report = await run(w, model, budget=Budget(max_actions=1, grace_turns=2))
    assert report.status == "incomplete"
    assert len(model.calls) == 3


async def test_no_grace_turns_ends_at_once(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([LOOK, LOOK])
    report = await run(w, model, budget=Budget(max_actions=1, grace_turns=0))
    assert report.status == "incomplete"
    assert len(model.calls) == 1
    assert systems(model.calls[0]) == []


async def test_cost_ceiling_stops(world: MakeWorld) -> None:
    w = await world()
    test_price = Price(input=0.30, output=0.0, cache_write=0.0, cache_read=0.0)

    def pricing(model: str, usage: Usage) -> float:
        assert model == "fake"
        return usage.input_tokens * test_price.input / 1_000_000

    model = FakeModel([LOOK] * 10, usage=Usage(1_000_000, 0, 0, 0))
    report = await run(w, model, budget=Budget(max_cost_usd=1.00), pricing=pricing)
    assert report.status == "incomplete"
    assert report.reason == "the cost ceiling of $1.00 was reached before the tester called finish"
    assert len(model.calls) == 6  # $1.20 after four turns, then two grace turns
    assert model.calls[4][-1] == {"role": "system", "content": NUDGE}
    assert report.cost_usd == pytest.approx(1.80)
    assert report.actions == 4


async def test_a_fake_model_costs_nothing(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([LOOK, finish()], usage=Usage(1_000_000, 1_000_000, 0, 0))
    report = await run(w, model)
    assert report.status == "finished"
    assert report.cost_usd == 0.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Slow:
    """A fake model that takes `seconds` of the clock's time to answer."""

    def __init__(self, model: FakeModel, clock: Clock, seconds: float) -> None:
        self.model, self.clock, self.seconds = model, clock, seconds

    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        self.clock.now += self.seconds
        return await self.model.turn(system, tools, messages)


async def test_time_limit_stops(world: MakeWorld) -> None:
    w = await world()
    clock = Clock()
    fake = FakeModel([LOOK] * 10)
    # Wrapped, the fake is no longer known to be free, so the test says it is.
    slow = Slow(fake, clock, 100.0)
    report = await run(w, slow, budget=Budget(max_seconds=250), clock=clock, pricing=free)
    assert report.status == "incomplete"
    assert report.reason == "the time limit of 250 s ran out before the tester called finish"
    assert len(fake.calls) == 5  # 300 s after three turns, then two grace turns
    assert fake.calls[3][-1] == {"role": "system", "content": NUDGE}
    assert report.seconds == 500.0


async def test_brief_max_actions_raises_budget(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel.from_file(SCRIPTS / "wanders.json")
    brief = Brief(name="long", text="Look around.", start="signed-out", max_actions=4)
    report = await run(w, model, budget=Budget(max_actions=2, grace_turns=1), brief=brief)
    assert report.status == "incomplete"
    assert report.reason == "the budget of 4 actions ran out before the tester called finish"
    assert report.actions == 4
    assert len(model.calls) == 5


def test_a_budget_must_be_positive() -> None:
    for wrong in (
        {"max_actions": 0},
        {"max_cost_usd": 0.0},
        {"max_seconds": -1.0},
        {"grace_turns": -1},
    ):
        with pytest.raises(ValueError, match="budget"):
            Budget(**wrong)


# ------------------------------------------------------------------ how runs end badly


async def test_refusal_is_incomplete(world: MakeWorld) -> None:
    w = await world()
    model = Turns(answer(use("look")), answer(stop="refusal"), answer(FINISH_BLOCK))
    report = await run(w, model)
    assert report.status == "incomplete"
    assert report.reason == DECLINED == "the model declined"
    assert len(model.calls) == 2
    assert report.actions == 1


async def test_a_context_window_that_is_full_is_incomplete(world: MakeWorld) -> None:
    w = await world()
    model = Turns(answer(stop="model_context_window_exceeded"), answer(FINISH_BLOCK))
    report = await run(w, model)
    assert report.status == "incomplete"
    assert report.reason == "the conversation outgrew the model's context window"


async def test_exception_is_failed_without_detail_leak(
    world: MakeWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    w = await world(redact=frozenset({SECRET}))
    model = Turns(answer(use("look")), RuntimeError(f"the key {KEY} and the password {SECRET}"))
    report = await run(w, model)
    assert report.status == "failed"
    assert report.reason == "RuntimeError"
    assert report.actions == 1
    dumped = json.dumps(report.to_json())
    for secret in (KEY, SECRET):
        assert secret not in dumped
        assert secret not in caplog.text
        assert all(secret not in r.getMessage() for r in caplog.records)


async def test_a_page_that_stops_answering_fails_the_persona(
    world: MakeWorld, app_server: AppServer
) -> None:
    app_server.page(
        "/throws",
        "<button>Hello</button><script>window.getComputedStyle = function () {"
        " throw new Error('no'); };</script>",
    )
    w = await world("/throws")
    model = FakeModel([finish()])
    report = await run(w, model)
    assert report.status == "failed"
    assert report.reason == PAGE_STOPPED == "the page stopped answering"
    assert model.calls == []


async def test_a_fallback_is_priced_as_the_model_that_answered(world: MakeWorld) -> None:
    w = await world()
    usage = Usage(10_000, 2_000, 5_000, 40_000)
    model = Turns(
        answer(use("look"), usage=usage, model="claude-opus-5-5"),
        answer(FINISH_BLOCK, usage=usage, model="claude-sonnet-5-5"),
    )
    report = await run(w, model)
    assert report.status == "finished"
    expected = cost_usd("claude-opus-5-5", usage) + cost_usd("claude-sonnet-5-5", usage)
    assert report.cost_usd == pytest.approx(expected)
    assert cost_usd("claude-opus-5-5", usage) != cost_usd("claude-sonnet-5-5", usage)


async def test_an_unpriced_model_fails_the_persona(world: MakeWorld) -> None:
    w = await world()
    model = Turns(answer(use("look"), usage=Usage(1, 1, 0, 0), model="claude-sonnet-5"))
    report = await run(w, model)
    assert report.status == "failed"
    assert report.reason == 'the API answered as model "claude-sonnet-5", which has no price here'


async def test_the_report_is_redacted(world: MakeWorld) -> None:
    w = await world(redact=frozenset({SECRET}))
    # Plain, and split by characters the report strips, which the executor cannot see past.
    hidden = SECRET[:4] + "‮" + SECRET[4:]
    finding = {
        "title": f"Password {SECRET} shown",
        "severity": "major",
        "page": f"/x/{hidden}",
        "steps": [f"Type {SECRET}", f"Read {hidden}"],
        "expected": f"No {hidden}",
        "actual": f"Saw {SECRET}",
        "screenshots": ["01"],
    }
    model = FakeModel([finish(summary=f"I typed {hidden} and {SECRET}.", findings=[finding])])
    report = await run(w, model)
    assert report.status == "finished"
    dumped = json.dumps(report.to_json(), ensure_ascii=False)
    for run_ in {SECRET[i : i + 4] for i in range(len(SECRET) - 3)}:
        assert run_ not in dumped, run_
    assert report.summary == "I typed [password] and [password]."
    (found,) = report.findings
    assert found.title == "Password [password] shown"
    assert found.steps == ("Type [password]", "Read [password]")
    assert (found.page, found.expected, found.actual) == (
        "/x/[password]",
        "No [password]",
        "Saw [password]",
    )


async def test_the_executor_exposes_its_secrets_read_only(world: MakeWorld) -> None:
    w = await world(redact=frozenset({"short", "a-longer-secret", ""}))
    assert w.executor.secrets == ("a-longer-secret", "short")
    with pytest.raises(AttributeError):
        w.executor.secrets = ()  # type: ignore[misc]


# ------------------------------------------------------------------ through the real client


async def test_a_whole_run_through_the_messages_api(world: MakeWorld, api: Api) -> None:
    w = await world()
    thinking = {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"}
    api.answer(
        message([thinking, {"type": "tool_use", "id": "toolu_a", "name": "look", "input": {}}])
    )
    api.answer(
        message(
            [
                thinking,
                {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "finish",
                    "input": {
                        "summary": "Fine.",
                        "score": 9,
                        "goal_reached": "yes",
                        "findings": [],
                    },
                },
            ]
        )
    )
    model = AnthropicModel(client=api.client())
    report = await run(w, model, budget=Budget(max_actions=1))
    assert report.status == "finished"
    first, second = api.bodies()
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["messages"][1]["content"][0] == thinking
    # The nudge goes to the API as a system message after the tool results.
    assert second["messages"][-1] == {"role": "system", "content": NUDGE}
    assert second["messages"][-2]["content"][0]["type"] == "tool_result"
    assert first["tools"] == second["tools"] == TOOLS
    usage = Usage(1200, 80, 1000, 200)
    assert report.cost_usd == pytest.approx(2 * cost_usd("claude-sonnet-5-5", usage))


async def test_api_status_error_is_failed_without_the_key(
    world: MakeWorld, api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    w = await world(redact=frozenset({SECRET}))
    # The tester types the password, so the next request carries it; the API's error
    # echoes the key and the password back.
    api.answer(
        message([{"type": "tool_use", "id": "toolu_t", "name": "type", "input": {"text": SECRET}}])
    )
    api.answer(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": f"bad {KEY} {SECRET}"},
        },
        400,
    )
    report = await run(w, AnthropicModel(client=api.client()))
    assert report.status == "failed"
    assert report.reason == "BadRequestError (status 400)"
    typed = api.bodies()[1]["messages"][1]["content"][0]
    assert typed["input"] == {"text": SECRET}  # the API is sent it
    assert any(r.name.startswith("anthropic") for r in caplog.records)
    dumped = json.dumps(report.to_json())
    places = [dumped, caplog.text, *(r.getMessage() for r in caplog.records)]
    for place in places:
        assert KEY not in place
        for run_ in {SECRET[i : i + 4] for i in range(len(SECRET) - 3)}:
            assert run_ not in place, (run_, place[:200])


async def test_a_refused_report_is_not_an_action(world: MakeWorld) -> None:
    w = await world()
    broken = [tool("finish", summary="Done.", score=11, goal_reached="yes", findings=[])]
    model = FakeModel([broken, broken, broken, finish()])
    report = await run(w, model)
    (result,) = results(model.calls[1][-1])
    assert result["is_error"] is True
    assert "`score` must be a whole number from 0 to 10" in result["content"][0]["text"]
    # Three refused reports in a row are three turns in which nothing was done.
    assert report.status == "incomplete"
    assert report.reason == "the tester stopped using its tools"
    assert report.actions == 0


async def test_a_tool_call_without_an_id_fails_the_persona(world: MakeWorld) -> None:
    w = await world()
    model = Turns(answer({"type": "tool_use", "name": "look", "input": {}}))
    report = await run(w, model)
    assert report.status == "failed"
    assert report.reason == "the model's answer held a tool call without an id"


async def test_an_empty_turn_after_the_nudge_leaves_the_nudge_last(world: MakeWorld) -> None:
    w = await world()
    model = FakeModel([LOOK, [], finish()])
    report = await run(w, model, budget=Budget(max_actions=1, grace_turns=2))
    assert report.status == "finished"
    # A system message must be last or followed by the assistant, so nothing is added.
    assert model.calls[1][-1] == {"role": "system", "content": NUDGE}
    assert model.calls[2] == model.calls[1]
