from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tools
from auditor.workspace import RepoWorkspace
from tools import RepoTools, ToolError, normalize_repo_path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Dev One", "-c", "user.email=dev@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo_tools(tmp_path):
    src = tmp_path / "src-repo"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    files = {
        "README.md": "# Demo\n\nUsage text.\n",
        "src/app.py": "import os\n\n\ndef main():\n    return os.getenv('HOME')\n",
        "src/pkg/deep/mod.py": "VALUE = 1\n",
        "config/settings.py": "DEBUG = True\nAPI_KEY = 'abc123'\nDB = 'C:/Users/dev/db.sqlite'\n",
        ".github/workflows/ci.yml": "on: [push]\njobs: {}\n",
        "notes.txt": "\n".join(f"line {i}" for i in range(1, 1001)) + "\n",
        "empty.txt": "",
    }
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (src / "image.png").write_bytes(b"\x89PNG\x00\x00binary")
    (src / "big.txt").write_text("x" * (tools.MAX_READ_BYTES + 10), encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial commit")
    (src / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _git(src, "add", ".env")
    _git(src, "commit", "-q", "-m", "add env file")
    _git(src, "rm", "-q", ".env")
    _git(src, "commit", "-q", "-m", "remove env file")

    with RepoWorkspace(src.as_uri()) as ws:
        yield RepoTools(ws.root, ws.run_git)


# -- path guard ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "../secret",
        "src/../../x",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:\\x",
        "~/x",
        ".git/config",
    ],
)
def test_normalize_rejects_unsafe_paths(bad):
    with pytest.raises(ToolError):
        normalize_repo_path(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(".", ""), ("", ""), ("./src/app.py", "src/app.py"), ("src\\app.py", "src/app.py")],
)
def test_normalize_accepts_relative_paths(raw, expected):
    assert normalize_repo_path(raw) == expected


def test_tools_return_errors_instead_of_raising(repo_tools):
    assert repo_tools.read_file("../outside").startswith("ERROR:")
    assert repo_tools.list_files("/etc").startswith("ERROR:")
    assert repo_tools.git_log(path="../x").startswith("ERROR:")


# -- list_files ------------------------------------------------------------------------------
def test_list_files_root(repo_tools):
    out = repo_tools.list_files()
    assert "README.md" in out and "src/pkg/deep/mod.py" in out
    assert ".github/workflows/ci.yml" in out
    assert ".env" not in out.split()  # deleted file is not tracked any more
    assert out.splitlines()[0].startswith("9 tracked file(s)")


def test_list_files_subdir_and_depth(repo_tools):
    out = repo_tools.list_files("src", max_depth=1)
    assert "src/app.py" in out
    assert "src/pkg/  (1 files)" in out


def test_list_files_missing_path(repo_tools):
    assert repo_tools.list_files("nope").startswith("ERROR: path not found")


def test_list_files_collapses_large_trees(repo_tools, monkeypatch):
    monkeypatch.setattr(tools, "MAX_LIST_ENTRIES", 3)
    out = repo_tools.list_files()
    assert "Directories not expanded" in out
    assert "src/  (2 files)" in out


# -- read_file -------------------------------------------------------------------------------
def test_read_file_line_numbers(repo_tools):
    out = repo_tools.read_file("config/settings.py")
    assert out.splitlines()[0] == "config/settings.py (lines 1-3 of 3)"
    assert "2| API_KEY = 'abc123'" in out


def test_read_file_range_and_pagination(repo_tools):
    out = repo_tools.read_file("notes.txt", start_line=10, end_line=12)
    assert "notes.txt (lines 10-12 of 1000)" in out
    assert "10| line 10" in out and "12| line 12" in out and "line 13" not in out
    long = repo_tools.read_file("notes.txt")
    assert f"lines 1-{tools.MAX_READ_LINES} of 1000" in long
    assert f"start_line={tools.MAX_READ_LINES + 1}" in long


def test_read_file_special_cases(repo_tools):
    assert "binary file" in repo_tools.read_file("image.png")
    assert "not returned" in repo_tools.read_file("big.txt")
    assert "empty" in repo_tools.read_file("empty.txt")
    assert repo_tools.read_file("missing.py").startswith("ERROR: file not found")
    assert "is a directory" in repo_tools.read_file("src")
    assert repo_tools.read_file("notes.txt", start_line=5000).startswith("ERROR:")


# -- search_code -----------------------------------------------------------------------------
def test_search_code_finds_matches(repo_tools):
    out = repo_tools.search_code(r"API_KEY\s*=")
    assert "1 match(es)" in out
    assert "config/settings.py:2: API_KEY = 'abc123'" in out


def test_search_code_glob_and_case(repo_tools):
    out = repo_tools.search_code("import os", path_glob="src/*.py")
    assert "src/app.py:1:" in out
    out = repo_tools.search_code("DEBUG", path_glob="*.py", case_insensitive=True)
    assert "config/settings.py:1:" in out
    assert "0 match(es)" in repo_tools.search_code("DEBUG", path_glob="*.md")


def test_search_code_skips_binary_and_caps_results(repo_tools):
    out = repo_tools.search_code("line", path_glob="notes.txt", max_results=5)
    assert "1000 match(es)" in out and "showing first 5" in out
    assert len([ln for ln in out.splitlines() if ln.startswith("notes.txt:")]) == 5
    assert "skipped" in repo_tools.search_code("x")  # big.txt / image.png skipped


def test_search_code_invalid_regex(repo_tools):
    assert repo_tools.search_code("([unclosed").startswith("ERROR: invalid regular expression")
    assert repo_tools.search_code("").startswith("ERROR:")


# -- git_log ---------------------------------------------------------------------------------
def test_git_log_lists_commits_and_files(repo_tools):
    out = repo_tools.git_log()
    assert "3 most recent commit(s)" in out
    assert "remove env file" in out and "D .env" in out and "A .env" in out
    assert "Dev One" in out and "dev@example.com" not in out  # no e-mail addresses
    first = out.index("remove env file")
    assert first < out.index("add env file") < out.index("initial commit")


def test_git_log_path_filter_and_limit(repo_tools):
    out = repo_tools.git_log(path=".env")
    assert "2 most recent commit(s) touching '.env'" in out
    assert "initial commit" not in out
    assert "1 most recent commit(s)" in repo_tools.git_log(max_count=1)
