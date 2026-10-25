from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors, types

from auditor.errors import AuditError
from auditor.llm import (
    STOP_END,
    STOP_MALFORMED,
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    AssistantTurn,
    LLMError,
    ToolResult,
    ToolResultsTurn,
    ToolSpec,
    UserTurn,
)
from auditor.llm_gemini import (
    DEFAULT_MODEL,
    GeminiLLM,
    is_daily_quota,
    retry_delay,
    to_contents,
)

TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object", "properties": {}})]


def _response(parts, finish="STOP", prompt=120, out=30, thoughts=10, cached=50):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=out,
            thoughts_token_count=thoughts,
            cached_content_token_count=cached,
        ),
    )


class StubModels:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _llm(results, **kwargs):
    models = StubModels(results)
    sleeps = []
    llm = GeminiLLM(client=SimpleNamespace(models=models), sleep=sleeps.append, **kwargs)
    return llm, models, sleeps


def _client_error(code, message="err", details=None, status="X"):
    return errors.ClientError(
        code,
        {"error": {"code": code, "message": message, "status": status, "details": details or []}},
    )


RETRY_INFO = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]
DAILY = [
    {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
    }
]


# -- request shape -------------------------------------------------------------------------------
def test_request_shape(monkeypatch):
    monkeypatch.delenv("AUDITOR_MODEL", raising=False)
    monkeypatch.delenv("AUDITOR_EFFORT", raising=False)
    llm, models, _ = _llm([_response([types.Part(text="done")])])
    llm.complete("SYSTEM", [UserTurn("audit this")], TOOLS)
    kw = models.calls[0]
    assert kw["model"] == DEFAULT_MODEL == "gemini-3.5-flash-lite"
    cfg = kw["config"]
    assert cfg.system_instruction == "SYSTEM"
    assert cfg.automatic_function_calling.disable is True  # our own loop runs the tools
    assert str(cfg.thinking_config.thinking_level.value).lower() == "medium"
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "read_file" and decl.parameters_json_schema == TOOLS[0].parameters
    assert kw["contents"][0].role == "user" and kw["contents"][0].parts[0].text == "audit this"


def test_effort_maps_to_thinking_level():
    llm, _, _ = _llm([], effort="max")
    assert llm.thinking_level == "high"
    with pytest.raises(AuditError, match="Invalid effort"):
        _llm([], effort="extreme")


# -- parsing -------------------------------------------------------------------------------------
def test_parses_text_tool_calls_usage_and_keeps_raw_content():
    parts = [
        types.Part(text="hidden reasoning", thought=True),
        types.Part(text="I will read the README."),
        types.Part(
            function_call=types.FunctionCall(
                id="fc1", name="read_file", args={"path": "README.md"}
            ),
            thought_signature=b"sig-bytes",
        ),
        types.Part(function_call=types.FunctionCall(name="list_files", args={})),
    ]
    llm, _, _ = _llm([_response(parts)])
    r = llm.complete("s", [UserTurn("go")], TOOLS)
    assert r.text == "I will read the README."  # thought text excluded
    assert r.stop_reason == STOP_TOOL_USE
    assert [(c.id, c.name, c.input) for c in r.tool_calls] == [
        ("fc1", "read_file", {"path": "README.md"}),
        ("gen_3_list_files", "list_files", {}),
    ]
    assert r.content.parts[2].thought_signature == b"sig-bytes"
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_tokens) == (120, 40, 50)


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("STOP", STOP_END),
        ("MAX_TOKENS", STOP_MAX_TOKENS),
        ("SAFETY", STOP_REFUSAL),
        ("MALFORMED_FUNCTION_CALL", STOP_MALFORMED),
    ],
)
def test_finish_reasons(finish, expected):
    llm, _, _ = _llm([_response([types.Part(text="x")], finish=finish)])
    assert llm.complete("s", [], TOOLS).stop_reason == expected


def test_blocked_prompt_is_refusal():
    blocked = types.GenerateContentResponse(
        candidates=[],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"),
    )
    llm, _, _ = _llm([blocked])
    r = llm.complete("s", [], TOOLS)
    assert r.stop_reason == STOP_REFUSAL and "SAFETY" in r.detail


def test_history_conversion_preserves_model_turns():
    prior_content = types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(id="fc1", name="read_file", args={}),
                thought_signature=b"sig",
            )
        ],
    )
    llm, _, _ = _llm([_response([types.Part(text="ok")])])
    from auditor.llm import LLMResponse

    prior = LLMResponse(text="", tool_calls=[], stop_reason=STOP_TOOL_USE, content=prior_content)
    contents = to_contents(
        [
            UserTurn("go"),
            AssistantTurn(prior),
            ToolResultsTurn(
                [
                    ToolResult("fc1", "read_file", "file text", False),
                    ToolResult("gen_1_list_files", "list_files", "ERROR: bad path", True),
                ]
            ),
        ]
    )
    assert contents[1] is prior_content  # resent unchanged (thought signature kept)
    fr1, fr2 = (p.function_response for p in contents[2].parts)
    assert (fr1.id, fr1.name, fr1.response) == ("fc1", "read_file", {"output": "file text"})
    assert (fr2.id, fr2.response) == (None, {"error": "ERROR: bad path"})  # generated id dropped


