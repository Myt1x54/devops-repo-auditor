from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from auditor.errors import AuditError
from auditor.llm import (
    STOP_END,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    AssistantTurn,
    LLMError,
    ToolResult,
    ToolResultsTurn,
    ToolSpec,
    UserTurn,
)
from auditor.llm_anthropic import (
    DEFAULT_MODEL,
    FALLBACK_BETA,
    AnthropicLLM,
    parse_content,
    to_messages,
)

TOOLS = [ToolSpec("list_files", "x", {"type": "object", "properties": {}})]


class StubMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _client(result):
    messages = StubMessages(result)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _api_response(content, stop_reason="tool_use"):
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=80,
        cache_creation_input_tokens=5,
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, model="m")


def _status_error(cls, code):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=request), body=None)


def test_request_parameters(monkeypatch):
    monkeypatch.delenv("AUDITOR_MODEL", raising=False)
    monkeypatch.delenv("AUDITOR_EFFORT", raising=False)
    client, messages = _client(_api_response([{"type": "text", "text": "hi"}], "end_turn"))
    AnthropicLLM(client=client).complete("SYS", [UserTurn("go")], TOOLS)
    kw = messages.kwargs
    assert kw["model"] == DEFAULT_MODEL == "claude-opus-5-5"
    assert kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "go"}]
    assert kw["tools"] == [
        {"name": "list_files", "description": "x", "input_schema": TOOLS[0].parameters}
    ]
    assert kw["output_config"] == {"effort": "high"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in kw and "tool_choice" not in kw


def test_history_conversion_is_append_only():
    raw = [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "tool_use", "id": "t1"}]
    prior = parse_content(raw, stop_reason="tool_use")
    history = [
        UserTurn("go"),
        AssistantTurn(prior),
        ToolResultsTurn([ToolResult("t1", "list_files", "README.md", False)]),
    ]
    messages = to_messages(history)
    assert messages[1] == {"role": "assistant", "content": raw}  # resent unchanged
    assert messages[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "README.md",
        "is_error": False,
    }


def test_model_and_effort_from_env(monkeypatch):
    monkeypatch.setenv("AUDITOR_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("AUDITOR_EFFORT", "medium")
    client, messages = _client(_api_response([], "end_turn"))
    AnthropicLLM(client=client).complete("s", [], TOOLS)
    assert messages.kwargs["model"] == "claude-haiku-5-5"
    assert messages.kwargs["output_config"] == {"effort": "medium"}
    assert "fallbacks" not in messages.kwargs  # no server-side fallback for Haiku


def test_invalid_effort_rejected():
    with pytest.raises(AuditError, match="Invalid effort"):
        AnthropicLLM(client=object(), effort="extreme")


def test_parses_text_tool_calls_and_usage():
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Let me look at the files."},
        {"type": "tool_use", "id": "t1", "name": "list_files", "input": {"path": "."}},
        {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "README.md"}},
    ]
    client, _ = _client(_api_response(content))
    llm = AnthropicLLM(client=client)
    r = llm.complete("s", [], TOOLS)
    assert r.text == "Let me look at the files."
    assert [(c.id, c.name, c.input) for c in r.tool_calls] == [
        ("t1", "list_files", {"path": "."}),
        ("t2", "read_file", {"path": "README.md"}),
    ]
    assert r.stop_reason == STOP_TOOL_USE
    assert r.content == content
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_tokens) == (100, 20, 80)
    llm.complete("s", [], TOOLS)
    assert llm.usage.calls == 2 and llm.usage.input_tokens == 200


@pytest.mark.parametrize(
    ("raw", "expected"), [("end_turn", STOP_END), ("refusal", STOP_REFUSAL), ("weird", "other")]
)
def test_stop_reason_normalised(raw, expected):
    assert parse_content([], stop_reason=raw).stop_reason == expected


def test_parse_sdk_objects_and_bad_input():
    block = SimpleNamespace(type="tool_use", id="x", name="read_file", input="not-a-dict")
    r = parse_content([SimpleNamespace(type="text", text="hi"), block], stop_reason="tool_use")
    assert r.text == "hi" and r.tool_calls[0].input == {}


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (lambda: _status_error(anthropic.AuthenticationError, 401), "key was rejected"),
        (lambda: _status_error(anthropic.PermissionDeniedError, 403), "permission denied"),
        (lambda: _status_error(anthropic.NotFoundError, 404), "was not found"),
        (lambda: _status_error(anthropic.BadRequestError, 400), "rejected the request"),
        (lambda: _status_error(anthropic.RateLimitError, 429), "rate limit"),
        (lambda: _status_error(anthropic.InternalServerError, 500), "error 500"),
        (
            lambda: anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com")
            ),
            "Could not connect",
        ),
        (
            lambda: anthropic.APITimeoutError(
                request=httpx2.Request("POST", "https://api.anthropic.com")
            ),
            "timed out",
        ),
    ],
)
def test_api_errors_become_llm_errors(error, message):
    client, _ = _client(error())
    with pytest.raises(LLMError, match=message) as exc:
        AnthropicLLM(client=client).complete("s", [], TOOLS)
    assert exc.value.exit_code == 1


def test_thinking_summary_returned_as_reasoning():
    r = parse_content(
        [{"type": "thinking", "thinking": "Check CI first.", "signature": "s"}],
        stop_reason="end_turn",
    )
    assert r.reasoning == "Check CI first." and r.text == ""
