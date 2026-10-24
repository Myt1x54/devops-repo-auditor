"""Edge cases and failure paths found by the S12 coverage review."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from google.genai import errors, types

import agent
import tools
from auditor.errors import AuditError
from auditor.findings import CATEGORIES
from auditor.llm import STOP_OTHER, FakeLLM, LLMError, ToolSpec, call, fake_response
from auditor.llm_gemini import GeminiLLM
from auditor.trace import read_trace
from auditor.workflows import analyze_workflow
from auditor.workspace import RepoWorkspace
from tools import RepoTools

# -- workflow analysis -----------------------------------------------------------------------
RICH_WORKFLOW = """\
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
env:
  NODE_ENV: production
jobs:
  call:
    uses: org/shared/.github/workflows/deploy.yml@main
    secrets: inherit
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Publish
        run: npm publish
        continue-on-error: true
      - just-a-string
  broken: not-a-mapping
"""


def test_workflow_rich_features():
    out = analyze_workflow("w.yml", RICH_WORKFLOW)
    assert "workflow_run trigger (privileged follow-up workflow)" in out
    assert "env (workflow level): NODE_ENV" in out
    assert "calls reusable workflow org/shared/.github/workflows/deploy.yml@main" in out
    assert "secrets=inherit" in out
    assert "job 'release': write permissions (contents: write)" in out
    assert "step 1 'Publish': continue-on-error: true" in out
    assert "<deploy/publish>" in out
    assert "job 'broken': invalid definition" in out


def test_workflow_without_jobs_and_odd_permissions():
    out = analyze_workflow("w.yml", "on: push\npermissions: 42\n")
    assert "jobs: NONE" in out and "permissions (workflow level): 42" in out


# -- tools -----------------------------------------------------------------------------------
def test_shallow_history_notes(local_repo_url):
    with RepoWorkspace(local_repo_url, depth=1) as ws:
        t = RepoTools(ws.root, ws.run_git)
        assert "(shallow clone: older history is not available)" in t.git_log()
        assert "shallow clone" in t.git_file_history("*.txt")
        assert "shallow clone" in t.search_history("requests")


def test_search_time_budget(local_repo_url, monkeypatch):
    monkeypatch.setattr(tools, "SEARCH_TIME_BUDGET", -1.0)
    with RepoWorkspace(local_repo_url) as ws:
        out = RepoTools(ws.root, ws.run_git).search_code("x")
    assert "search stopped early (time budget)" in out


def test_misc_tool_input_errors(local_repo_url):
    with RepoWorkspace(local_repo_url) as ws:
        t = RepoTools(ws.root, ws.run_git)
        assert t.read_file("").startswith("ERROR: a file path is required")
        assert "end_line must be >= start_line" in t.read_file("README.md", 1, 0)
        assert t.git_file_history(" ").startswith("ERROR")
        assert t.search_history("x" * 201).startswith("ERROR")
        assert t.file_exists("README.md") == "README.md: EXISTS (file)"  # string, not list
        (ws.root / "untracked_dir").mkdir()
        assert "directory exists but is untracked" in t.list_files("untracked_dir")


def test_inspect_workflows_reports_other_ci_alongside(local_repo_url, monkeypatch):
    with RepoWorkspace(local_repo_url) as ws:
        t = RepoTools(ws.root, ws.run_git)
        wf = ws.root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\njobs: {}\n", encoding="utf-8")
        monkeypatch.setattr(
            t, "tracked_files", lambda: [".github/workflows/ci.yml", ".gitlab-ci.yml"]
        )
        out = t.inspect_workflows()
    assert "Other CI configuration also present: .gitlab-ci.yml" in out


def test_run_ruff_timeout(local_repo_url, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ruff", timeout=1)

    with RepoWorkspace(local_repo_url) as ws:
        t = RepoTools(ws.root, ws.run_git)
        t.tracked_files()  # cache git ls-files before subprocess is patched
        monkeypatch.setattr(tools.subprocess, "run", slow)
        assert "ruff timed out" in t.run_ruff()


# -- workspace -------------------------------------------------------------------------------
def test_git_missing(monkeypatch):
    monkeypatch.setattr("auditor.workspace.shutil.which", lambda _: None)
    with pytest.raises(AuditError, match="git is not installed"), RepoWorkspace("x"):
        pass


def test_clone_timeout(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("auditor.workspace.subprocess.run", slow)
    with pytest.raises(AuditError, match="timed out"), RepoWorkspace("x", timeout=1):
        pass


def test_run_git_timeout(local_repo_url, monkeypatch):
    with RepoWorkspace(local_repo_url) as ws:

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr("auditor.workspace.subprocess.run", slow)
        with pytest.raises(AuditError, match="timed out"):
            ws.run_git("log")


# -- Gemini client ---------------------------------------------------------------------------
TOOLS = [ToolSpec("t", "d", {"type": "object", "properties": {}})]


def _gemini(results):
    calls = list(results)

    def generate_content(**_):
        r = calls.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps = []
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return GeminiLLM(client=client, sleep=sleeps.append, fallback_models=()), sleeps


def _ok(finish="STOP"):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text="x")]),
                finish_reason=finish,
            )
        ]
    )


def test_gemini_persistent_server_error():
    llm, sleeps = _gemini([errors.ServerError(500, {"error": {"code": 500}})] * 8)
    with pytest.raises(LLMError, match="after retries"):
        llm.complete("s", [], TOOLS)
    assert len(sleeps) == 7


def test_gemini_unexpected_api_error_is_fatal():
    llm, _ = _gemini([errors.APIError(302, {"error": {"code": 302}})])
    with pytest.raises(LLMError, match="Gemini API error"):
        llm.complete("s", [], TOOLS)


def test_gemini_transport_error_retried():
    class ConnectTimeout(Exception):  # stands in for httpx transport errors
        pass

    llm, sleeps = _gemini([ConnectTimeout("slow"), _ok()])
    assert llm.complete("s", [], TOOLS).text == "x" and len(sleeps) == 1


def test_gemini_programming_errors_are_not_swallowed():
    llm, _ = _gemini([ValueError("bug")])
    with pytest.raises(ValueError):
        llm.complete("s", [], TOOLS)


def test_gemini_unknown_finish_reason():
    llm, _ = _gemini([_ok(finish="LANGUAGE")])
    assert llm.complete("s", [], TOOLS).stop_reason == STOP_OTHER


# -- CLI -------------------------------------------------------------------------------------
FINISH = fake_response(
    call("finish_audit", {"summary": "s", "categories_examined": list(CATEGORIES)})
)


def _cli(monkeypatch, local_repo_url, llm):
    monkeypatch.setattr(agent, "RepoWorkspace", lambda _url: RepoWorkspace(local_repo_url))
    monkeypatch.setattr(agent, "create_llm", lambda **_: llm)


def test_cli_max_steps_zero(tmp_path, capsys):
    args = ["--repo", "https://github.com/a/b", "--output", str(tmp_path / "r.json")]
    assert agent.main([*args, "--max-steps", "0"]) == 1
    assert "at least 1" in capsys.readouterr().err


def test_cli_traces_model_switches(tmp_path, local_repo_url, monkeypatch):
    llm = FakeLLM([FINISH])
    llm.model_switches = ["a -> b (daily quota used up)"]
    _cli(monkeypatch, local_repo_url, llm)
    out = tmp_path / "r.json"
    assert agent.main(["--repo", "https://github.com/a/b", "--output", str(out)]) == 0
    events = read_trace(tmp_path / "r.trace.jsonl")
    assert any(e["event"] == "model_switch" and "daily quota" in e["change"] for e in events)


def test_cli_warns_if_clone_modified(tmp_path, local_repo_url, monkeypatch, caplog):
    def modify(_history):
        ws_root = agent_state["root"]
        (ws_root / "new.txt").write_text("x", encoding="utf-8")
        return FINISH

    agent_state = {}
    real = RepoWorkspace

    def recording(_url):
        ws = real(local_repo_url)
        original_open = ws.open

        def open_and_record():
            original_open()
            agent_state["root"] = ws.root

        ws.open = open_and_record
        return ws

    monkeypatch.setattr(agent, "RepoWorkspace", recording)
    monkeypatch.setattr(agent, "create_llm", lambda **_: FakeLLM([modify]))
    out = tmp_path / "r.json"
    assert agent.main(["--repo", "https://github.com/a/b", "--output", str(out)]) == 0
    assert "clone was modified" in caplog.text
    end = read_trace(tmp_path / "r.trace.jsonl")[-1]
    assert end["repository_unchanged"] is False


def test_cli_report_write_failure(tmp_path, local_repo_url, monkeypatch, capsys):
    _cli(monkeypatch, local_repo_url, FakeLLM([FINISH]))

    def fail(*_a, **_k):
        raise PermissionError("read-only disk")

    monkeypatch.setattr(agent, "write_report", fail)
    assert (
        agent.main(["--repo", "https://github.com/a/b", "--output", str(tmp_path / "r.json")]) == 1
    )
    assert "Could not write the report" in capsys.readouterr().err


def test_cli_unexpected_exception_is_reported_not_raised(
    tmp_path, local_repo_url, monkeypatch, capsys
):
    def bug(_history):
        raise ZeroDivisionError("bug in harness")

    _cli(monkeypatch, local_repo_url, FakeLLM([bug]))
    out = tmp_path / "r.json"
    assert agent.main(["--repo", "https://github.com/a/b", "--output", str(out)]) == 1
    assert "unexpected failure" in capsys.readouterr().err
    assert read_trace(tmp_path / "r.trace.jsonl")[-1]["event"] == "error"
    assert not out.exists()


def test_report_json_is_utf8_and_pretty(tmp_path, local_repo_url, monkeypatch):
    _cli(monkeypatch, local_repo_url, FakeLLM([FINISH]))
    out = tmp_path / "r.json"
    agent.main(["--repo", "https://github.com/a/b", "--output", str(out)])
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n") and json.loads(text) == {
        "repository": "https://github.com/a/b",
        "violations": [],
    }


@pytest.mark.parametrize("module_name", ["httpx", "httpx2"])
def test_gemini_retries_connection_reset_read_errors(module_name):
    httpx_like = pytest.importorskip(module_name)
    # e.g. "[WinError 10054] An existing connection was forcibly closed by the remote host"
    reset = httpx_like.ReadError("connection reset by peer")
    llm, sleeps = _gemini([reset, _ok()])
    assert llm.complete("s", [], TOOLS).text == "x" and len(sleeps) == 1
