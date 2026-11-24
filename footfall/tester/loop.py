"""One persona's run: the loop between the model and the executor.

Footfall makes the first look itself and sends it as the first user message. Each turn
after that appends the model's content unchanged, runs its one tool call through the
executor, and appends the result with the screenshot it led to. Nothing already in the
conversation is ever edited or removed (AGENTS.md rule 5), and the executor is handed a
copy of each tool input, so that not even it can reach the history.

What the model writes, and what the pages it reads say, can steer only which tool it
calls with what input, and when it calls `finish`. The budget, the count of actions and
the end of the run are decided here. A turn that breaks the rules (no tool call, more than
one, an unknown tool, a call cut off at the token limit) gets a reply that keeps the
conversation valid and counts as a turn in which nothing was done.

Each persona has a budget of actions, cost and time, checked after every turn. Once one
runs out, a system message tells the tester to finish, and it has `grace_turns` more turns
to do so. In those turns a `finish` is honoured, and any other tool still runs but adds
nothing to the count of actions in the report.

A run never raises, except to be cancelled. Its report says why it ended, and a failure
is given by a fixed reason or the exception's class alone, because an exception's message
may hold page text, the API's reply or the password the tester typed (AGENTS.md rule 7).
The report's text is redacted with the executor's secrets after it is cleaned, so that a
password split by characters the cleaning removes is caught too.
"""

import base64
import copy
import logging
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

import anthropic

from footfall.manifest import Brief
from footfall.personas import Device, Persona
from footfall.tester.model import FakeModel, ModelClient, ModelTurn
from footfall.tester.pricing import PricingError, Usage, cost_usd
from footfall.tester.prompt import system_prompt
from footfall.tester.report import (
    CAPS,
    PAGE_CAP,
    Finding,
    PersonaReport,
    Status,
    clean_text,
    parse_finish,
)
from footfall.tester.screen import CaptureError, quote
from footfall.tester.tools import REDACTED, TOOLS, Executor, ToolOutcome

__all__ = [
    "BEGIN",
    "DECLINED",
    "MAX_IDLE_TURNS",
    "NUDGE",
    "PAGE_STOPPED",
    "REMINDER",
    "Budget",
    "Pricing",
    "run_persona",
]

Pricing = Callable[[str, Usage], float]

MAX_IDLE_TURNS: Final = 3  # turns in a row in which nothing was done, before the budget is spent
BEGIN: Final = "That is the app as you find it. Begin your test, one action at a time."
NUDGE: Final = "Your budget is spent. Call finish now with what you found."
REMINDER: Final = (
    "Your last turn called no tool. Keep testing with your tools, one action at a time, "
    "or call finish with what you found."
)
ONE_AT_A_TIME: Final = (
    "Call one tool at a time. None of these calls was run; make the one you want first "
    "again, on its own."
)
CUT_OFF: Final = (
    "Your turn reached its length limit before this tool call was complete, so it was not "
    "run. Make the call again, more briefly."
)
FINISHED: Final = "the tester called finish"
DECLINED: Final = "the model declined"
CONTEXT_FULL: Final = "the conversation outgrew the model's context window"
IDLE: Final = "the tester stopped using its tools"
PAGE_STOPPED: Final = "the page stopped answering"
LOG = logging.getLogger(__name__)

_TOOL_NAMES: Final = frozenset(t["name"] for t in TOOLS)
_UNCAPPED: Final = sys.maxsize


@dataclass(frozen=True)
class Budget:
    """What one persona may spend: actions, dollars and seconds, then turns to finish in."""

    max_actions: int = 30
    max_cost_usd: float = 1.00
    max_seconds: float = 600
    grace_turns: int = 2

    def __post_init__(self) -> None:
        if self.max_actions < 1 or self.max_cost_usd <= 0 or self.max_seconds <= 0:
            raise ValueError("a budget needs at least one action, some money and some time")
        if self.grace_turns < 0:
            raise ValueError("a budget cannot have fewer than no grace turns")


class _Unpriced(Exception):
    """The API answered as a model that has no price, so the run cannot be held to its cost."""

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model


def _free(model: str, usage: Usage) -> float:
    return 0.0


def _text(text: str) -> dict[str, Any]:
    # The API refuses an empty text block.
    return {"type": "text", "text": text or "(nothing to report)"}


def _screen_blocks(outcome: ToolOutcome) -> list[dict[str, Any]]:
    blocks = [_text(outcome.text)]
    if outcome.screen is not None:
        data = base64.b64encode(outcome.screen.jpeg).decode("ascii")
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
            }
        )
    return blocks


