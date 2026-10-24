from __future__ import annotations

import re

from auditor.findings import CATEGORIES, AuditState
from auditor.prompts import SYSTEM_PROMPT, initial_message
from auditor.toolset import build_registry

TOOL_NAME_RE = re.compile(
    r"\b(list_files|read_file|search_code|git_log|inspect_workflows|file_exists|"
    r"git_file_history|search_history|run_ruff|git_status|record_finding|retract_finding|"
    r"finish_audit|[a-z]+_[a-z_]+\()"
)


class _StubTools:
    """Minimal RepoTools stand-in: build_registry only needs the bound methods."""

    def tracked_files(self):
        return []

    def __getattr__(self, name):
        return lambda **kw: ""


def test_every_tool_named_in_prompt_exists():
    registry = build_registry(_StubTools(), AuditState())
    named = {m.rstrip("(") for m in TOOL_NAME_RE.findall(SYSTEM_PROMPT)}
    tool_like = {n for n in named if n in registry.names() or n.endswith(("_files", "_file"))}
    assert tool_like <= set(registry.names()), tool_like - set(registry.names())
    registry.close()


def test_prompt_contains_key_guidance():
    for phrase in (
        "precision",
        "write one short sentence",
        "same turn",
        "acceptable equivalents",
        "@v4",
        "Do not infer remote settings",
        "retract",
        "git_file_history",
        "inspect_workflows",
        "Never write secret values",
    ):
        assert phrase.lower() in SYSTEM_PROMPT.lower(), phrase


def test_prompt_search_patterns_are_valid_regexes():
    for pattern in (
        r"(?i)(api[_-]?key|secret|token|passw(or)?d)\s*[:=]",
        r"[A-Za-z]:\\Users|/home/\w+|/Users/\w+",
    ):
        assert pattern in SYSTEM_PROMPT
        re.compile(pattern)
    assert re.search(r"[A-Za-z]:\\Users|/home/\w+", r'path = "C:\Users\dev\data"')


def test_prompt_is_static():
    assert "{" not in SYSTEM_PROMPT.split("## Rule catalogue")[0].replace("${{", "")


def test_initial_message():
    msg = initial_message("https://github.com/a/b", "README.md  (10 B)")
    assert "https://github.com/a/b" in msg and "README.md  (10 B)" in msg
    assert all(c in msg for c in CATEGORIES)
    assert "list_files(path=...)" in msg
