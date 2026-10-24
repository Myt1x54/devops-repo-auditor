from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REAL_SETTINGS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "AUDITOR_PROVIDER",
    "AUDITOR_MODEL",
    "AUDITOR_EFFORT",
    "AUDITOR_MAX_STEPS",
)


@pytest.fixture(autouse=True)
def isolate_from_real_settings(monkeypatch):
    """Tests never read the developer's .env or call a real LLM API."""
    for name in _REAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("agent.load_env", lambda *a, **k: [])


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A small git repository with two commits, usable as a clone source."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial commit")
    (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add requirements")
    return repo


@pytest.fixture
def local_repo_url(local_repo: Path) -> str:
    return local_repo.as_uri()  # file:// URL so --depth is honoured
