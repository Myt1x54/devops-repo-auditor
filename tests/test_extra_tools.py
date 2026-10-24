from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auditor.workflows import analyze_workflow, pin_status
from auditor.workspace import RepoWorkspace
from tools import RepoTools

SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"

VULNERABLE_WORKFLOW = """\
name: PR checks
on:
  pull_request_target:
    branches: [main]
permissions: write-all
jobs:
  test:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@main
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - name: Greet
        run: echo "Title ${{ github.event.pull_request.title }}"
      - name: Run tests
        run: pytest -q || true
      - name: Lint
        run: ruff check . --exit-zero
      - name: Deploy
        run: ./deploy.sh
        env:
          TOKEN: ${{ secrets.DEPLOY_TOKEN }}
"""

GOOD_WORKFLOW = f"""\
name: CI
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{SHA} # v4.2.2
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: pytest
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, files: dict[str, str], message: str, remove: list[str] = ()) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel in remove:
        _git(repo, "rm", "-q", rel)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def ws_tools(tmp_path):
    src = tmp_path / "src-repo"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _commit(
        src,
        {
            "README.md": "# Demo\n",
            "app.py": "import os\nimport sys\n\n\ndef main():\n    return os.name\n",
            ".github/workflows/pr.yml": VULNERABLE_WORKFLOW,
            ".github/workflows/ci.yml": GOOD_WORKFLOW,
            ".github/workflows/broken.yaml": "on: [push]\njobs:\n  x: [unclosed\n",
            "config.py": "API_KEY = 'q8Zr2LmX9vB4nT7kP1wY6cD3'\n",
        },
        "initial",
    )
    _commit(src, {".env": "DB_PASSWORD=hunter2\n"}, "add env")
    _commit(src, {"config.py": "import os\nAPI_KEY = os.getenv('API_KEY')\n"}, "use env var")
    _commit(src, {}, "remove env", remove=[".env"])
    with RepoWorkspace(src.as_uri()) as ws:
        yield ws, RepoTools(ws.root, ws.run_git)


@pytest.fixture
def repo_tools(ws_tools):
    return ws_tools[1]


# -- pin_status / analyze_workflow -----------------------------------------------------------
@pytest.mark.parametrize(
    ("uses", "expected"),
    [
        (f"actions/checkout@{SHA}", "pinned to full commit SHA"),
        ("actions/checkout@v4", "version tag 'v4'"),
        ("actions/checkout@v4.2.2", "version tag"),
        ("actions/checkout@main", "branch/other ref 'main'"),
        ("some/action", "NO REF"),
        ("./.github/actions/local", "local action"),
        ("docker://alpine:3.19", "docker image (tag)"),
    ],
)
def test_pin_status(uses, expected):
    assert expected in pin_status(uses)


def test_analyze_vulnerable_workflow_signals():
    out = analyze_workflow(".github/workflows/pr.yml", VULNERABLE_WORKFLOW)
    assert "triggers: pull_request_target(branches=['main'])" in out
    assert "permissions (workflow level): write-all" in out
    assert "line 3: pull_request_target trigger" in out
    assert "checks out the PR head under pull_request_target" in out
    assert "untrusted event data" in out and "pull_request.title" in out
    assert "'|| true' style error suppression" in out
    assert "'--exit-zero'" in out
    assert "continue-on-error: true" in out
    assert "uses actions/checkout@main [branch/other ref 'main' (mutable)] (line 11)" in out
    assert "step 1 (line 11): action actions/checkout@main" in out
    assert "secrets referenced: DEPLOY_TOKEN" in out
    assert "<test>" in out and "<lint>" in out and "<deploy/publish>" in out


def test_analyze_good_workflow_has_no_signals():
    out = analyze_workflow(".github/workflows/ci.yml", GOOD_WORKFLOW)
    assert "triggers: push(branches=['main']), pull_request" in out
    assert "permissions (workflow level): contents: read" in out
    assert "pinned to full commit SHA" in out and "persist-credentials=false" in out
    assert "job runs: test" in out
    assert "signals to verify" not in out


def test_analyze_invalid_and_odd_workflows():
    assert "INVALID YAML at line" in analyze_workflow(
        "w.yml", "on: [push]\njobs:\n  x: [unclosed\n"
    )
    assert "Not a mapping" in analyze_workflow("w.yml", "- just\n- a list\n")
    out = analyze_workflow("w.yml", "name: x\njobs:\n  a:\n    runs-on: ubuntu-latest\n")
    assert "NONE (workflow never runs)" in out
    out = analyze_workflow("w.yml", "on: push\njobs:\n  a:\n    permissions: {}\n    steps: []\n")
    assert "permissions (workflow level): not set" in out


# -- inspect_workflows -----------------------------------------------------------------------
def test_inspect_workflows_all_and_single(repo_tools):
    out = repo_tools.inspect_workflows()
    assert out.startswith("3 workflow file(s).")
    assert "=== .github/workflows/pr.yml" in out and "=== .github/workflows/ci.yml" in out
    assert "INVALID YAML" in out
    single = repo_tools.inspect_workflows(".github/workflows/ci.yml")
    assert "ci.yml" in single and "pr.yml" not in single
    assert repo_tools.inspect_workflows("README.md").startswith("ERROR:")


def test_inspect_workflows_none(repo_tools, monkeypatch):
    monkeypatch.setattr(repo_tools, "tracked_files", lambda: ["README.md", ".gitlab-ci.yml"])
    out = repo_tools.inspect_workflows()
    assert "No GitHub Actions workflows" in out and ".gitlab-ci.yml" in out


# -- file_exists -----------------------------------------------------------------------------
def test_file_exists(repo_tools):
    out = repo_tools.file_exists(
        ["README.md", ".github", "poetry.lock", "*.py", "tests/test_*.py", "./config.py"]
    )
    assert "README.md: EXISTS (file)" in out
    assert ".github: EXISTS (directory, 3 tracked files)" in out
    assert "poetry.lock: missing" in out
    assert "*.py: 2 match(es)" in out
    assert "tests/test_*.py: no match" in out
    assert "./config.py: EXISTS (file)" in out
    assert repo_tools.file_exists([]).startswith("ERROR:")
    assert repo_tools.file_exists(["../x"]).startswith("ERROR:")


# -- history tools ---------------------------------------------------------------------------
def test_git_file_history_finds_deleted_env(repo_tools):
    out = repo_tools.git_file_history(".env")
    assert ".env [NOT tracked now (deleted), 2 change(s)]" in out
    assert out.count("A ") >= 1 and "D " in out
    assert "No file matching" in repo_tools.git_file_history("*.pem")
    tracked = repo_tools.git_file_history("config.py")
    assert "config.py [still tracked, 2 change(s)]" in tracked


def test_search_history_finds_removed_secret_without_content(repo_tools):
    out = repo_tools.search_history(r"API_KEY = '")
    assert "commit(s) added/removed lines" in out
    assert "use env var" in out and "config.py" in out
    assert "q8Zr2" not in out  # never returns the matched content
    assert "No commit" in repo_tools.search_history("NEVER_PRESENT_STRING")
    assert repo_tools.search_history("([bad").startswith("ERROR:")


# -- run_ruff / git_status -------------------------------------------------------------------
def test_run_ruff_reports_issues_and_leaves_repo_clean(ws_tools):
    ws, repo_tools = ws_tools
    out = repo_tools.run_ruff()
    assert "ruff check:" in out and "F401" in out  # unused import sys
    assert "app.py:2:" in out
    assert str(ws.root) not in out  # paths are repo-relative
    assert repo_tools.git_status().startswith("Working tree clean")
    assert not (ws.root / ".ruff_cache").exists()


def test_run_ruff_without_python_files(repo_tools, monkeypatch):
    monkeypatch.setattr(repo_tools, "tracked_files", lambda: ["README.md"])
    assert "not applicable" in repo_tools.run_ruff()


def test_git_status_detects_changes(ws_tools):
    ws, repo_tools = ws_tools
    (ws.root / "new.txt").write_text("x", encoding="utf-8")
    out = repo_tools.git_status()
    assert "has changes" in out and "new.txt" in out


def test_folded_run_block_gets_a_line_number():
    text = (
        "on: push\njobs:\n  rel:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: >-\n          gh release create --draft --repo ${{ github.repository }}\n"
        "          ${{ github.ref_name }} artifact/*\n"
    )
    out = analyze_workflow("w.yml", text)
    assert "run (line 7)" in out
