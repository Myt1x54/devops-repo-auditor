from __future__ import annotations

import pytest

from auditor.errors import AuditError
from auditor.llm import (
    STOP_END,
    STOP_TOOL_USE,
    FakeLLM,
    LLMError,
    ToolSpec,
    UserTurn,
    call,
    create_llm,
    fake_response,
)
from auditor.llm_anthropic import AnthropicLLM
from auditor.llm_gemini import GeminiLLM

TOOLS = [ToolSpec("list_files", "List files", {"type": "object", "properties": {}})]


def test_create_llm_defaults_to_gemini(monkeypatch):
    monkeypatch.delenv("AUDITOR_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    llm = create_llm()
    assert isinstance(llm, GeminiLLM) and llm.provider == "gemini"


def test_create_llm_anthropic(monkeypatch):
    monkeypatch.setenv("AUDITOR_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert isinstance(create_llm(), AnthropicLLM)


def test_create_llm_unknown_provider():
    with pytest.raises(AuditError, match="Unknown AUDITOR_PROVIDER"):
        create_llm("openai")


@pytest.mark.parametrize(
    ("provider", "env_var"), [("gemini", "GEMINI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")]
)
def test_missing_key_is_clear_error(provider, env_var, monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(LLMError, match=env_var) as exc:
        create_llm(provider)
    assert exc.value.exit_code == 1


def test_fake_llm_scripts_and_records():
    fake = FakeLLM(
        [
            fake_response("Looking", call("list_files")),
            lambda history: fake_response(f"saw {len(history)} turns"),
        ]
    )
    r1 = fake.complete("s", [UserTurn("go")], TOOLS)
    assert r1.stop_reason == STOP_TOOL_USE and r1.tool_calls[0].name == "list_files"
    r2 = fake.complete("s", [UserTurn("a"), UserTurn("b"), UserTurn("c")], TOOLS)
    assert r2.text == "saw 3 turns" and r2.stop_reason == STOP_END
    assert len(fake.calls) == 2 and fake.usage.calls == 2
    with pytest.raises(LLMError, match="exhausted"):
        fake.complete("s", [], TOOLS)
