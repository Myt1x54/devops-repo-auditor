"""Fixture repositories mirroring the hidden evaluation (A obvious, B subtle, C healthy).

Each fixture is a list of commits (files to write / delete) and an answer key. Repositories are
built at runtime with git, so this source file never contains a committed .env, binary artefacts
or anything that looks like a real credential (fake secrets are assembled from pieces).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Fake credentials, assembled so no literal secret-looking string appears in this file.
FAKE_DB_PASSWORD = "Sup3r" + "S3cret" + "Passw0rd!"
FAKE_API_KEY = "sk_" + "live_" + "51HfQ2kLmNoPqRsTuVwXyZ01"
FAKE_TOKEN = "ghp_" + "Zx9" + "Q2wE4rT6yU8iO0pA1sD3fG5hJ7kL9zX1cV3b"


@dataclass(frozen=True)
class Expected:
    """One known violation. A prediction matches when its rule is in ``rules`` and its file
    matches one of ``files`` (glob patterns; ``None`` = a finding without a file)."""

    id: str
    rules: tuple[str, ...]
    files: tuple[str | None, ...]
    note: str = ""
    lines: tuple[int, ...] = ()  # optional: disambiguates two items in the same file


@dataclass
class Commit:
    message: str
    write: dict[str, str | bytes] = field(default_factory=dict)
    delete: tuple[str, ...] = ()


@dataclass
class Fixture:
    name: str
    description: str
    commits: list[Commit]
    expected: list[Expected]
    # Debatable findings: neither rewarded nor penalised if reported.
    optional: list[Expected] = field(default_factory=list)

    def build(self, root: Path) -> Path:
        repo = root / self.name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        for commit in self.commits:
            for rel, content in commit.write.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8", newline="\n")
            for rel in commit.delete:
                _git(repo, "rm", "-q", "-r", rel)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", commit.message)
        return repo


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Fixture Dev", "-c", "user.email=dev@example.com",
         "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )  # fmt: skip


# ---------------------------------------------------------------------------------------------
# A - obvious issues: a small Flask app with no hygiene at all.
# ---------------------------------------------------------------------------------------------
A_FILES: dict[str, str | bytes] = {
    "app.py": (
        "from flask import Flask\n"
        "import config\n\n"
        "app = Flask(__name__)\n\n\n"
        "@app.route('/')\n"
        "def index():\n"
        "    return 'orders'\n\n\n"
        "if __name__ == '__main__':\n"
        "    app.run(debug=True)\n"
    ),
    "config.py": (
        "DB_HOST = 'localhost'\n"
        "DB_USER = 'admin'\n"
        f"DB_PASSWORD = '{FAKE_DB_PASSWORD}'\n"
        f"PAYMENT_API_KEY = '{FAKE_API_KEY}'\n"
    ),
    "requirements.txt": "flask\nrequests\nsqlalchemy\n",
    ".env": f"SECRET_KEY={FAKE_TOKEN}\nDB_PASSWORD={FAKE_DB_PASSWORD}\n",
    "__pycache__/config.cpython-312.pyc": b"\x00\x0d\x0d\x0a" + b"\x00" * 40,
    "__pycache__/app.cpython-312.pyc": b"\x00\x0d\x0d\x0a" + b"\x01" * 40,
    ".DS_Store": b"\x00\x00\x00\x01Bud1" + b"\x00" * 30,
    "server.log": "2026-01-01 INFO started\n2026-01-01 ERROR db timeout\n",
}

FIXTURE_A = Fixture(
    name="a_obvious",
    description="Obvious issues: secrets, committed artefacts, no README/.gitignore/tests/CI/lint.",
    commits=[Commit("initial version", A_FILES)],
    expected=[
        Expected("A1", ("HYG-001",), (None, "README*"), "no README"),
        Expected("A2", ("HYG-003",), (None, ".gitignore"), "no .gitignore"),
        Expected(
            "A3",
            ("HYG-004",),
            (None, "__pycache__/*", "*.pyc", "*.log", "server.log"),
            "compiled files / log committed",
        ),
        Expected("A4", ("HYG-005",), (".DS_Store", None), "OS file committed"),
        Expected("A5", ("SEC-003", "SEC-001"), (".env",), ".env with secrets committed"),
        Expected("A6", ("SEC-001",), ("config.py",), "hardcoded DB password and API key"),
        Expected("A7", ("DEP-001",), ("requirements.txt",), "unpinned dependencies"),
        Expected("A8", ("TST-001",), (None,), "no tests"),
        Expected("A9", ("CI-001",), (None,), "no CI"),
        Expected("A10", ("CQ-001",), (None,), "no linter/formatter"),
    ],  # fmt: skip
    optional=[
        Expected("A-o1", ("DEP-003", "DEP-002"), (None, "requirements.txt"), "lockfile"),
        Expected("A-o2", ("SEC-009", "CFG-003"), ("app.py", "config.py", None), "debug=True"),
        Expected("A-o3", ("CFG-003", "CFG-004"), ("config.py", None), "no env separation"),
        Expected("A-o4", ("HYG-004", "HYG-007"), ("server.log",), "log file"),
        Expected("A-o5", ("COL-002", "COL-003", "HYG-002"), (None,), "docs/collab"),
    ],
)

# ---------------------------------------------------------------------------------------------
# B - subtle CI / security / configuration defects in an otherwise tidy project.
# ---------------------------------------------------------------------------------------------
B_CI = """\
name: CI
on:
  push:
    branches: [main]
  pull_request_target:
    types: [opened, synchronize]
