"""Google Gemini client via the official ``google-genai`` SDK (default, free tier - D22)."""

from __future__ import annotations

import logging
import os
import random
import re
import time
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import errors, types

from auditor.errors import AuditError
from auditor.llm import (
    STOP_END,
    STOP_MALFORMED,
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

log = logging.getLogger(__name__)

# Free-tier models, chosen by measurement (D22, D25): gemini-3.8-flash was overloaded (60-130 s per
# call) and gemini-3.6-flash allows only 20 requests/day - less than one audit. The Flash-Lite
# models are fast (~1 s) and each has its own daily quota.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
# Models to try in order when the current one is overloaded (503), out of daily quota, or not
# found (404, renamed/retired). gemini-3.6-flash is last: its free quota is only 20 requests/day.
FALLBACK_MODELS = ("gemini-3.1-flash-lite", "gemini-3.6-flash")
SWITCH_AFTER_503 = 3  # consecutive 503s before switching to the next fallback model
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_OUTPUT_TOKENS = 16000  # includes thinking tokens
DEFAULT_TIMEOUT_MS = 300_000
MAX_ATTEMPTS = 8  # per request, for 429 (free-tier per-minute quota) and 5xx
MAX_BACKOFF = 90.0  # seconds
# Our effort names -> Gemini thinking levels.
_THINKING_LEVELS = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
_REFUSAL_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "RECITATION",
    "IMAGE_SAFETY",
}


def to_contents(history: list[Turn]) -> list[types.Content]:
    """Convert the neutral history into Gemini ``contents``.

    Model turns are resent exactly as received so thought signatures attached to their parts
    are preserved.
    """
    contents: list[types.Content] = []
    for turn in history:
        if isinstance(turn, UserTurn):
            contents.append(types.Content(role="user", parts=[types.Part(text=turn.text)]))
        elif isinstance(turn, AssistantTurn):
            contents.append(turn.response.content)
        elif isinstance(turn, ToolResultsTurn):
            parts = [
                types.Part(
                    function_response=types.FunctionResponse(
                        id=r.call_id if not r.call_id.startswith("gen_") else None,
                        name=r.name,
                        response={"error": r.content} if r.is_error else {"output": r.content},
                    )
                )
                for r in turn.results
            ]
            contents.append(types.Content(role="user", parts=parts))
    return contents


def to_tools(tools: list[ToolSpec]) -> list[types.Tool]:
    if not tools:
        return []
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t.name, description=t.description, parameters_json_schema=t.parameters
                )
                for t in tools
            ]
        )
    ]


def parse_response(response: Any, *, usage: Usage, model: str) -> LLMResponse:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        feedback = getattr(response, "prompt_feedback", None)
        reason = str(getattr(feedback, "block_reason", "") or "no candidates returned")
        return LLMResponse(
            text="",
            tool_calls=[],
            stop_reason=STOP_REFUSAL,
            content=types.Content(role="model", parts=[types.Part(text="")]),
            usage=usage,
            model=model,
            detail=reason,
        )

    candidate = candidates[0]
    content = candidate.content or types.Content(role="model", parts=[])
    texts: list[str] = []
    thoughts: list[str] = []
    calls: list[ToolCall] = []
    for idx, part in enumerate(content.parts or []):
        if part.function_call is not None:
            fc = part.function_call
            calls.append(
                ToolCall(
                    id=fc.id or f"gen_{idx}_{fc.name}",
                    name=fc.name or "",
                    input=dict(fc.args or {}),
                )
            )
        elif part.text and part.thought:
            thoughts.append(part.text)
        elif part.text:
            texts.append(part.text)

    finish = getattr(candidate, "finish_reason", None)
    finish_name = getattr(finish, "name", None) or str(finish or "")
    if calls:
        stop = STOP_TOOL_USE
    elif finish_name in ("STOP", "FINISH_REASON_UNSPECIFIED", ""):
        stop = STOP_END
    elif finish_name == "MAX_TOKENS":
        stop = STOP_MAX_TOKENS
    elif finish_name in _REFUSAL_REASONS:
        stop = STOP_REFUSAL
    elif finish_name in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"):
        stop = STOP_MALFORMED
    else:
        stop = STOP_OTHER
    if content.role is None:
        content.role = "model"
    return LLMResponse(
        text="\n".join(texts).strip(),
        reasoning="\n".join(thoughts).strip(),
        tool_calls=calls,
        stop_reason=stop,
        content=content,
        usage=usage,
        model=model,
        detail=finish_name,
    )


