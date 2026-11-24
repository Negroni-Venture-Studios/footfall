"""The model clients: Claude through the Messages API, and a scripted fake for tests.

`AnthropicModel` sends each turn with the parameters phase 1 fixes: Claude Sonnet 5.5 by
default, at medium effort, with adaptive thinking (no `thinking` parameter is sent), the
server-side refusal fallback, automatic prompt caching and one tool call at a time. A
forced `tool_choice` is refused by Sonnet 5.5, so the choice is always `auto`.

The conversation is append-only (AGENTS.md rule 5): a turn's content is every block the
API returned, as `block.to_dict()` gives it, so that thinking signatures and any field
this code does not know about go back exactly as they came.

The SDK holds the API key; nothing here reads it. The SDK logs each request's options at
debug level, and those hold the whole conversation, including any password the tester has
typed, so that one record is dropped (AGENTS.md rule 7). For the same reason, the client
`make_client` builds never puts content on an OpenTelemetry span.
"""

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast

import anthropic
from anthropic.types.beta import (
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaToolUnionParam,
)

from footfall.tester.pricing import Usage

__all__ = [
    "BETAS",
    "CLIENT_RETRIES",
    "CLIENT_TIMEOUT",
    "DEFAULT_MODEL",
    "FAKE_MODEL",
    "MAX_TOKENS",
    "REQUESTABLE",
    "AnthropicModel",
    "Charges",
    "FakeModel",
    "ModelClient",
    "ModelSpecError",
    "ModelTurn",
    "ScriptError",
    "make_client",
    "model_from_spec",
]

DEFAULT_MODEL: Final = "claude-sonnet-5-5"
DEFAULT_EFFORT: Final = "medium"
MAX_TOKENS: Final = 16_000
BETAS: Final = ("server-side-fallback-2026-07-01",)
FAKE_MODEL: Final = "fake"
CLIENT_TIMEOUT: Final = 300.0  # seconds for one request; the loop also bounds each turn
CLIENT_RETRIES: Final = 2
NO_USAGE: Final = Usage(0, 0, 0, 0)
# The models a run may ask for. The price table also holds their fallbacks, which serve a
# turn the model asked for declines, but which Footfall never asks for itself.
REQUESTABLE: Final = ("claude-sonnet-5-5", "claude-opus-5-5")

# What one turn costs: each attempt's model and usage.
Charges = tuple[tuple[str, Usage], ...]


@dataclass(frozen=True)
class ModelTurn:
    """One answer from the model.

    `content` is the assistant's blocks as plain dicts, to be appended to the conversation
    unchanged. `model` is the model that answered, which a fallback can make different
    from the one asked for, and `usage` is what that model's attempt used.

    `charges` is every attempt the turn took, each with its own model and usage: with a
    fallback, the model asked for may run and decline before another serves the reply, and
    both are billed. Empty means the turn is charged as `usage` on `model`.
    """

    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage
    model: str
    charges: Charges = ()


class ModelClient(Protocol):
    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn: ...


# ------------------------------------------------------------------ the API


class _NoRequestOptions(logging.Filter):
    """Drop the SDK's debug record of a request's options, which holds the conversation."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (isinstance(record.msg, str) and record.msg.startswith("Request options"))


_SDK_LOG: Final = logging.getLogger("anthropic._base_client")
if not any(isinstance(f, _NoRequestOptions) for f in _SDK_LOG.filters):
    _SDK_LOG.addFilter(_NoRequestOptions())


def make_client() -> anthropic.AsyncAnthropic:
    """The client Footfall reaches the Messages API through, and the only one it builds.

    The SDK finds the API key in the environment. A request gives up after `CLIENT_TIMEOUT`
    seconds and is tried `CLIENT_RETRIES` more times. Spans carry metadata only: the
    OpenTelemetry option is set here, so the SDK ignores the ANTHROPIC_OPEN_TELEMETRY_*
    variables, one of which would put every prompt and reply, typed password included, on
    a span (the SDK's OpenTelemetry notes, read on 24 November 2026).
    """
    return anthropic.AsyncAnthropic(
        timeout=CLIENT_TIMEOUT,
        max_retries=CLIENT_RETRIES,
        open_telemetry={"traces": {"content_mode": "metadata_only"}},
    )


class AnthropicModel:
    """Claude, through `client.beta.messages.create`.

    Without a `client`, `make_client` makes one on the first turn.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        if model not in REQUESTABLE:
            known = ", ".join(REQUESTABLE)
            raise ModelSpecError(f"cannot run model {model!r}; use one of {known}")
        self.model = model
        self.effort = effort
        self._client = client

    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        if self._client is None:
            self._client = make_client()
        response = await self._client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=cast(list[BetaToolUnionParam], tools),
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=cast(list[BetaMessageParam], messages),
            output_config=cast(BetaOutputConfigParam, {"effort": self.effort}),
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=list(BETAS),
        )
        geo = response.usage.inference_geo
        usage = _usage(response.usage, geo)
        return ModelTurn(
            content=[block.to_dict() for block in response.content],
            stop_reason=response.stop_reason or "",
            usage=usage,
            model=response.model,
            charges=self._charges(response.usage.iterations, response.model, usage, geo),
        )

    def _charges(self, iterations: object, answered: str, usage: Usage, geo: str | None) -> Charges:
        """Each attempt the turn took, each to be priced at its own model's rates.

        With a fallback, the top-level usage covers only the attempt that served the reply,
        and `usage.iterations` lists every attempt: a "message" entry the model asked for
        declined, and a "fallback_message" entry served the turn (Anthropic's refusals and
        fallback page, https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback,
        checked on 24 November 2026). Some declines before any output are not billed, but
        every attempt is charged here: a cost ceiling should overcharge rather than
        undercharge. An attempt that names no model is charged as the model asked for.

        Without iterations, the turn is the top-level usage on the model that answered,
        which after a fallback can go on being the fallback model for later turns.
        """
        if not isinstance(iterations, list) or not iterations:
            return ((answered, usage),)
        return tuple(
            (getattr(entry, "model", None) or self.model, _usage(entry, geo))
            for entry in iterations
        )


