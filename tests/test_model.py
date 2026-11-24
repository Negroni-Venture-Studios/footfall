"""The model clients: the request Claude is sent, and the scripted fake."""

import copy
import logging
from pathlib import Path
from typing import Any

import anthropic
import pytest

from footfall.tester.model import (
    FAKE_MODEL,
    MAX_TOKENS,
    AnthropicModel,
    FakeModel,
    ModelTurn,
    ScriptError,
    model_from_spec,
)
from footfall.tester.pricing import PRICES, PricingError, Usage
from footfall.tester.tools import TOOLS
from tests.fake_api import KEY, Api, message

SCRIPTS = Path(__file__).parent / "fixtures" / "scripts"
SYSTEM = "You are a tester."
FIRST: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": "Begin."}]}]


LOOK = {"type": "tool_use", "id": "toolu_01", "name": "look", "input": {}}


# ------------------------------------------------------------------ the request


async def test_request_shape(api: Api) -> None:
    api.answer(message([LOOK]))
    model = AnthropicModel(client=api.client())
    await model.turn(SYSTEM, TOOLS, FIRST)
    (request,) = api.requests
    assert request.method == "POST"
    assert request.url.path == "/v1/messages"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    (body,) = api.bodies()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == MAX_TOKENS == 16_000
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default"
    assert body["system"] == SYSTEM
    assert body["tools"] == TOOLS
    assert body["messages"] == FIRST
    # Adaptive thinking is the default and needs no parameter; sampling settings are refused.
    for absent in ("thinking", "temperature", "top_p", "top_k", "stream"):
        assert absent not in body


async def test_model_and_effort_are_sent_as_chosen(api: Api) -> None:
    api.answer(message([LOOK], model="claude-opus-5-5"))
    model = AnthropicModel("claude-opus-5-5", "high", client=api.client())
    await model.turn(SYSTEM, TOOLS, FIRST)
    (body,) = api.bodies()
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "high"}