def _result(tool_id: str, outcome: ToolOutcome) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": _screen_blocks(outcome),
    }
    if outcome.error:
        block["is_error"] = True
    return block


def _refused(tool_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [_text(text)],
        "is_error": True,
    }


class _MalformedTurn(Exception):
    """The model's answer holds a tool call without an id, which no result can answer."""


def _tool_uses(turn: ModelTurn) -> list[tuple[str, Mapping[str, Any]]]:
    uses: list[tuple[str, Mapping[str, Any]]] = []
    for block in turn.content:
        if isinstance(block, Mapping) and block.get("type") == "tool_use":
            ident = block.get("id")
            if not isinstance(ident, str) or not ident:
                raise _MalformedTurn
            uses.append((ident, block))
    return uses


class _Run:
    def __init__(
        self,
        *,
        persona: Persona,
        device: Device,
        executor: Executor,
        model: ModelClient,
        brief: Brief,
        focus: str | None,
        app_name: str,
        budget: Budget,
        pricing: Pricing,
        clock: Callable[[], float],
    ) -> None:
        self.persona = persona
        self.device = device
        self.executor = executor
        self.model = model
        self.budget = budget
        self.max_actions = (
            brief.max_actions if brief.max_actions is not None else budget.max_actions
        )
        self.pricing = pricing
        self.clock = clock
        self.system = system_prompt(persona, device, brief, app_name, focus)
        self.messages: list[dict[str, Any]] = []
        self.started = clock()
        self.actions = 0
        self.cost = 0.0
        self.idle = 0
        self.spent: str | None = None  # why the budget ran out, once it has
        self.grace_used = 0

    # ------------------------------------------------------------------ the loop

    async def go(self) -> PersonaReport:
        first = await self.executor.first_look()
        self.messages.append({"role": "user", "content": [*_screen_blocks(first), _text(BEGIN)]})
        while True:
            turn = await self.model.turn(self.system, TOOLS, list(self.messages))
            self._charge(turn)
            if turn.content:
                # Appended as it came, and never touched again.
                self.messages.append({"role": "assistant", "content": turn.content})
            if turn.stop_reason == "refusal":
                return self.report("incomplete", DECLINED)
            if turn.stop_reason == "model_context_window_exceeded":
                return self.report("incomplete", CONTEXT_FULL)
            acted, finish = await self._answer(turn)
            if finish is not None:
                return self._finished(finish)
            ended = self._after(acted)
            if ended is not None:
                return ended

    def _charge(self, turn: ModelTurn) -> None:
        try:
            self.cost += self.pricing(turn.model, turn.usage)
        except PricingError:
            raise _Unpriced(turn.model) from None

    async def _answer(self, turn: ModelTurn) -> tuple[bool, dict[str, Any] | None]:
        """Reply to the turn. Returns whether an action was done, and an accepted report."""
        uses = _tool_uses(turn)
        if not uses:
            # A system message must be last or followed by the assistant's turn. After an
            # empty turn the nudge is still last, and the same request goes again.
            if self.messages[-1]["role"] != "system":
                self.messages.append({"role": "user", "content": [_text(REMINDER)]})
            return False, None
        problem: str | None = None
        name = uses[0][1].get("name")
        if turn.stop_reason == "max_tokens":
            problem = CUT_OFF
        elif len(uses) > 1:
            problem = ONE_AT_A_TIME
        elif not isinstance(name, str) or name not in _TOOL_NAMES:
            problem = (
                f"There is no tool called {quote(str(name), 40)}, so nothing was run. The "
                "tools are look, tap, type, scroll, go and finish."
            )
        if problem is not None:
            self.messages.append(
                {"role": "user", "content": [_refused(ident, problem) for ident, _ in uses]}
            )
            return False, None
        ident, block = uses[0]
        tool = str(name)  # one of the tool names, checked above
        # A copy, so that nothing the executor does can reach the conversation.
        data: Any = copy.deepcopy(block.get("input"))
        outcome = await self.executor.run(tool, data)
        if outcome.finish is not None:
            return False, outcome.finish
        self.messages.append({"role": "user", "content": [_result(ident, outcome)]})
        if tool == "finish":
            return False, None  # a report that was refused is not an action
        if self.spent is None:
            self.actions += 1
        return True, None

    def _after(self, acted: bool) -> PersonaReport | None:
        """Check the budget after a turn, and end the run or nudge the tester as it says."""
        if self.spent is not None:
            self.grace_used += 1
            if self.grace_used >= self.budget.grace_turns:
                return self.report("incomplete", self.spent)
            return None
        self.idle = 0 if acted else self.idle + 1
        self.spent = self._limit()
        if self.spent is not None:
            if self.budget.grace_turns == 0:
                return self.report("incomplete", self.spent)
            self.messages.append({"role": "system", "content": NUDGE})
            return None
        if self.idle >= MAX_IDLE_TURNS:
            return self.report("incomplete", IDLE)
        return None

    def _limit(self) -> str | None:
        after = "before the tester called finish"
        if self.actions >= self.max_actions:
            return f"the budget of {self.max_actions} actions ran out {after}"
        if self.cost >= self.budget.max_cost_usd:
            return f"the cost ceiling of ${self.budget.max_cost_usd:.2f} was reached {after}"
        if self.clock() - self.started >= self.budget.max_seconds:
            return f"the time limit of {self.budget.max_seconds:g} s ran out {after}"
        return None

    # ------------------------------------------------------------------ the report

    def _redact(self, text: str) -> str:
        for secret in self.executor.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def _bare(self, value: Any) -> Any:
        """`value` with the characters the report strips removed, then redacted."""
        if isinstance(value, str):
            return self._redact(clean_text(value, _UNCAPPED))
        if isinstance(value, list):
            return [self._bare(item) for item in value]
        if isinstance(value, dict):
            return {key: self._bare(item) for key, item in value.items()}
        return value

    def _field(self, text: str, cap: int, *, single_line: bool = False) -> str:
        return clean_text(self._redact(text), cap, single_line=single_line)

    def _finished(self, finish: dict[str, Any]) -> PersonaReport:
        summary, score, reached, findings = parse_finish(
            self._bare(finish),
            source=self.persona.id,
            device=self.device.name,
            screenshots=self.executor.screenshot_ids,
        )
        return self.report(
            "finished",
            FINISHED,
            score=score,
            goal_reached=reached,
            summary=self._field(summary, CAPS["summary"]),
            findings=[self._finding(f) for f in findings],
        )

    def _finding(self, finding: Finding) -> Finding:
        return replace(
            finding,
            title=self._field(finding.title, CAPS["title"], single_line=True),
            page=self._field(finding.page, PAGE_CAP, single_line=True),
            steps=tuple(self._field(s, CAPS["step"], single_line=True) for s in finding.steps),
            expected=self._field(finding.expected, CAPS["expected"]),
            actual=self._field(finding.actual, CAPS["actual"]),
        )

    def report(self, status: Status, reason: str, **found: Any) -> PersonaReport:
        report = PersonaReport(
            persona=self.persona.id,
            device=self.device.name,
            status=status,
            reason=reason,
            score=found.get("score"),
            goal_reached=found.get("goal_reached"),
            summary=found.get("summary", ""),
            findings=found.get("findings", []),
            actions=self.actions,
            cost_usd=self.cost,
            seconds=self.clock() - self.started,
        )
        LOG.info("persona %s on %s: %s, %s", self.persona.id, self.device.name, status, reason)
        return report