def _count(source: object, name: str) -> int:
    value = getattr(source, name, None)
    return value if isinstance(value, int) else 0


def _usage(source: object, geo: str | None) -> Usage:
    """The token counts of a response's usage or of one attempt; a count left out is 0.

    `cache_creation_input_tokens` is the total of the five-minute and one-hour cache writes
    in `cache_creation`. Tokens the split leaves out are five-minute writes, as is the
    whole total when there is no split; a split that adds up to more than the total is
    believed over the total. No written token is ever dropped.
    """
    split = getattr(source, "cache_creation", None)
    one_hour = _count(split, "ephemeral_1h_input_tokens")
    written = max(
        _count(source, "cache_creation_input_tokens"),
        _count(split, "ephemeral_5m_input_tokens") + one_hour,
    )
    return Usage(
        input_tokens=_count(source, "input_tokens"),
        output_tokens=_count(source, "output_tokens"),
        cache_creation_input_tokens=written,
        cache_read_input_tokens=_count(source, "cache_read_input_tokens"),
        cache_write_1h_input_tokens=one_hour,
        inference_geo=geo,
    )


# ------------------------------------------------------------------ the fake


class ModelSpecError(ValueError):
    """A model spec names nothing Footfall can run."""


class ScriptError(ModelSpecError):
    """A fake model's script is malformed, or has no turn left."""


def _is_tool(block: object) -> bool:
    return (
        isinstance(block, dict)
        and set(block) == {"tool", "input"}
        and isinstance(block["tool"], str)
        and isinstance(block["input"], dict)
    )


def _is_text(block: object) -> bool:
    return isinstance(block, dict) and set(block) == {"text"} and isinstance(block["text"], str)


def _check_script(script: object) -> list[list[dict[str, Any]]]:
    if not isinstance(script, list):
        raise ScriptError("a script is a list of turns")
    for number, turn in enumerate(script, start=1):
        if not isinstance(turn, list):
            raise ScriptError(f"turn {number}: a turn is a list of blocks")
        for block in turn:
            if not (_is_tool(block) or _is_text(block)):
                raise ScriptError(
                    f'turn {number}: each block is {{"tool": name, "input": {{...}}}} '
                    'or {"text": "..."}'
                )
    return cast(list[list[dict[str, Any]]], copy.deepcopy(script))


class FakeModel:
    """A model that answers from a script, for tests and for trying Footfall without a key.

    Each turn of the script is a list of blocks, `{"tool": name, "input": {...}}` or
    `{"text": "..."}`. A turn with a tool stops for `tool_use`, and any other for
    `end_turn`. `calls` holds a deep copy of every `messages` list the fake was sent. Its
    model name is "fake", which has no price: the loop charges nothing for a fake.

    A script for a whole run must end with a `finish` call. A run that asks for a turn
    past the end gets a `ScriptError`, which fails the persona.
    """

    def __init__(self, script: list[list[dict[str, Any]]], *, usage: Usage = NO_USAGE) -> None:
        self._script = _check_script(script)
        self._usage = usage
        self.calls: list[list[dict[str, Any]]] = []

    @classmethod
    def from_file(cls, path: Path) -> "FakeModel":
        try:
            script = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise ScriptError(f"{path}: cannot read the script: {error}") from error
        try:
            return cls(script)
        except ScriptError as error:
            raise ScriptError(f"{path}: {error}") from None

    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        self.calls.append(copy.deepcopy(messages))
        number = len(self.calls)
        if number > len(self._script):
            raise ScriptError(
                f"the script has {len(self._script)} turns, and turn {number} was asked for"
            )
        content: list[dict[str, Any]] = []
        for index, block in enumerate(self._script[number - 1]):
            if "tool" in block:
                content.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_fake_{number:03d}_{index}",
                        "name": block["tool"],
                        "input": copy.deepcopy(block["input"]),
                    }
                )
            else:
                content.append({"type": "text", "text": block["text"]})
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return ModelTurn(content, stop, self._usage, FAKE_MODEL)


# ------------------------------------------------------------------ choosing one


def model_from_spec(spec: str) -> ModelClient:
    """`fake:<path>` gives a `FakeModel` from that script; a model in `REQUESTABLE`, Claude."""
    if spec.startswith("fake:"):
        path = spec.removeprefix("fake:")
        if not path:
            raise ScriptError("`fake:` needs the path of a script, as in fake:script.json")
        return FakeModel.from_file(Path(path))
    if spec not in REQUESTABLE:
        known = ", ".join(REQUESTABLE)
        raise ModelSpecError(
            f"cannot run model {spec!r}; use one of {known}, or fake:<path> for a script"
        )
    return AnthropicModel(spec)
