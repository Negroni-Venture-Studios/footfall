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
typed, so that one record is dropped (AGENTS.md rule 7).
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

from footfall.tester.pricing import PRICES, PricingError, Usage

__all__ = [
    "BETAS",
    "DEFAULT_MODEL",
    "FAKE_MODEL",
    "MAX_TOKENS",
    "AnthropicModel",
    "FakeModel",
    "ModelClient",
    "ModelTurn",
    "ScriptError",
    "model_from_spec",
]

DEFAULT_MODEL: Final = "claude-sonnet-5-5"
DEFAULT_EFFORT: Final = "medium"
MAX_TOKENS: Final = 16_000
BETAS: Final = ("server-side-fallback-2026-07-01",)
FAKE_MODEL: Final = "fake"
NO_USAGE: Final = Usage(0, 0, 0, 0)


@dataclass(frozen=True)
class ModelTurn:
    """One answer from the model.

    `content` is the assistant's blocks as plain dicts, to be appended to the conversation
    unchanged. `model` is the model that answered, which a fallback can make different
    from the one asked for, and is the one whose price applies.
    """

    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage
    model: str


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


class AnthropicModel:
    """Claude, through `client.beta.messages.create`.

    Without a `client`, one is made on the first turn, and the SDK finds the API key in
    the environment as it always does.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self._client = client

    async def turn(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
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
        usage = response.usage
        return ModelTurn(
            content=[block.to_dict() for block in response.content],
            stop_reason=response.stop_reason or "",
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            ),
            model=response.model,
        )


# ------------------------------------------------------------------ the fake


class ScriptError(ValueError):
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
    """`fake:<path>` gives a `FakeModel` from that script; a priced model id gives Claude."""
    if spec.startswith("fake:"):
        path = spec.removeprefix("fake:")
        if not path:
            raise ScriptError("`fake:` needs the path of a script, as in fake:script.json")
        return FakeModel.from_file(Path(path))
    if spec not in PRICES:
        known = ", ".join(sorted(PRICES))
        raise PricingError(
            f"no price for model {spec!r}; use one of {known}, or fake:<path> for a script"
        )
    return AnthropicModel(spec)