# -- retries and errors --------------------------------------------------------------------------
def test_rate_limit_waits_server_delay_then_succeeds():
    llm, models, sleeps = _llm(
        [_client_error(429, details=RETRY_INFO), _response([types.Part(text="ok")])]
    )
    r = llm.complete("s", [], TOOLS)
    assert r.text == "ok" and len(models.calls) == 2
    assert 7 <= sleeps[0] <= 8 and r.usage.retries == 1


def test_rate_limit_backoff_without_hint_and_gives_up():
    llm, _, sleeps = _llm([_client_error(429)] * 8)
    with pytest.raises(LLMError, match="rate limit persisted"):
        llm.complete("s", [], TOOLS)
    assert len(sleeps) == 7 and sleeps[0] < sleeps[1] < sleeps[2]


def test_daily_quota_switches_model_without_waiting():
    llm, models, sleeps = _llm(
        [_client_error(429, details=DAILY), _response([types.Part(text="ok")])]
    )
    r = llm.complete("s", [], TOOLS)
    assert r.text == "ok" and sleeps == []
    assert [c["model"] for c in models.calls] == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert llm.model_switches == [
        "gemini-3.5-flash-lite -> gemini-3.1-flash-lite (daily quota used up)"
    ]


def test_daily_quota_on_last_model_fails_fast():
    llm, models, sleeps = _llm([_client_error(429, details=DAILY)], fallback_models=())
    with pytest.raises(LLMError, match="daily quota"):
        llm.complete("s", [], TOOLS)
    assert len(models.calls) == 1 and sleeps == []


def test_server_error_retried():
    llm, _, sleeps = _llm(
        [errors.ServerError(503, {"error": {"code": 503}}), _response([types.Part(text="ok")])]
    )
    assert llm.complete("s", [], TOOLS).text == "ok" and len(sleeps) == 1


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (_client_error(400, "API key not valid. Please pass a valid API key."), "key was rejected"),
        (_client_error(403, "permission"), "key was rejected"),
        (_client_error(404, "model not found"), "was not found"),
        (_client_error(400, "Invalid JSON payload"), r"rejected the request \(400\)"),
    ],
)
def test_client_errors_are_fatal(error, message):
    llm, models, sleeps = _llm([error], fallback_models=())  # no model left to try
    with pytest.raises(LLMError, match=message):
        llm.complete("s", [], TOOLS)
    assert len(models.calls) == 1 and sleeps == []


def test_network_error_retried_then_fatal():
    llm, _, sleeps = _llm([ConnectionError("down")] * 8)
    with pytest.raises(LLMError, match="Could not reach"):
        llm.complete("s", [], TOOLS)
    assert len(sleeps) == 7


def test_error_detail_helpers():
    assert retry_delay(_client_error(429, details=RETRY_INFO)) == 7.0
    assert retry_delay(_client_error(429)) is None
    assert is_daily_quota(_client_error(429, details=DAILY))
    assert not is_daily_quota(_client_error(429, details=RETRY_INFO))


def test_switches_to_fallback_model_when_overloaded():
    overloaded = errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    llm, models, _ = _llm([overloaded] * 3 + [_response([types.Part(text="ok")])])
    r = llm.complete("s", [], TOOLS)
    assert r.text == "ok"
    assert [c["model"] for c in models.calls] == ["gemini-3.5-flash-lite"] * 3 + [
        "gemini-3.1-flash-lite"
    ]
    assert llm.model == "gemini-3.1-flash-lite" and r.model == "gemini-3.1-flash-lite"
    assert llm.model_switches == [
        "gemini-3.5-flash-lite -> gemini-3.1-flash-lite (overloaded (503))"
    ]


def test_no_fallback_left_keeps_retrying_same_model():
    overloaded = errors.ServerError(503, {"error": {"code": 503}})
    llm, models, _ = _llm(
        [overloaded] * 4 + [_response([types.Part(text="ok")])], fallback_models=()
    )
    assert llm.complete("s", [], TOOLS).text == "ok"
    assert {c["model"] for c in models.calls} == {"gemini-3.5-flash-lite"}


def test_thought_summaries_returned_as_reasoning():
    parts = [
        types.Part(text="Workflows usually hold CI risks; inspect them first.", thought=True),
        types.Part(function_call=types.FunctionCall(name="inspect_workflows", args={})),
    ]
    llm, models, _ = _llm([_response(parts)])
    r = llm.complete("s", [], TOOLS)
    assert r.reasoning == "Workflows usually hold CI risks; inspect them first." and r.text == ""
    assert models.calls[0]["config"].thinking_config.include_thoughts is True


def test_model_not_found_falls_back_to_next_model():
    llm, models, sleeps = _llm(
        [_client_error(404, "models/gemini-x is not found"), _response([types.Part(text="ok")])]
    )
    r = llm.complete("s", [], TOOLS)
    assert r.text == "ok" and sleeps == []
    assert [c["model"] for c in models.calls] == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert llm.model_switches == [
        "gemini-3.5-flash-lite -> gemini-3.1-flash-lite (not found (404))"
    ]


def test_fallback_chain_order():
    llm, _, _ = _llm([])
    assert llm.fallback_models == ["gemini-3.1-flash-lite", "gemini-3.6-flash"]
