"""End-to-end: CLI -> clone -> agent loop -> tools -> verification -> report + trace.

A fixture repository with injected DevOps defects is audited by a scripted agent that, like a
real model, reacts to tool output. The script also records a hallucinated finding (file never
inspected) and a duplicate, which the harness must remove.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import agent
from auditor.findings import CATEGORIES
from auditor.llm import FakeLLM, ToolResultsTurn, call, fake_response
from auditor.report import FIELDS
from auditor.trace import read_trace
from auditor.workspace import RepoWorkspace

FAKE_KEY = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"  # assembled: never a literal secret

WORKFLOW = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: someone/setup-thing@main
      - run: pip install -r requirements.txt
      - run: pytest || true
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def defect_repo(tmp_path) -> str:
    repo = tmp_path / "defects"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    files = {
        "README.md": "# Payments service\n",
        "requirements.txt": "flask\nrequests==2.31.0\n",
        "app/config.py": f"DEBUG = True\nSTRIPE_KEY = '{FAKE_KEY}'\n",
        "app/main.py": "from app.config import STRIPE_KEY\n",
        ".github/workflows/ci.yml": WORKFLOW,
        "tests/test_main.py": "def test_ok():\n    assert True\n",
    }
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    (repo / ".env").write_text("DB_PASSWORD=hunter2hunter2\n", encoding="utf-8")
    _git(repo, "add", ".env")
    _git(repo, "commit", "-q", "-m", "add env")
    _git(repo, "rm", "-q", ".env")
    _git(repo, "commit", "-q", "-m", "remove env")
    return repo.as_uri()


def finding(rule, category, severity, desc, evidence, rec, file=None, line=None):
    args = {
        "rule_id": rule,
        "category": category,
        "severity": severity,
        "description": desc,
        "evidence": evidence,
        "recommendation": rec,
    }
    if file:
        args["file"] = file
    if line:
        args["line"] = line
    return call("record_finding", args)


def last_output(history, name):
    turn = next(t for t in reversed(history) if isinstance(t, ToolResultsTurn))
    return next(r.content for r in turn.results if r.name == name)


def scripted_agent() -> FakeLLM:
    def after_reads(history):
        config = last_output(history, "read_file")
        # The agent sees the key only as a redacted marker, never the value.
        assert FAKE_KEY not in config and "[REDACTED:" in config
        return fake_response(
            "Config holds a literal key and debug flag; checking CI and history next.",
            call("inspect_workflows"),
            call("git_file_history", {"pattern": "*.env*"}),
            call("read_file", {"path": "requirements.txt"}),
        )

    def record_all(history):
        assert ".env [NOT tracked now (deleted)" in last_output(history, "git_file_history")
        return fake_response(
            "Recording the evidenced problems.",
            finding(
                "SEC-001",
                "security",
                "critical",
                "Hardcoded Stripe API key in source",
                "app/config.py line 2 assigns `STRIPE_KEY = '` a literal live key",
                "Load the key from an environment variable or a secrets manager",
                "app/config.py",
                2,
            ),
            finding(
                "TST-003",
                "testing",
                "high",
                "Test failures are ignored in CI",
                "`run: pytest || true` in the CI workflow can never fail",
                "Remove '|| true' so failing tests fail the job",
                ".github/workflows/ci.yml",
                3,  # wrong line: verification moves it to 10
            ),
            finding(
                "CI-004",
                "ci_cd",
                "medium",
                "Third-party action pinned to a mutable branch",
                "`uses: someone/setup-thing@main` follows a moving branch",
                "Pin the action to a release tag or full commit SHA",
                ".github/workflows/ci.yml",
                8,
            ),
            finding(
                "SEC-004",
                "security",
                "high",
                "A .env file is still in git history",
                "git_file_history shows .env added then deleted; values remain retrievable",
                "Rotate the credentials and purge the file from history",
                ".env",
            ),
            finding(
                "DEP-001",
                "dependency_management",
                "medium",
                "Unpinned runtime dependency",
                "requirements.txt line 1 lists `flask` without any version",
                "Pin or constrain flask and commit a lockfile",
                "requirements.txt",
                1,
            ),
            finding(  # duplicate of the TST-003 above on another line -> merged
                "TST-003",
                "testing",
                "high",
                "Test failures are ignored in CI",
                "Same workflow suppresses errors with `|| true`",
                "Remove '|| true'",
                ".github/workflows/ci.yml",
                9,
            ),
            finding(  # hallucination: tests/test_main.py was never shown to the agent
                "TST-004",
                "testing",
                "medium",
                "Tests are skipped wholesale",
                "All tests in tests/test_main.py are marked skip",
                "Remove the skip markers",
                "tests/test_main.py",
                1,
            ),
        )

    return FakeLLM(
        [
            fake_response(
                "Python service with CI; start with config and the workflow.",
                call("read_file", {"path": "app/config.py"}),
                call("read_file", {"path": ".github/workflows/ci.yml"}),
            ),
            after_reads,
            record_all,
            fake_response(
                "All categories examined.",
                call("finish_audit", {"summary": "done", "categories_examined": list(CATEGORIES)}),
            ),
        ]
    )


def test_full_audit_of_defect_repo(tmp_path, defect_repo, monkeypatch):
    monkeypatch.setattr(agent, "RepoWorkspace", lambda _url: RepoWorkspace(defect_repo))
    monkeypatch.setattr(agent, "create_llm", lambda **_: scripted_agent())
    out = tmp_path / "report.json"

    code = agent.main(["--repo", "https://github.com/example/payments", "--output", str(out)])
    assert code == 0

    raw = out.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["repository"] == "https://github.com/example/payments"
    got = {(v["rule_id"], v["file"], v["line"]) for v in report["violations"]}
    assert got == {
        ("SEC-001", "app/config.py", 2),
        ("TST-003", ".github/workflows/ci.yml", 10),  # line corrected from 3 by verification
        ("SEC-004", ".env", None),
        ("CI-004", ".github/workflows/ci.yml", 8),  # correct line kept as cited
        ("DEP-001", "requirements.txt", 1),
    }
    # severity ordering and exact contract
    assert report["violations"][0]["severity"] == "critical"
    assert all(tuple(v) == FIELDS for v in report["violations"])
    # no secret value anywhere in the report or the trace
    trace_raw = (tmp_path / "report.trace.jsonl").read_text(encoding="utf-8")
    for text in (raw, trace_raw):
        assert FAKE_KEY not in text and "hunter2hunter2" not in text

    events = read_trace(tmp_path / "report.trace.jsonl")
    verify = {(e["rule_id"], e["action"]) for e in events if e["event"] == "verify"}
    assert ("TST-004", "dropped") in verify  # hallucinated finding removed
    assert ("TST-003", "merged") in verify  # duplicate merged
    assert ("TST-003", "line_corrected") in verify
    end = events[-1]
    assert end["findings_recorded"] == 7 and end["violations_reported"] == 5
    assert end["repository_unchanged"] is True
