"""Provider-neutral LLM layer (D1, D20, D22).

The agent loop (S8) talks to an ``LLMClient``: one method, ``complete(system, history, tools)``.

- ``history`` is a provider-neutral list of turns: ``UserTurn`` (text), ``AssistantTurn``
  (a previous ``LLMResponse``, whose raw provider content is resent unchanged) and
  ``ToolResultsTurn`` (results of the tools the model asked for).
- ``tools`` is a list of ``ToolSpec`` (name, description, JSON schema).
- Each provider module converts these into its own request format and parses the reply into
  an ``LLMResponse`` (visible text, tool calls, normalised stop reason, raw content, usage).

Providers: ``gemini`` (default, free tier - ``auditor/llm_gemini.py``) and ``anthropic``
(paid - ``auditor/llm_anthropic.py``). Only official provider SDKs are used; the agent loop,
tool dispatch and stopping logic are our own code (D0).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from auditor.errors import AuditError

DEFAULT_PROVIDER = "gemini"
PROVIDERS = ("gemini", "anthropic")

# Normalised stop reasons used by the agent loop.
STOP_END = "end_turn"  # model finished without asking for tools
STOP_TOOL_USE = "tool_use"  # model asked for one or more tools
STOP_MAX_TOKENS = "max_tokens"  # reply cut off by the output limit
STOP_REFUSAL = "refusal"  # provider safety system declined
STOP_MALFORMED = "malformed_tool_call"  # model produced an unparseable tool call
STOP_OTHER = "other"


class LLMError(AuditError):
    """The LLM could not be reached or rejected the request (fatal, exit code 1)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema of the arguments (type: object)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0
    retries: int = 0

    def add(self, other: Usage) -> None:
        for name, value in other.__dict__.items():
            setattr(self, name, getattr(self, name) + value)

    def as_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


@dataclass
class LLMResponse:
    text: str  # visible text (the agent's stated reasoning between tool calls)
    tool_calls: list[ToolCall]
    stop_reason: str  # one of the STOP_* constants
    content: Any  # raw provider content, resent unchanged in later turns (append-only)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    detail: str = ""  # provider-specific stop detail (e.g. refusal category)
    reasoning: str = ""  # provider's thought summary, for the trace only (D28)


@dataclass
class UserTurn:
    text: str


@dataclass
class AssistantTurn:
    response: LLMResponse


@dataclass
class ToolResultsTurn:
    results: list[ToolResult]


Turn = UserTurn | AssistantTurn | ToolResultsTurn


class LLMClient(Protocol):
    provider: str
    model: str
    usage: Usage

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMResponse: ...


def create_llm(
    provider: str | None = None, model: str | None = None, *, effort: str | None = None
) -> LLMClient:
    """Build the configured client (``AUDITOR_PROVIDER``, default ``gemini``)."""
    name = (provider or os.environ.get("AUDITOR_PROVIDER", "").strip() or DEFAULT_PROVIDER).lower()
    if name == "gemini":
        from auditor.llm_gemini import GeminiLLM

        return GeminiLLM(model, effort=effort)
    if name == "anthropic":
        from auditor.llm_anthropic import AnthropicLLM

        return AnthropicLLM(model, effort=effort)
    raise AuditError(f"Unknown AUDITOR_PROVIDER {name!r}; choose one of {PROVIDERS}.")


def env_effort(effort: str | None, default: str) -> str:
    return (effort or os.environ.get("AUDITOR_EFFORT", "").strip() or default).lower()


def env_model(model: str | None, default: str) -> str:
    return model or os.environ.get("AUDITOR_MODEL", "").strip() or default


# -- scripted client for tests ----------------------------------------------------------------
Script = LLMResponse | Callable[[list[Turn]], LLMResponse]


class FakeLLM:
    """Deterministic ``LLMClient`` for tests: returns scripted responses in order.

    Each script item is an ``LLMResponse`` or a callable receiving the history (so a test can
    react to tool results). ``calls`` records what the agent sent on each turn.
    """

    provider = "fake"

    def __init__(self, script: Sequence[Script], model: str = "fake-model") -> None:
        self.script = list(script)
        self.model = model
        self.usage = Usage()
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append({"system": system, "history": list(history), "tools": list(tools)})
        if not self.script:
            raise LLMError("FakeLLM script exhausted")
        item = self.script.pop(0)
        response = item(list(history)) if callable(item) else item
        self.usage.add(Usage(calls=1))
        return response


_fake_ids = iter(range(1, 10**9))


def call(name: str, input: dict[str, Any] | None = None, id: str | None = None) -> ToolCall:
    """Build a ToolCall for scripted responses."""
    return ToolCall(id=id or f"call_{next(_fake_ids)}", name=name, input=input or {})


def fake_response(*items: ToolCall | str, stop_reason: str | None = None) -> LLMResponse:
    """Build an LLMResponse from text strings and ``call(...)`` tool calls."""
    texts = [i for i in items if isinstance(i, str)]
    calls = [i for i in items if isinstance(i, ToolCall)]
    if stop_reason is None:
        stop_reason = STOP_TOOL_USE if calls else STOP_END
    return LLMResponse(
        text="\n".join(texts),
        tool_calls=calls,
        stop_reason=stop_reason,
        content={"texts": texts, "calls": [c.__dict__ for c in calls]},
        usage=Usage(calls=1),
        model="fake-model",
    )
