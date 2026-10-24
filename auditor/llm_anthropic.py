"""Anthropic Messages API client (optional, paid provider - D20)."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import Any

import anthropic

from auditor.errors import AuditError
from auditor.llm import (
    STOP_END,
    STOP_MAX_TOKENS,
    STOP_OTHER,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    AssistantTurn,
    LLMError,
    LLMResponse,
    ToolCall,
    ToolResultsTurn,
    ToolSpec,
    Turn,
    Usage,
    UserTurn,
    env_effort,
    env_model,
)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT = 300.0  # seconds per request
DEFAULT_MAX_RETRIES = 5  # SDK retries 408/409/429/5xx/connection errors with backoff
FALLBACK_BETA = "server-side-fallback-2026-07-01"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_STOP_MAP = {
    "end_turn": STOP_END,
    "stop_sequence": STOP_END,
    "tool_use": STOP_TOOL_USE,
    "max_tokens": STOP_MAX_TOKENS,
    "refusal": STOP_REFUSAL,
}


def _block_type(block: Any) -> str:
    return block.get("type", "") if isinstance(block, dict) else getattr(block, "type", "")


def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def to_messages(history: list[Turn]) -> list[dict[str, Any]]:
    """Convert the neutral history into Anthropic ``messages``."""
    messages: list[dict[str, Any]] = []
    for turn in history:
        if isinstance(turn, UserTurn):
            messages.append({"role": "user", "content": turn.text})
        elif isinstance(turn, AssistantTurn):
            messages.append({"role": "assistant", "content": turn.response.content})
        elif isinstance(turn, ToolResultsTurn):
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in turn.results
                    ],
                }
            )
    return messages


def to_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
    ]


def parse_content(
    content: Sequence[Any], *, stop_reason: str, usage: Usage | None = None, model: str = ""
) -> LLMResponse:
    """Extract visible text and tool calls from Anthropic content blocks (SDK objects or dicts)."""
    texts: list[str] = []
    thoughts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        kind = _block_type(block)
        if kind == "text":
            texts.append(_get(block, "text") or "")
        elif kind == "thinking" and _get(block, "thinking"):
            thoughts.append(_get(block, "thinking"))
        elif kind == "tool_use":
            raw_input = _get(block, "input")
            calls.append(
                ToolCall(
                    id=_get(block, "id"),
                    name=_get(block, "name"),
                    input=raw_input if isinstance(raw_input, dict) else {},
                )
            )
    return LLMResponse(
        text="\n".join(t for t in texts if t).strip(),
        reasoning="\n".join(thoughts).strip(),
        tool_calls=calls,
        stop_reason=_STOP_MAP.get(stop_reason, STOP_OTHER),
        content=list(content),
        usage=usage or Usage(),
        model=model,
        detail=stop_reason,
    )


class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        *,
        effort: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        fallbacks: bool = True,
        client: Any = None,
    ) -> None:
        self.model = env_model(model, DEFAULT_MODEL)
        self.effort = env_effort(effort, DEFAULT_EFFORT)
        if self.effort not in VALID_EFFORTS:
            raise AuditError(f"Invalid effort {self.effort!r}; choose one of {VALID_EFFORTS}.")
        self.max_tokens = max_tokens
        # Server-side refusal fallback is not available for Haiku models.
        self.fallbacks = fallbacks and "haiku" not in self.model
        self.usage = Usage()
        if client is not None:
            self._client = client
            return
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise LLMError("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).")
        try:
            self._client = anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Could not create the Anthropic client: {exc}") from None

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": to_messages(history),
            "tools": to_tools(tools),
            "output_config": {"effort": self.effort},
            # Adaptive thinking is always on for this model; "summarized" returns a readable
            # summary of the reasoning for the trace (default would be empty text) (D28).
            "thinking": {"type": "adaptive", "display": "summarized"},
            # Automatic prompt caching: the growing history is read from cache each turn.
            "cache_control": {"type": "ephemeral"},
        }
        if self.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"

        started = time.monotonic()
        try:
            response = self._client.beta.messages.create(**params)
        except anthropic.AuthenticationError:
            raise LLMError(
                "Anthropic API key was rejected (401). Check ANTHROPIC_API_KEY."
            ) from None
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"Anthropic API permission denied (403): {exc.message}") from None
        except anthropic.NotFoundError:
            raise LLMError(
                f"Model {self.model!r} was not found (404). Check AUDITOR_MODEL."
            ) from None
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic API rejected the request (400): {exc.message}") from None
        except anthropic.RateLimitError:
            raise LLMError("Anthropic API rate limit persisted after retries (429).") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code} after retries.") from None
        except anthropic.APITimeoutError:
            raise LLMError("Anthropic API request timed out after retries.") from None
        except anthropic.APIConnectionError:
            raise LLMError("Could not connect to the Anthropic API (network error).") from None

        u = getattr(response, "usage", None)
        usage = Usage(
            calls=1,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            seconds=time.monotonic() - started,
        )
        self.usage.add(usage)
        return parse_content(
            list(response.content),
            stop_reason=response.stop_reason or "",
            usage=usage,
            model=getattr(response, "model", self.model),
        )
