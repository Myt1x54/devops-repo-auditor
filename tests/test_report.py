from __future__ import annotations

import json

import pytest

from auditor.errors import AuditError
from auditor.findings import Finding
from auditor.report import FIELDS, build_report, validate_report, write_report


def f(rule, category, severity, file=None, line=None):
    return Finding(
        id="F",
        rule_id=rule,
        category=category,
        severity=severity,
        file=file,
        line=line,
        description="A problem description",
        evidence="Observed evidence text",
        recommendation="A concrete recommendation",
    )


def test_build_report_orders_and_keeps_contract_fields():
    report = build_report(
        "https://github.com/a/b",
        [
            f("HYG-003", "repository_hygiene", "low"),
            f("SEC-001", "security", "critical", "config.py", 3),
            f("CI-003", "ci_cd", "high", ".github/workflows/ci.yml", 9),
            f("TST-003", "testing", "high", ".github/workflows/ci.yml", 12),
        ],
    )
    assert report["repository"] == "https://github.com/a/b"
    assert [v["rule_id"] for v in report["violations"]] == [
        "SEC-001",
        "TST-003",
        "CI-003",
        "HYG-003",
    ]
    first = report["violations"][0]
    assert tuple(first) == FIELDS and first["file"] == "config.py" and first["line"] == 3
    assert report["violations"][-1]["file"] is None and report["violations"][-1]["line"] is None


def test_empty_report_is_valid():
    assert build_report("https://github.com/a/b", []) == {
        "repository": "https://github.com/a/b",
        "violations": [],
    }


GOOD = {
    "rule_id": "SEC-001",
    "category": "security",
    "severity": "critical",
    "file": "config.py",
    "line": 17,
    "description": "Hardcoded API credential",
    "evidence": "Credential assigned directly in source code",
    "recommendation": "Use a secrets manager or environment variable",
}


def test_assignment_example_validates():
    validate_report({"repository": "https://github.com/example/demo-project", "violations": [GOOD]})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"severity": "urgent"}, "severity"),
        ({"category": "ml"}, "category"),
        ({"line": 0}, "line"),
        ({"line": True}, "line"),
        ({"file": None}, "line requires a file"),
        ({"file": ""}, "file"),
        ({"description": " "}, "description"),
        ({"evidence": "token = 'q8Zr2LmX9vB4nT7kP1wY6cD3'"}, "secret"),
    ],
)
def test_contract_violations_rejected(change, message):
    with pytest.raises(AuditError, match=message):
        validate_report({"repository": "r", "violations": [{**GOOD, **change}]})


def test_structure_errors():
    with pytest.raises(AuditError):
        validate_report({"repository": "r"})
    with pytest.raises(AuditError):
        validate_report({"repository": "r", "violations": {}})
    with pytest.raises(AuditError, match="exactly the fields"):
        validate_report({"repository": "r", "violations": [{**GOOD, "extra": 1}]})


def test_write_report_atomic_and_valid_json(tmp_path):
    out = tmp_path / "sub" / "report.json"
    write_report(out, {"repository": "r", "violations": [GOOD]})
    assert json.loads(out.read_text(encoding="utf-8"))["violations"][0] == GOOD
    assert [p.name for p in out.parent.iterdir()] == ["report.json"]  # no temp files left
    with pytest.raises(AuditError):
        write_report(tmp_path / "bad.json", {"repository": "r", "violations": [{"x": 1}]})
    assert not (tmp_path / "bad.json").exists()
