"""The agent loop: Observe -> Reason -> Select tool -> Execute -> Interpret -> Report (S8, D5, D24).

Hand-written control flow (no agent framework, no SDK tool runner):

    observe   the harness seeds the first turn with the repository tree
    loop      ask the LLM; it states its reasoning and selects tool(s)
              execute every requested tool through the ToolRegistry
              send all results back in one turn (the LLM interprets them next turn)
              stop on finish_audit, on the step budget or on the time budget

The history is append-only: each LLM turn is stored exactly as returned (D20, D22).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from auditor.errors import AuditError
from auditor.findings import AuditState
from auditor.llm import (
    STOP_END,
    STOP_MALFORMED,
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    AssistantTurn,
    LLMClient,
    LLMResponse,
    ToolCall,
    ToolResult,
    ToolResultsTurn,
    Turn,
    UserTurn,
)
from auditor.prompts import (
    CONTINUE_NUDGE,
    FINAL_STEP_NOTE,
    MALFORMED_NUDGE,
    MAX_TOKENS_NUDGE,
    SYSTEM_PROMPT,
    budget_note,
    initial_message,
)
from auditor.registry import ToolRegistry

log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 40
DEFAULT_TIME_BUDGET = 30 * 60.0  # seconds; free-tier models can be slow
BUDGET_WARNING_STEPS = 3  # warn the model when this many steps are left
MAX_IDLE_NUDGES = 2  # replies without tool calls before we give up
MAX_REFUSALS = 2
SEED_LOG_COMMITS = 15  # recent commits shown in the first observation

TraceFn = Callable[..., None]


@dataclass
class LoopResult:
    state: AuditState
    steps: int
    stop_reason: str  # finished | step_budget | time_budget | idle
    history: list[Turn] = field(default_factory=list)
    elapsed: float = 0.0


class AuditAgent:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        state: AuditState,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        time_budget: float = DEFAULT_TIME_BUDGET,
        trace: TraceFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        if max_steps < 1:
            raise AuditError("max_steps must be at least 1")
        self.llm = llm
        self.registry = registry
        self.state = state
        self.max_steps = max_steps
        self.time_budget = time_budget
        self.trace: TraceFn = trace or (lambda event, **data: None)
        self.clock = clock
        self.system_prompt = system_prompt
        self.history: list[Turn] = []

    # -- public ----------------------------------------------------------------------------
    def run(self, repo_url: str, tree: str | None = None) -> LoopResult:
        """Run the audit. Without ``tree`` the harness makes the first observation itself:
        ``list_files`` and a short ``git_log``, executed through the registry so the files they
        show count as observed and the calls appear in the trace as step 0 (D30)."""
        started = self.clock()
        tools = self.registry.specs()
        self.trace(
            "loop_start",
            tools=[t.name for t in tools],
            system_prompt_chars=len(self.system_prompt),
            max_steps=self.max_steps,
            time_budget=self.time_budget,
        )
        history_text = ""
        if tree is None:
            tree = self._execute(0, ToolCall("observe-files", "list_files", {})).content
            history_text = self._execute(
                0, ToolCall("observe-history", "git_log", {"max_count": SEED_LOG_COMMITS})
            ).content
        self.history = [UserTurn(initial_message(repo_url, tree, history_text))]

        steps = 0
        idle = refusals = 0
        stop_reason = "step_budget"
        while steps < self.max_steps:
            steps += 1
            response = self.llm.complete(self.system_prompt, self.history, tools)
            self.history.append(AssistantTurn(response))
            self._trace_llm(steps, response)

            if response.tool_calls:
                idle = 0
                results = [self._execute(steps, call) for call in response.tool_calls]
                if self.state.finished:
                    self.history.append(ToolResultsTurn(results))
                    stop_reason = "finished"
                    break
                note = self._budget_note(steps, started)
                if note:
                    last = results[-1]
                    results[-1] = ToolResult(
                        last.call_id, last.name, last.content + note, last.is_error
                    )
                self.history.append(ToolResultsTurn(results))
                if note == FINAL_STEP_NOTE and self._over_time(started):
                    steps = self._final_step(steps, tools)
                    stop_reason = "finished" if self.state.finished else "time_budget"
                    break
                continue

            # No tool calls: decide how to nudge the model (Interpret step of the harness).
            if response.stop_reason == STOP_REFUSAL:
                refusals += 1
                if refusals >= MAX_REFUSALS:
                    raise AuditError(
                        f"The LLM provider declined the request repeatedly ({response.detail})."
                    )
                nudge = CONTINUE_NUDGE
            elif response.stop_reason == STOP_MAX_TOKENS:
                nudge = MAX_TOKENS_NUDGE
            elif response.stop_reason == STOP_MALFORMED:
                nudge = MALFORMED_NUDGE
            else:  # STOP_END or other: the model stopped talking without finishing
                idle += 1
                if idle > MAX_IDLE_NUDGES:
                    stop_reason = "idle"
                    break
                nudge = CONTINUE_NUDGE
            self.trace("nudge", step=steps, reason=response.stop_reason, text=nudge)
            self.history.append(UserTurn(nudge))
            if self._over_time(started):
                stop_reason = "time_budget"
                break

        elapsed = self.clock() - started
        self.trace(
            "end",
            stop_reason=stop_reason,
            steps=steps,
            findings=len(self.state.active_findings()),
            seconds=round(elapsed, 1),
        )
        return LoopResult(self.state, steps, stop_reason, self.history, elapsed)

    # -- helpers ---------------------------------------------------------------------------
    def _execute(self, step: int, call: Any) -> ToolResult:
        result, record = self.registry.dispatch(call)
        self.trace(
            "tool",
            step=step,
            name=record.name,
            args=record.args,
            is_error=record.is_error,
            seconds=record.seconds,
            output_chars=record.output_chars,
            truncated=record.truncated,
            output=result.content,
        )
        return result

    def _over_time(self, started: float) -> bool:
        return self.clock() - started >= self.time_budget

    def _budget_note(self, steps: int, started: float) -> str:
        steps_left = self.max_steps - steps
        if steps_left <= 1 or self._over_time(started):
            return FINAL_STEP_NOTE
        if steps_left <= BUDGET_WARNING_STEPS:
            return budget_note(steps_left)
        return ""

    def _final_step(self, steps: int, tools: list) -> int:
        """Time is up: give the model one last turn to record findings and finish."""
        steps += 1
        response = self.llm.complete(self.system_prompt, self.history, tools)
        self.history.append(AssistantTurn(response))
        self._trace_llm(steps, response)
        allowed = {"record_finding", "retract_finding", "finish_audit"}
        results = []
        for call in response.tool_calls:
            if call.name in allowed:
                results.append(self._execute(steps, call))
            else:
                results.append(
                    ToolResult(call.id, call.name, "ERROR: budget exhausted; not run", True)
                )
        if results:
            self.history.append(ToolResultsTurn(results))
        return steps

    def _trace_llm(self, step: int, response: LLMResponse) -> None:
        self.trace(
            "llm",
            step=step,
            model=response.model,
            stop_reason=response.stop_reason,
            detail=response.detail,
            text=response.text,
            reasoning=response.reasoning,
            tool_calls=[{"name": c.name, "args": c.input} for c in response.tool_calls],
            usage=response.usage.as_dict(),
        )
        if response.text:
            log.info("step %d: %s", step, response.text.replace("\n", " ")[:200])
        elif response.reasoning:
            log.info("step %d (thinking): %s", step, response.reasoning.replace("\n", " ")[:200])
        for c in response.tool_calls:
            log.info("step %d: -> %s(%s)", step, c.name, _short_args(c.input))
        if response.stop_reason not in (STOP_END,) and not response.tool_calls:
            log.info("step %d: stop=%s %s", step, response.stop_reason, response.detail)


def _short_args(args: dict[str, Any]) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return text if len(text) <= 120 else text[:120] + "..."
