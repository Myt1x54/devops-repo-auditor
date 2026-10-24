"""Final JSON report: assembly, contract validation and atomic writing (S9, D10, D26).

Output contract (assignment section 5):
    {"repository": str, "violations": [ {rule_id, category, severity, file, line,
                                        description, evidence, recommendation}, ... ]}
- every violation has all eight fields; severity in critical|high|medium|low;
- file/line are JSON null when not applicable; no secret values anywhere.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from auditor.errors import AuditError
from auditor.findings import CATEGORIES, SEVERITIES, Finding
from auditor.redact import redact

FIELDS = (
    "rule_id",
    "category",
    "severity",
    "file",
    "line",
    "description",
    "evidence",
    "recommendation",
)
_SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}
_CATEGORY_ORDER = {c: i for i, c in enumerate(CATEGORIES)}


def build_report(repository: str, findings: Iterable[Finding]) -> dict[str, Any]:
    """Assemble the report, ordered by severity, category, file and line."""
    ordered = sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            _CATEGORY_ORDER.get(f.category, 99),
            f.file or "",
            f.line or 0,
            f.rule_id,
        ),
    )
    report = {"repository": repository, "violations": [f.report_dict() for f in ordered]}
    validate_report(report)
    return report


def validate_report(report: Any) -> None:
    """Raise AuditError if ``report`` violates the output contract."""
    problems: list[str] = []
    if not isinstance(report, dict) or set(report) != {"repository", "violations"}:
        raise AuditError("Report must be an object with exactly 'repository' and 'violations'.")
    if not isinstance(report["repository"], str) or not report["repository"]:
        problems.append("repository must be a non-empty string")
    violations = report["violations"]
    if not isinstance(violations, list):
        raise AuditError("Report 'violations' must be a list.")
    for i, v in enumerate(violations):
        where = f"violations[{i}]"
        if not isinstance(v, dict) or tuple(v) != FIELDS:
            problems.append(f"{where} must have exactly the fields {FIELDS}")
            continue
        for key in ("rule_id", "category", "severity", "description", "evidence", "recommendation"):
            if not isinstance(v[key], str) or not v[key].strip():
                problems.append(f"{where}.{key} must be a non-empty string")
        if v["severity"] not in SEVERITIES:
            problems.append(f"{where}.severity must be one of {SEVERITIES}")
        if v["category"] not in CATEGORIES:
            problems.append(f"{where}.category must be one of {tuple(CATEGORIES)}")
        if v["file"] is not None and (not isinstance(v["file"], str) or not v["file"]):
            problems.append(f"{where}.file must be a non-empty string or null")
        if v["line"] is not None and (
            isinstance(v["line"], bool) or not isinstance(v["line"], int) or v["line"] < 1
        ):
            problems.append(f"{where}.line must be a positive integer or null")
        if v["line"] is not None and v["file"] is None:
            problems.append(f"{where}.line requires a file")
        for key in ("description", "evidence", "recommendation"):
            if isinstance(v[key], str) and redact(v[key]) != v[key]:
                problems.append(f"{where}.{key} contains a secret-looking value")
    if problems:
        raise AuditError("Report failed validation: " + "; ".join(problems[:5]))


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Validate, then write atomically so a crash never leaves half-written JSON."""
    validate_report(report)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
