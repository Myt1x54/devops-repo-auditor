from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor.findings import CATEGORIES, AuditState
from auditor.llm import ToolCall
from auditor.toolset import build_registry
from auditor.workspace import RepoWorkspace
from tools import RepoTools

EXPECTED_TOOLS = [
    "list_files",
    "read_file",
    "search_code",
    "git_log",
    "inspect_workflows",
    "file_exists",
    "git_file_history",
    "search_history",
    "run_ruff",
    "git_status",
    "record_finding",
    "retract_finding",
    "finish_audit",
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "README.md").write_text("# Demo\n", encoding="utf-8")
    (src / "config.py").write_text("DEBUG = True\nAPI_KEY = 'q8Zr2LmX9vB4nT7kP1wY6cD3'\n", "utf-8")
    (src / ".github" / "workflows").mkdir(parents=True)
    (src / ".github" / "workflows" / "ci.yml").write_text(
        "on: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n",
        "utf-8",
    )
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "init")
    (src / ".env").write_text("X=1\n", "utf-8")
    _git(src, "add", ".env")
    _git(src, "commit", "-q", "-m", "env")
    _git(src, "rm", "-q", ".env")
    _git(src, "commit", "-q", "-m", "rm env")
    with RepoWorkspace(src.as_uri()) as ws:
        state = AuditState()
        registry = build_registry(RepoTools(ws.root, ws.run_git), state)
        yield registry, state
        registry.close()


def run(registry, name, **args):
    result, _ = registry.dispatch(ToolCall(f"id-{name}", name, args))
    return result


FINDING = {
    "rule_id": "SEC-001",
    "category": "security",
    "severity": "critical",
    "file": "config.py",
    "line": 2,
    "description": "Hardcoded API key in source code",
    "evidence": "API_KEY assigned a literal credential value at config.py line 2",
    "recommendation": "Load the key from an environment variable or secrets manager",
}


def test_all_tools_registered_with_schemas(setup):
    registry, _ = setup
    assert registry.names() == EXPECTED_TOOLS
    for spec in registry.specs():
        assert spec.description and spec.parameters["type"] == "object"
    record = registry.get("record_finding").spec.parameters
    assert record["properties"]["severity"]["enum"] == ["critical", "high", "medium", "low"]
    assert len(record["properties"]["category"]["enum"]) == 9


def test_inspection_tools_dispatch_and_track_observations(setup):
    registry, state = setup
    assert "README.md" in run(registry, "list_files").content
    assert {"README.md", "config.py"} <= state.seen_files
    assert state.read_files == set()

    out = run(registry, "read_file", path="config.py").content
    assert "[REDACTED:" in out and "q8Zr2" not in out  # redacted before the LLM sees it
    assert "config.py" in state.read_files

    run(registry, "search_code", pattern="pytest")
    run(registry, "inspect_workflows")
    assert ".github/workflows/ci.yml" in state.read_files

    run(registry, "git_file_history", pattern=".env")
    assert ".env" in state.history_files and ".env" in state.seen_files
    assert state.tool_calls == 5


def test_tool_error_does_not_track(setup):
    registry, state = setup
    result = run(registry, "read_file", path="../outside")
    assert result.is_error and state.read_files == set()


def test_record_finding_happy_path_and_duplicate(setup):
    registry, state = setup
    run(registry, "read_file", path="config.py")
    first = run(registry, "record_finding", **FINDING)
    assert not first.is_error and first.content.startswith("Recorded F1: SEC-001 [critical]")
    assert "Warning" not in first.content
    dup = run(registry, "record_finding", **FINDING)
    assert "duplicate of F1" in dup.content
    assert len(state.active_findings()) == 1
    f = state.findings[0]
    assert (f.file, f.line, f.category) == ("config.py", 2, "security")


def test_record_finding_repository_wide_and_history_file(setup):
    registry, state = setup
    missing = {**FINDING, "rule_id": "HYG-002", "category": "repository_hygiene", "severity": "low"}
    missing.pop("file")
    missing.pop("line")
    assert run(registry, "record_finding", **missing).content.startswith("Recorded F1")
    assert state.findings[0].file is None and state.findings[0].line is None

    run(registry, "git_file_history", pattern=".env")
    hist = {**FINDING, "rule_id": "SEC-004", "file": ".env", "line": None}
    assert run(registry, "record_finding", **hist).content.startswith("Recorded F2")


def test_record_finding_warns_when_file_not_inspected(setup):
    registry, _ = setup
    out = run(registry, "record_finding", **FINDING).content
    assert "Warning: you have not inspected this file yet" in out


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rule_id": "SECURITY-1"}, "rule_id must look like"),
        ({"rule_id": "CI-001"}, "prefix does not match"),
        ({"category": "ml_training"}, "must be one of"),
        ({"severity": "urgent"}, "must be one of"),
        ({"file": "nope.py"}, "not a file in the repository"),
        ({"file": "../etc/passwd"}, "may not contain"),
        ({"line": 99}, "outside 'config.py' (1-2)"),
        ({"file": None, "line": 3}, "line given without file"),
        ({"evidence": "short"}, "too short"),
    ],
)
def test_record_finding_validation(setup, changes, message):
    registry, state = setup
    result = run(registry, "record_finding", **{**FINDING, **changes})
    assert result.is_error and message in result.content
    assert state.findings == []


def test_retract_and_finish(setup):
    registry, state = setup
    run(registry, "record_finding", **FINDING)
    assert run(
        registry, "retract_finding", finding_id="f1", reason="alternative exists"
    ).content == ("Retracted F1. Active findings: 0.")
    assert run(registry, "retract_finding", finding_id="F1", reason="again").is_error
    assert state.findings[0].retracted and state.findings[0].retract_reason == "alternative exists"

    done = run(
        registry,
        "finish_audit",
        summary="Checked all nine categories.",
        categories_examined=list(CATEGORIES),
    )
    assert done.content == "Audit finished with 0 active finding(s)."
    assert state.finished and state.summary == "Checked all nine categories."


def test_finish_audit_nudges_once_for_missing_categories(setup):
    registry, state = setup
    first = run(registry, "finish_audit", summary="partial", categories_examined=["security"])
    assert first.content.startswith("Not finished yet") and "ci_cd" in first.content
    assert not state.finished and state.coverage_nudged
    second = run(
        registry, "finish_audit", summary="rest not applicable", categories_examined=["testing"]
    )
    assert second.content.startswith("Audit finished") and state.finished
    assert state.categories_declared == {"security", "testing"}


def test_record_finding_requires_catalogue_or_custom_id(setup):
    registry, state = setup
    bad = run(registry, "record_finding", **{**FINDING, "rule_id": "SEC-050"})
    assert bad.is_error and "not in the rule catalogue" in bad.content and "SEC-001" in bad.content
    custom = run(registry, "record_finding", **{**FINDING, "rule_id": "SEC-901"})
    assert custom.content.startswith("Recorded F1: SEC-901")