async def test_content_round_trips_thinking_signature(api: Api) -> None:
    content: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": "", "signature": "EqQBCkYIBxgCKkDsig/+=="},
        {"type": "text", "text": "I will look.", "citations": None},
        # A field this code has never heard of goes back as it came.
        {**LOOK, "caller": {"type": "direct"}},
    ]
    api.answer(message(content))
    api.answer(message([{"type": "text", "text": "Done."}], stop="end_turn"))
    model = AnthropicModel(client=api.client())
    turn = await model.turn(SYSTEM, TOOLS, FIRST)
    assert turn.content == content
    assert turn.stop_reason == "tool_use"
    after = [
        *FIRST,
        {"role": "assistant", "content": turn.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01"}]},
    ]
    await model.turn(SYSTEM, TOOLS, after)
    second = api.bodies()[1]
    assert second["messages"][1] == {"role": "assistant", "content": content}


async def test_turn_reports_usage_and_the_model_that_answered(api: Api) -> None:
    fallback = {
        "type": "fallback",
        "from": {"model": "claude-sonnet-5-5"},
        "to": {"model": "claude-opus-5-5"},
        "trigger": {"type": "refusal", "category": "cyber"},
    }
    api.answer(message([fallback, LOOK], model="claude-opus-5-5"))
    model = AnthropicModel(client=api.client())
    turn = await model.turn(SYSTEM, TOOLS, FIRST)
    assert turn.model == "claude-opus-5-5"
    assert turn.usage == Usage(1200, 80, 1000, 200)
    assert turn.content[0] == fallback  # "from" keeps its name on the wire


async def test_missing_cache_counts_are_zero(api: Api) -> None:
    api.answer(
        message(
            [LOOK],
            usage={"input_tokens": 9, "output_tokens": 4, "cache_creation_input_tokens": None},
        )
    )
    model = AnthropicModel(client=api.client())
    turn = await model.turn(SYSTEM, TOOLS, FIRST)
    assert turn.usage == Usage(9, 4, 0, 0)


async def test_an_error_status_is_raised(api: Api) -> None:
    api.answer({"type": "error", "error": {"type": "overloaded_error", "message": "no"}}, 529)
    model = AnthropicModel(client=api.client())
    with pytest.raises(anthropic.APIStatusError) as caught:
        await model.turn(SYSTEM, TOOLS, FIRST)
    assert caught.value.status_code == 529


async def test_the_sdk_never_logs_the_conversation(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "Zq7-throwaway-Wx9"
    typed = [
        *FIRST,
        {
            "role": "assistant",
            "content": [{**LOOK, "name": "type", "input": {"text": secret}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01"}]},
    ]
    api.answer(message([LOOK]))
    await AnthropicModel(client=api.client()).turn(SYSTEM, TOOLS, typed)
    assert secret in api.requests[0].content.decode()  # the API is sent it
    sdk = [r for r in caplog.records if r.name.startswith("anthropic")]
    assert sdk, "the SDK's debug log should have been captured"
    assert secret not in caplog.text
    assert KEY not in caplog.text
    assert all(secret not in r.getMessage() for r in caplog.records)


def test_a_model_made_without_a_client_reads_nothing_until_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    model = AnthropicModel()
    assert (model.model, model.effort) == ("claude-sonnet-5-5", "medium")


# ------------------------------------------------------------------ the fake


async def test_fake_model_plays_its_script() -> None:
    fake = FakeModel(
        [
            [{"text": "Looking."}, {"tool": "look", "input": {}}],
            [{"text": "Nothing to do."}],
            [],
        ]
    )
    messages = copy.deepcopy(FIRST)
    first = await fake.turn(SYSTEM, TOOLS, messages)
    assert first == ModelTurn(
        [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "toolu_fake_001_1", "name": "look", "input": {}},
        ],
        "tool_use",
        Usage(0, 0, 0, 0),
        FAKE_MODEL,
    )
    messages.append({"role": "assistant", "content": first.content})
    second = await fake.turn(SYSTEM, TOOLS, messages)
    assert (second.content, second.stop_reason) == (
        [{"type": "text", "text": "Nothing to do."}],
        "end_turn",
    )
    third = await fake.turn(SYSTEM, TOOLS, messages)
    assert (third.content, third.stop_reason) == ([], "end_turn")
    # Each call is kept as it was sent, whatever happens to the list afterwards.
    assert fake.calls[0] == FIRST
    assert len(fake.calls[1]) == 2
    messages[0]["content"][0]["text"] = "changed"
    assert fake.calls[0] == FIRST
    with pytest.raises(ScriptError, match="the script has 3 turns, and turn 4 was asked for"):
        await fake.turn(SYSTEM, TOOLS, messages)


async def test_fake_model_answers_are_fresh_copies() -> None:
    fake = FakeModel(
        [[{"tool": "go", "input": {"to": "/a"}}], [{"tool": "go", "input": {"to": "/a"}}]]
    )
    first = await fake.turn(SYSTEM, TOOLS, FIRST)
    first.content[0]["input"]["to"] = "/changed"
    second = await fake.turn(SYSTEM, TOOLS, FIRST)
    assert second.content[0]["input"] == {"to": "/a"}
    assert second.content[0]["id"] != first.content[0]["id"]


async def test_fake_model_can_report_usage() -> None:
    fake = FakeModel([[{"tool": "look", "input": {}}]], usage=Usage(5, 6, 7, 8))
    turn = await fake.turn(SYSTEM, TOOLS, FIRST)
    assert turn.usage == Usage(5, 6, 7, 8)


@pytest.mark.parametrize(
    "script",
    [
        {"turns": []},
        [{"tool": "look", "input": {}}],
        [[{"tool": "look"}]],
        [[{"tool": "look", "input": []}]],
        [[{"tool": 3, "input": {}}]],
        [[{"text": 3}]],
        [[{"text": "a", "tool": "look"}]],
        [["look"]],
    ],
)
def test_a_malformed_script_is_refused(script: Any) -> None:
    with pytest.raises(ScriptError):
        FakeModel(script)


def test_a_script_file_is_read(tmp_path: Path) -> None:
    fake = FakeModel.from_file(SCRIPTS / "finishes.json")
    assert isinstance(fake, FakeModel)
    broken = tmp_path / "broken.json"
    broken.write_text("[[{", encoding="utf-8")
    with pytest.raises(ScriptError, match="cannot read the script"):
        FakeModel.from_file(broken)
    wrong = tmp_path / "wrong.json"
    wrong.write_text('[["look"]]', encoding="utf-8")
    with pytest.raises(ScriptError, match=r"wrong\.json: turn 1"):
        FakeModel.from_file(wrong)
    with pytest.raises(ScriptError, match="cannot read the script"):
        FakeModel.from_file(tmp_path / "missing.json")


# ------------------------------------------------------------------ choosing one


def test_model_from_spec() -> None:
    fake = model_from_spec(f"fake:{SCRIPTS / 'finishes.json'}")
    assert isinstance(fake, FakeModel)
    for name in PRICES:
        claude = model_from_spec(name)
        assert isinstance(claude, AnthropicModel)
        assert claude.model == name
        assert claude.effort == "medium"


@pytest.mark.parametrize(
    "spec", ["fake", "FAKE", "claude-sonnet-5", "gpt-5", "", " claude-opus-5-5"]
)
def test_model_from_spec_refuses_unpriced_models(spec: str) -> None:
    assert spec not in PRICES
    with pytest.raises(PricingError, match="no price for model"):
        model_from_spec(spec)


def test_fake_needs_a_path() -> None:
    with pytest.raises(ScriptError, match="needs the path of a script"):
        model_from_spec("fake:")


def test_fake_is_never_priced() -> None:
    assert FAKE_MODEL not in PRICES


async def test_a_model_made_without_a_client_makes_one_when_first_used(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = api.client()  # made before the class is replaced
    made: list[anthropic.AsyncAnthropic] = []

    def make() -> anthropic.AsyncAnthropic:
        made.append(client)
        return client

    monkeypatch.setattr(anthropic, "AsyncAnthropic", make)
    model = AnthropicModel()
    assert made == []
    api.answer(message([LOOK]))
    api.answer(message([LOOK]))
    await model.turn(SYSTEM, TOOLS, FIRST)
    await model.turn(SYSTEM, TOOLS, FIRST)
    assert len(made) == 1
    assert len(api.requests) == 2