async def run_persona(
    *,
    persona: Persona,
    device: Device,
    executor: Executor,
    model: ModelClient,
    brief: Brief,
    focus: str | None,
    app_name: str,
    budget: Budget,
    pricing: Pricing | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PersonaReport:
    """Run one persona to its report.

    `brief.max_actions`, when set, replaces the budget's count of actions. Each turn is
    priced by `pricing`, which defaults to the price table, and charges nothing for a
    `FakeModel`. `clock` is a monotonic clock in seconds. When the page stops answering
    the persona fails, and the caller must close its browser context.
    """
    run = _Run(
        persona=persona,
        device=device,
        executor=executor,
        model=model,
        brief=brief,
        focus=focus,
        app_name=app_name,
        budget=budget,
        pricing=pricing or (_free if isinstance(model, FakeModel) else cost_usd),
        clock=clock,
    )
    try:
        return await run.go()
    except CaptureError:
        return run.report("failed", PAGE_STOPPED)
    except _Unpriced as error:
        return run.report(
            "failed", f"the API answered as model {quote(error.model, 60)}, which has no price here"
        )
    except _MalformedTurn:
        return run.report("failed", "the model's answer held a tool call without an id")
    except anthropic.APIStatusError as error:
        # The status and the class; never the body or the headers, which can echo the request.
        return run.report("failed", f"{type(error).__name__} (status {error.status_code})")
    except Exception as error:  # noqa: BLE001  # any failure ends this persona, not the run
        return run.report("failed", type(error).__name__)