def _transport_error_types() -> tuple[type[BaseException], ...]:
    """Base classes for network-level failures of the HTTP libraries the SDK may use
    (connection reset, read/write errors, protocol errors, timeouts)."""
    found: list[type[BaseException]] = []
    for module_name in ("httpx", "httpx2"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        cls = getattr(module, "TransportError", None)
        if isinstance(cls, type):
            found.append(cls)
    return tuple(found)


_TRANSPORT_ERRORS = _transport_error_types()


def is_transport_error(exc: BaseException) -> bool:
    if _TRANSPORT_ERRORS and isinstance(exc, _TRANSPORT_ERRORS):
        return True
    name = type(exc).__name__.lower()  # fallback for wrapped/unknown transport errors
    return any(word in name for word in ("timeout", "connect", "readerror", "protocolerror"))


def _error_details(exc: errors.APIError) -> list[dict[str, Any]]:
    body = getattr(exc, "details", None) or getattr(exc, "response_json", None) or {}
    if isinstance(body, dict):
        body = body.get("error", body)
        details = body.get("details", [])
        return details if isinstance(details, list) else []
    return []


def retry_delay(exc: errors.APIError) -> float | None:
    """Server-suggested wait from a RetryInfo detail (e.g. ``"retryDelay": "34s"``)."""
    for d in _error_details(exc):
        delay = d.get("retryDelay") if isinstance(d, dict) else None
        if isinstance(delay, str) and (m := re.match(r"^([\d.]+)s$", delay)):
            return float(m.group(1))
    return None


def is_daily_quota(exc: errors.APIError) -> bool:
    """Free-tier *daily* quota exhausted: waiting a minute will not help."""
    for d in _error_details(exc):
        for violation in (d.get("violations") or []) if isinstance(d, dict) else []:
            quota_id = str(violation.get("quotaId", ""))
            if "PerDay" in quota_id or "per_day" in quota_id.lower():
                return True
    return False


class GeminiLLM:
    provider = "gemini"

    def __init__(
        self,
        model: str | None = None,
        *,
        effort: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        fallback_models: tuple[str, ...] = FALLBACK_MODELS,
    ) -> None:
        self.model = env_model(model, DEFAULT_MODEL)
        self.fallback_models = [m for m in fallback_models if m != self.model]
        self.model_switches: list[str] = []
        effort_name = env_effort(effort, DEFAULT_EFFORT)
        if effort_name not in _THINKING_LEVELS:
            raise AuditError(
                f"Invalid effort {effort_name!r}; choose one of {tuple(_THINKING_LEVELS)}."
            )
        self.effort = effort_name
        self.thinking_level = _THINKING_LEVELS[effort_name]
        self.max_output_tokens = max_output_tokens
        self.usage = Usage()
        self._sleep = sleep
        if client is not None:
            self._client = client
            return
        api_key = (
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com "
                "and add it to .env (see .env.example)."
            )
        try:
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    timeout=DEFAULT_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=1),  # we retry ourselves
                ),
            )
        except Exception as exc:  # SDK raises various types on bad configuration
            raise LLMError(f"Could not create the Gemini client: {exc}") from None

    def _switch_model(self, reason: str) -> None:
        previous, self.model = self.model, self.fallback_models.pop(0)
        self.model_switches.append(f"{previous} -> {self.model} ({reason})")
        log.warning("Gemini model %s %s; switching to %s", previous, reason, self.model)

    def _config(self, system: str, tools: list[ToolSpec]) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system,
            tools=to_tools(tools),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # include_thoughts returns short thought summaries, recorded in the trace (D28).
            thinking_config=types.ThinkingConfig(
                thinking_level=self.thinking_level, include_thoughts=True
            ),
            max_output_tokens=self.max_output_tokens,
        )

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMResponse:
        contents = to_contents(history)
        config = self._config(system, tools)
        started = time.monotonic()
        retries = 0
        overloaded = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                break
            except errors.ClientError as exc:
                code = getattr(exc, "code", 0)
                message = str(getattr(exc, "message", "") or exc)
                if code == 429:
                    if is_daily_quota(exc):
                        if self.fallback_models:
                            self._switch_model("daily quota used up")
                            retries += 1
                            continue
                        raise LLMError(
                            "Gemini free-tier daily quota exhausted (429) for every configured "
                            "model. Quotas reset at midnight Pacific time; or use another key."
                        ) from None
                    if attempt == MAX_ATTEMPTS:
                        raise LLMError("Gemini rate limit persisted after retries (429).") from None
                    wait = retry_delay(exc) or min(MAX_BACKOFF, 5 * 2 ** (attempt - 1))
                    log.warning("Gemini rate limited (429); waiting %.0fs", wait)
                elif "api key not valid" in message.lower() or code in (401, 403):
                    raise LLMError(
                        "Gemini API key was rejected. Check GEMINI_API_KEY in .env."
                    ) from None
                elif code == 404:
                    if self.fallback_models:  # model renamed or retired: try the next one
                        self._switch_model("not found (404)")
                        retries += 1
                        continue
                    raise LLMError(
                        f"Gemini model {self.model!r} was not found (404) and no fallback model "
                        "is available. Set AUDITOR_MODEL to a current model id."
                    ) from None
                else:
                    raise LLMError(f"Gemini API rejected the request ({code}): {message}") from None
            except errors.ServerError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise LLMError(
                        f"Gemini API error {getattr(exc, 'code', '')} after retries."
                    ) from None
                overloaded += 1 if getattr(exc, "code", 0) == 503 else 0
                if overloaded >= SWITCH_AFTER_503 and self.fallback_models:
                    self._switch_model("overloaded (503)")
                    overloaded = 0
                    wait = 0.0
                else:
                    wait = min(MAX_BACKOFF, 2 * 2 ** (attempt - 1))
                    log.warning("Gemini server error %s; retrying in %.0fs", exc.code, wait)
            except errors.APIError as exc:
                raise LLMError(f"Gemini API error: {exc}") from None
            except (OSError, TimeoutError) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise LLMError(f"Could not reach the Gemini API: {exc}") from None
                wait = min(MAX_BACKOFF, 2 * 2 ** (attempt - 1))
                log.warning("Network error talking to Gemini (%s); retrying", type(exc).__name__)
            except Exception as exc:  # httpx/httpx2 transport errors etc.
                if is_transport_error(exc):
                    if attempt == MAX_ATTEMPTS:
                        raise LLMError(f"Could not reach the Gemini API: {exc}") from None
                    wait = min(MAX_BACKOFF, 2 * 2 ** (attempt - 1))
                    log.warning("Gemini transport error (%s); retrying", type(exc).__name__)
                else:
                    raise
            retries += 1
            self._sleep(wait + random.uniform(0, 1))

        meta = getattr(response, "usage_metadata", None)
        usage = Usage(
            calls=1,
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=(getattr(meta, "candidates_token_count", 0) or 0)
            + (getattr(meta, "thoughts_token_count", 0) or 0),
            cache_read_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
            seconds=time.monotonic() - started,
            retries=retries,
        )
        self.usage.add(usage)
        return parse_response(response, usage=usage, model=self.model)
