from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from auditor.findings import CATEGORIES
from auditor.llm import FakeLLM, call, fake_response
from auditor.rules import RULES
from evaluation.fixtures import FIXTURES, Expected, Fixture
from evaluation.scoring import score_report, summarize

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import evaluate  # noqa: E402


def v(rule, file=None, line=None):
    return {"rule_id": rule, "file": file, "line": line, "description": "d"}


FIX = Fixture(
    name="demo",
    description="",
    commits=[],
    expected=[
        Expected("E1", ("SEC-001",), ("config.py",)),
        Expected("E2", ("TST-003", "CI-003"), (".github/workflows/*.yml",)),
        Expected("E3", ("HYG-001",), (None, "README*")),
    ],
    optional=[Expected("O1", ("DEP-003",), (None,))],
)


def test_scoring_tp_fp_fn_neutral_and_duplicates():
    report = {
        "violations": [
            v("SEC-001", "config.py", 3),
            v("SEC-001", "config.py", 9),  # duplicate of an already matched item -> FP
            v("CI-003", ".github/workflows/ci.yml", 10),  # alternative rule accepted
            v("DEP-003"),  # optional -> neutral
            v("CFG-001", "app.py", 1),  # not in the key -> FP
        ]
    }
    s = score_report(FIX, report)
    assert [e for e, _ in s.tp] == ["E1", "E2"]
    assert [e.id for e in s.fn] == ["E3"]
    assert len(s.fp) == 2 and len(s.neutral) == 1
    assert s.precision == pytest.approx(0.5) and s.recall == pytest.approx(2 / 3)
    assert summarize([s]) == {"tp": 2, "fp": 2, "fn": 1, "precision": 0.5, "recall": 0.667}


def test_empty_report_on_healthy_fixture_is_perfect():
    healthy = Fixture("ok", "", [], expected=[])
    s = score_report(healthy, {"violations": []})
    assert s.precision == 1.0 and s.recall == 1.0


def test_fixture_answer_keys_use_catalogue_rules():
    for fixture in FIXTURES:
        for item in fixture.expected + fixture.optional:
            for rule in item.rules:
                assert rule in RULES, (fixture.name, item.id, rule)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_fixtures_build_as_git_repos(fixture, tmp_path):
    repo = fixture.build(tmp_path)
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert len(log.splitlines()) == len(fixture.commits)


def test_fixture_b_has_env_only_in_history(tmp_path):
    fixture = next(f for f in FIXTURES if f.name == "b_subtle")
    repo = fixture.build(tmp_path)
    assert not (repo / ".env").exists()
    history = subprocess.run(
        ["git", "log", "--name-status", "--format="], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "A\t.env" in history and "D\t.env" in history


def test_evaluate_runs_offline_with_scripted_llm(tmp_path, monkeypatch):
    fixture = next(f for f in FIXTURES if f.name == "a_obvious")
    script = [
        fake_response(call("read_file", {"path": "config.py"})),
        fake_response(
            call(
                "record_finding",
                {
                    "rule_id": "SEC-001",
                    "category": "security",
                    "severity": "critical",
                    "file": "config.py",
                    "line": 3,
                    "description": "Hardcoded database password",
                    "evidence": "config.py line 3 assigns `DB_PASSWORD = '` a literal value",
                    "recommendation": "Read the password from an environment variable",
                },
            )
        ),
        fake_response(
            call("finish_audit", {"summary": "s", "categories_examined": list(CATEGORIES)})
        ),
    ]
    report_path = evaluate.run_fixture(fixture, tmp_path, 10, llm_factory=lambda: FakeLLM(script))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    s = score_report(fixture, report)
    assert [e for e, _ in s.tp] == ["A6"] and s.precision == 1.0
    assert (tmp_path / "a_obvious.trace.jsonl").exists()


def test_scorer_prefers_item_on_the_cited_line():
    fix = Fixture(
        "two_in_one_file",
        "",
        [],
        expected=[
            Expected("T", ("TST-003", "CI-003"), ("ci.yml",), lines=(23,)),
            Expected("L", ("CI-003",), ("ci.yml",), lines=(21,)),
        ],
    )
    s = score_report(fix, {"violations": [v("CI-003", "ci.yml", 21), v("TST-003", "ci.yml", 23)]})
    assert sorted(e for e, _ in s.tp) == ["L", "T"] and not s.fp
