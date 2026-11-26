"""The model clients: the request Claude is sent, and the scripted fake."""

import copy
import logging
from pathlib import Path
from typing import Any

import anthropic
import pytest

from footfall.tester import model as model_module
from footfall.tester.model import (
    CLIENT_RETRIES,
    CLIENT_TIMEOUT,
    FAKE_MODEL,
    MAX_TOKENS,
    REQUESTABLE,
    AnthropicModel,
    FakeModel,
    ModelSpecError,
    ModelTurn,
    ScriptError,
    make_client,
    model_from_spec,
)
from footfall.tester.pricing import PRICES, Usage
from footfall.tester.tools import TOOLS
from tests.fake_api import KEY, Api, iteration, message

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
    api.answer(message([LOOK]))
    model = AnthropicModel(client=api.client())
    turn = await model.turn(SYSTEM, TOOLS, FIRST)
    assert turn.model == "claude-sonnet-5-5"
    assert turn.usage == Usage(1200, 80, 1000, 200)
    assert turn.charges == (("claude-sonnet-5-5", Usage(1200, 80, 1000, 200)),)


async def test_a_fallback_turn_is_charged_for_every_attempt(api: Api) -> None:
    # Sonnet 5.5 declines part way through, and Sonnet 5 serves the reply. The top-level
    # usage covers only the attempt that served it.
    fallback = {
        "type": "fallback",
        "from": {"model": "claude-sonnet-5-5"},
        "to": {"model": "claude-sonnet-5"},
        "trigger": {"type": "refusal", "category": "cyber"},
    }
    api.answer(
        message(
            [fallback, LOOK],
            model="claude-sonnet-5",
            usage={
                "input_tokens": 30,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 9000,
            },
            iterations=[
                iteration("message", "claude-sonnet-5-5", (10, 20, 5000, 0), one_hour=1200),
                iteration("fallback_message", "claude-sonnet-5", (30, 40, 0, 9000)),
            ],
        )
    )
    model = AnthropicModel(client=api.client())
    turn = await model.turn(SYSTEM, TOOLS, FIRST)
    assert turn.model == "claude-sonnet-5"
    assert turn.usage == Usage(30, 40, 0, 9000)
    assert turn.charges == (
        ("claude-sonnet-5-5", Usage(10, 20, 5000, 0, cache_write_1h_input_tokens=1200)),
        ("claude-sonnet-5", Usage(30, 40, 0, 9000)),
    )
    assert turn.content[0] == fallback  # "from" keeps its name on the wire


async def test_a_sticky_fallback_turn_is_charged_as_the_model_that_answered(api: Api) -> None:
    # After a fallback, later turns go straight to the fallback model for a while. Such a
    # turn has one fallback_message entry in usage.iterations and no fallback block.
    api.answer(
        message(
            [LOOK],
            model="claude-sonnet-5",
            iterations=[iteration("fallback_message", "claude-sonnet-5", (1200, 80, 1000, 200))],
        )
    )
    turn = await AnthropicModel(client=api.client()).turn(SYSTEM, TOOLS, FIRST)
    assert [block["type"] for block in turn.content] == ["tool_use"]
    assert turn.model == "claude-sonnet-5"
    assert turn.charges == (("claude-sonnet-5", Usage(1200, 80, 1000, 200)),)


async def test_cache_writes_the_split_does_not_cover_are_five_minute_writes(api: Api) -> None:
    def usage(total: int, five: int, hour: int) -> dict[str, Any]:
        return {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_creation_input_tokens": total,
            "cache_read_input_tokens": 0,
            "cache_creation": {
                "ephemeral_5m_input_tokens": five,
                "ephemeral_1h_input_tokens": hour,
            },
        }

    api.answer(message([LOOK], usage=usage(1000, 300, 200)))
    api.answer(message([LOOK], usage=usage(100, 300, 200)))
    model = AnthropicModel(client=api.client())
    # 500 tokens the split leaves out are kept, at the five-minute rate.
    first = await model.turn(SYSTEM, TOOLS, FIRST)
    assert first.usage == Usage(1, 1, 1000, 0, cache_write_1h_input_tokens=200)
    # A split larger than the total is believed over the total.
    second = await model.turn(SYSTEM, TOOLS, FIRST)
    assert second.usage == Usage(1, 1, 500, 0, cache_write_1h_input_tokens=200)


async def test_us_only_inference_is_read_for_every_attempt(api: Api) -> None:
    body = message(
        [LOOK],
        model="claude-sonnet-5",
        iterations=[
            iteration("message", "claude-sonnet-5-5", (1, 2, 0, 0)),
            iteration("fallback_message", "claude-sonnet-5", (3, 4, 0, 0)),
        ],
    )
    body["usage"]["inference_geo"] = "us"
    api.answer(body)
    turn = await AnthropicModel(client=api.client()).turn(SYSTEM, TOOLS, FIRST)
    assert turn.usage.inference_geo == "us"
    assert [usage.inference_geo for _, usage in turn.charges] == ["us", "us"]


def test_a_model_must_be_requestable_to_be_asked_for() -> None:
    for name in ("claude-sonnet-5", "claude-opus-4-8", "claude-haiku-5-5"):
        with pytest.raises(ModelSpecError, match="cannot run model"):
            AnthropicModel(name)


