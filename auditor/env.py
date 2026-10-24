"""Loading settings from a ``.env`` file (D21)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, find_dotenv


def load_env(path: str | Path | None = None) -> list[str]:
    """Load ``.env`` so that its *non-empty* values win over the shell environment.

    - A stale ``ANTHROPIC_API_KEY`` exported in the shell can't shadow the key in ``.env``.
    - An empty value (e.g. a fresh copy of ``.env.example``) never wipes out a working
      key that the evaluator exported in the shell.

    Returns the names of the variables that were set (never their values).
    """
    env_file = Path(path) if path else find_dotenv(usecwd=True)
    if not env_file or not Path(env_file).is_file():
        return []
    loaded = []
    for key, value in dotenv_values(env_file).items():
        if value is not None and value.strip():
            os.environ[key] = value.strip()
            loaded.append(key)
    return loaded