permissions: write-all
jobs:
  checks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - name: Announce
        run: echo "Testing PR: ${{ github.event.pull_request.title }}"
      - uses: astral-sh/setup-uv@master
      - run: uv sync
      - name: Lint
        run: uv run ruff check .
        continue-on-error: true
      - name: Tests
        run: uv run pytest || true
"""

B_DEPLOY = """\
name: Deploy
on:
  push:
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy to production
        run: ./scripts/deploy.sh production
        env:
          DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}
"""

B_FILES: dict[str, str | bytes] = {
    "README.md": (
        "# Orders service\n\nInternal order-processing API.\n\n## Setup\n\n"
        "```bash\nuv sync\nuv run pytest\n```\n\n## Run\n\n```bash\nuv run python -m orders\n```\n"
    ),
    ".gitignore": "__pycache__/\n*.pyc\n.venv/\ndist/\n.pytest_cache/\n",
    "pyproject.toml": (
        '[project]\nname = "orders"\nversion = "1.4.0"\nrequires-python = ">=3.11"\n'
        'dependencies = ["fastapi>=0.110,<1", "pydantic>=2.6,<3"]\n\n'
        '[dependency-groups]\ndev = ["pytest>=8", "ruff>=0.5"]\n\n'
        "[tool.ruff]\nline-length = 100\n\n"
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n\n'
        '[tool.coverage.run]\nsource = ["legacy_billing"]\n'
    ),
    "uv.lock": 'version = 1\nrequires-python = ">=3.11"\n\n[[package]]\nname = "fastapi"\n',
    "orders/__init__.py": "",
    "orders/service.py": (
        "from orders.settings import DATA_DIR\n\n\n"
        "def load_orders():\n"
        "    return (DATA_DIR / 'orders.csv').read_text()\n"
    ),
    "orders/settings.py": (
        "from pathlib import Path\n\n"
        "DEBUG = True\n"
        "ALLOWED_HOSTS = ['*']\n"
        "DATA_DIR = Path(r'C:\\Users\\jdoe\\projects\\orders\\data')\n"
        "DATABASE_URL = 'postgresql://orders@db.internal:5432/orders'\n"
    ),
    "tests/test_service.py": "def test_placeholder():\n    assert 1 + 1 == 2\n",
    "scripts/deploy.sh": "#!/bin/sh\nset -e\necho deploying to $1\n",
    ".github/workflows/ci.yml": B_CI,
    ".github/workflows/deploy.yml": B_DEPLOY,
}

FIXTURE_B = Fixture(
    name="b_subtle",
    description="Subtle CI/security/config defects; README, tests, lockfile and lint config exist.",
    commits=[
        Commit("initial service", B_FILES),
        Commit("add local env", {".env": f"DEPLOY_TOKEN={FAKE_TOKEN}\n"}),
        Commit("remove env file", delete=(".env",)),
    ],
    expected=[
        Expected(
            "B1",
            ("SEC-006",),
            (".github/workflows/ci.yml",),
            "pull_request_target checks out PR head",
        ),
        Expected("B2", ("SEC-005",), (".github/workflows/ci.yml",), "permissions: write-all"),
        Expected("B3", ("SEC-007",), (".github/workflows/ci.yml",), "PR title in run script"),
        Expected("B4", ("CI-004",), (".github/workflows/ci.yml",), "action pinned to @master"),
        Expected(
            "B5",
            ("TST-003", "CI-003"),
            (".github/workflows/ci.yml",),
            "pytest || true",
            lines=(22, 23),
        ),
        Expected(
            "B6",
            ("CI-003", "CQ-002"),
            (".github/workflows/ci.yml",),
            "lint continue-on-error",
            lines=(19, 20, 21),
        ),
        Expected(
            "B7",
            ("BLD-002",),
            (".github/workflows/deploy.yml",),
            "deploy on every push without tests/gates",
        ),
        Expected("B8", ("SEC-004", "SEC-003"), (".env",), ".env still in git history"),
        Expected("B9", ("CFG-001",), ("orders/settings.py",), "machine-specific path"),
        Expected(
            "B10", ("SEC-009", "CFG-003"), ("orders/settings.py",), "DEBUG=True / ALLOWED_HOSTS=*"
        ),
        Expected(
            "B11",
            ("TST-005", "CFG-005"),
            ("pyproject.toml",),
            "coverage source names another package",
        ),
    ],  # fmt: skip
    optional=[
        Expected(
            "B-o1",
            ("CFG-003", "CFG-004", "SEC-009", "CFG-001"),
            ("orders/settings.py", None),
            "hardcoded DB URL / env separation",
        ),
        Expected(
            "B-o2",
            ("TST-002", "BLD-001", "CI-002"),
            (".github/workflows/deploy.yml", ".github/workflows/ci.yml", None),
            "no build validation / trigger details",
        ),
        Expected("B-o3", ("COL-002", "COL-003", "CFG-004", "HYG-002"), (None,), "docs/collab"),
        Expected(
            "B-o4",
            ("SEC-008", "SEC-005"),
            (".github/workflows/deploy.yml",),
            "deploy secrets on any branch",
        ),
        # True but not injected on purpose: .gitignore does not list .env (and one was committed).
        Expected("B-o5", ("HYG-003",), (".gitignore", None), ".gitignore lacks .env"),
    ],  # fmt: skip
)

# ---------------------------------------------------------------------------------------------
# C - mostly healthy project (false-positive check).
# ---------------------------------------------------------------------------------------------
C_CI = """\
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
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: ruff format --check .
      - run: pytest --cov=textstats
  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build
      - run: python -m build