async def test_one_hour_cache_writes_are_counted_apart(api: Api) -> None:
    api.answer(
        message(
            [LOOK],
            usage={
                "input_tokens": 5,
                "output_tokens": 6,
                "cache_creation_input_tokens": 700,
                "cache_read_input_tokens": 8,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 400,
                    "ephemeral_1h_input_tokens": 300,
                },
            },
        )
    )
    turn = await AnthropicModel(client=api.client()).turn(SYSTEM, TOOLS, FIRST)
    assert turn.usage == Usage(5, 6, 700, 8, cache_write_1h_input_tokens=300)


async def test_an_attempt_without_a_model_is_charged_as_the_model_asked_for(api: Api) -> None:
    # With no sign of a fallback, the model asked for is the one that ran.
    api.answer(message([LOOK], iterations=[iteration("message", None, (1, 2, 3, 4))]))
    turn = await AnthropicModel(client=api.client()).turn(SYSTEM, TOOLS, FIRST)
    assert turn.charges == (("claude-sonnet-5-5", Usage(1, 2, 3, 4)),)


@pytest.mark.parametrize(
    ("asked", "answered", "served_entry", "charged"),
    [
        # Opus 5 is dearer than Opus 5.5 in every kind of token.
        ("claude-opus-5-5", "claude-opus-5", True, "claude-opus-5"),
        # Sonnet 5's cache reads cost twice Sonnet 5.5's, and this attempt is mostly reads.
        ("claude-sonnet-5-5", "claude-sonnet-5", False, "claude-sonnet-5"),
        # A model with no price will be charged at the dearest rates, so it is the dearer.
        ("claude-sonnet-5-5", "claude-opus-4-7", True, "claude-opus-4-7"),
    ],
)
async def test_after_a_fallback_an_attempt_without_a_model_is_charged_at_the_dearer(
    api: Api, asked: str, answered: str, served_entry: bool, charged: str
) -> None:
    entries = [iteration("message", None, (1_000, 10, 0, 1_000_000))]
    if served_entry:
        entries.append(iteration("fallback_message", answered, (5, 6, 7, 8)))
    api.answer(message([LOOK], model=answered, iterations=entries))
    turn = await AnthropicModel(asked, client=api.client()).turn(SYSTEM, TOOLS, FIRST)
    assert turn.charges[0] == (charged, Usage(1_000, 10, 0, 1_000_000))


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
    assert model._client is None


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
    assert REQUESTABLE == ("claude-sonnet-5-5", "claude-opus-5-5")
    assert set(REQUESTABLE) <= set(PRICES)
    for name in REQUESTABLE:
        claude = model_from_spec(name)
        assert isinstance(claude, AnthropicModel)
        assert claude.model == name
        assert claude.effort == "medium"


@pytest.mark.parametrize(
    "spec", ["fake", "FAKE", "claude-sonnet-4-6", "gpt-5", "", " claude-opus-5-5"]
)
def test_model_from_spec_refuses_unknown_models(spec: str) -> None:
    assert spec not in PRICES
    with pytest.raises(ModelSpecError, match="cannot run model"):
        model_from_spec(spec)


@pytest.mark.parametrize("spec", ["claude-sonnet-5", "claude-opus-5", "claude-opus-4-8"])
def test_model_from_spec_refuses_a_model_priced_only_as_a_fallback(spec: str) -> None:
    assert spec in PRICES
    with pytest.raises(ModelSpecError, match="cannot run model"):
        model_from_spec(spec)


def test_fake_needs_a_path() -> None:
    with pytest.raises(ScriptError, match="needs the path of a script"):
        model_from_spec("fake:")
    assert issubclass(ScriptError, ModelSpecError)


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

    monkeypatch.setattr(model_module, "make_client", make)
    model = AnthropicModel()
    assert made == []
    api.answer(message([LOOK]))
    api.answer(message([LOOK]))
    await model.turn(SYSTEM, TOOLS, FIRST)
    await model.turn(SYSTEM, TOOLS, FIRST)
    assert len(made) == 1
    assert len(api.requests) == 2


async def test_the_one_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    # Set in the environment, this would put prompts and replies on every span.
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    bare = anthropic.AsyncAnthropic()
    client = make_client()
    try:
        assert bare.open_telemetry["traces"]["content_mode"] == "content"
        assert client.open_telemetry["traces"]["content_mode"] == "metadata_only"
        assert client.timeout == CLIENT_TIMEOUT == 300.0
        assert client.max_retries == CLIENT_RETRIES == 2
    finally:
        await bare.close()
        await client.close()


@pytest.mark.parametrize(
    ("setting", "enabled"), [(None, True), ("true", True), ("false", False), ("FALSE", False)]
)
async def test_an_operator_can_turn_tracing_off(
    monkeypatch: pytest.MonkeyPatch, setting: str | None, enabled: bool
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    if setting is None:
        monkeypatch.delenv("ANTHROPIC_OPEN_TELEMETRY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY", setting)
    client = make_client()
    try:
        traces = client.open_telemetry["traces"]
        assert traces["enabled"] is enabled
        assert traces["content_mode"] == "metadata_only"
    finally:
        await client.close()
