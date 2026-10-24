from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from auditor.errors import AuditError
from auditor.workspace import RepoWorkspace, _describe_clone_failure


@pytest.fixture
def created_tmp_dirs(monkeypatch):
    """Record temp dirs created by the workspace so tests can assert they were removed."""
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr("auditor.workspace.tempfile.mkdtemp", recording_mkdtemp)
    return created


def test_clone_and_cleanup(local_repo_url, created_tmp_dirs):
    with RepoWorkspace(local_repo_url) as ws:
        assert ws.root is not None
        assert (ws.root / "README.md").is_file()
        assert (ws.root / "requirements.txt").is_file()
        assert ws.head and len(ws.head) == 40
        log = ws.run_git("log", "--format=%s")
        assert "add requirements" in log and "initial commit" in log
    assert created_tmp_dirs and not Path(created_tmp_dirs[0]).exists()
    assert ws.root is None


def test_shallow_depth(local_repo_url):
    with RepoWorkspace(local_repo_url, depth=1) as ws:
        assert ws.run_git("rev-list", "--count", "HEAD").strip() == "1"


def test_clone_does_not_modify_source(local_repo, local_repo_url):
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=local_repo, capture_output=True, text=True
    ).stdout
    with RepoWorkspace(local_repo_url):
        pass
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=local_repo, capture_output=True, text=True
    ).stdout
    assert before == after == ""


def test_missing_repo_raises_and_cleans_up(tmp_path, created_tmp_dirs):
    missing = (tmp_path / "does-not-exist").as_uri()
    with pytest.raises(AuditError, match="Could not clone"), RepoWorkspace(missing):
        pass
    assert created_tmp_dirs and not Path(created_tmp_dirs[0]).exists()


def test_size_limit_raises_and_cleans_up(local_repo_url, created_tmp_dirs):
    with pytest.raises(AuditError, match="too large"), RepoWorkspace(local_repo_url, max_bytes=1):
        pass
    assert not Path(created_tmp_dirs[0]).exists()


def test_run_git_failure_raises(local_repo_url):
    with RepoWorkspace(local_repo_url) as ws, pytest.raises(AuditError):
        ws.run_git("show", "no-such-revision")


def test_run_git_requires_open_workspace():
    with pytest.raises(AuditError, match="not open"):
        RepoWorkspace("unused").run_git("status")


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("remote: Repository not found.\nfatal: ...", "not found or not public"),
        ("fatal: could not read Username for 'https://github.com'", "not found or not public"),
        ("fatal: unable to access ...: Could not resolve host: github.com", "network error"),
        ("fatal: something odd\n", "fatal: something odd"),
        ("", "unknown git error"),
    ],
)
def test_describe_clone_failure(stderr, expected):
    assert expected in _describe_clone_failure(stderr)