"""

C_RELEASE = """\
name: Release
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build && python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
"""

C_FILES: dict[str, str | bytes] = {
    "README.md": (
        "# textstats\n\nSmall library for text statistics.\n\n## Install\n\n"
        "```bash\npip install textstats\n```\n\n## Development\n\n"
        '```bash\npip install -e ".[dev]"\npytest\n```\n'
    ),
    "LICENSE": "MIT License\n\nCopyright (c) 2026\n",
    ".gitignore": "__pycache__/\n*.pyc\n.venv/\ndist/\nbuild/\n*.egg-info/\n.env\n.pytest_cache/\n",
    ".env.example": "TEXTSTATS_LOG_LEVEL=info\n",
    "pyproject.toml": (
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[project]\nname = "textstats"\nversion = "0.3.1"\nrequires-python = ">=3.11"\n'
        'dependencies = ["regex>=2023.0"]\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.5"]\n\n'
        '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F", "I"]\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n\n'
        '[tool.coverage.run]\nsource = ["textstats"]\n'
    ),
    ".pre-commit-config.yaml": (
        "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.5.0\n"
        "    hooks:\n      - id: ruff\n      - id: ruff-format\n"
    ),
    "src/textstats/__init__.py": (
        "import os\n\n"
        "LOG_LEVEL = os.getenv('TEXTSTATS_LOG_LEVEL', 'info')\n\n\n"
        "def word_count(text: str) -> int:\n"
        "    return len(text.split())\n"
    ),
    "tests/test_word_count.py": (
        "from textstats import word_count\n\n\n"
        "def test_word_count():\n"
        "    assert word_count('a b c') == 3\n"
    ),
    "tests/conftest.py": "API_KEY = 'test-key'  # dummy value for tests\n",
    ".github/workflows/ci.yml": C_CI,
    ".github/workflows/release.yml": C_RELEASE,
}

FIXTURE_C = Fixture(
    name="c_healthy",
    description="Mostly healthy library: everything in place, a few debatable nits only.",
    commits=[
        Commit("initial library", C_FILES),
        Commit(
            "add contributing guide",
            {"CONTRIBUTING.md": "# Contributing\n\nOpen an issue first; PRs need passing CI.\n"},
        ),
    ],
    expected=[],
    optional=[
        Expected(
            "C-o1",
            ("CI-004",),
            (".github/workflows/release.yml",),
            "pypi publish action pinned to release/v1 branch (debatable)",
        ),
        Expected("C-o2", ("COL-003", "HYG-002", "DEP-003"), (None,), "templates/lockfile nits"),
        Expected("C-o3", ("BLD-003",), (".github/workflows/release.yml",), "publishing nits"),
    ],  # fmt: skip
)

FIXTURES = [FIXTURE_A, FIXTURE_B, FIXTURE_C]
