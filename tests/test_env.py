from __future__ import annotations

import os

from auditor.env import load_env


def test_env_file_values_override_shell(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\nAUDITOR_MODEL=\n# comment\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-shell-value")
    monkeypatch.setenv("AUDITOR_MODEL", "shell-model")
    loaded = load_env(env)
    assert loaded == ["ANTHROPIC_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == "from-file"
    assert os.environ["AUDITOR_MODEL"] == "shell-model"  # empty value does not wipe it


def test_missing_env_file_is_fine(tmp_path):
    assert load_env(tmp_path / "nope.env") == []
