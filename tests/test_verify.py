from __future__ import annotations

from auditor.findings import AuditState, Finding
from auditor.verify import verify_findings

FILES = {
    "pyproject.toml": (
        "[tool.x]\nname = 'demo'\n\n[tool.coverage.run]\n" + 'source = ["jinja2", "tests"]\n'
    ),
    "ci.yml": "steps:\n  - run: pytest || true\n  - run: ruff check . || true\n",
    "big.bin": None,
}


def read_text(path):
    return FILES.get(path)


def finding(fid, rule="CFG-005", file="pyproject.toml", line=5, evidence="", **kw):
    return Finding(
        id=fid,
        rule_id=rule,
        category=kw.pop("category", "configuration"),
        severity=kw.pop("severity", "medium"),
        file=file,
        line=line,
        description=kw.pop("description", "Coverage configured for another package"),
        evidence=evidence or 'Line shows `source = ["jinja2", "tests"]`',
        recommendation=kw.pop("recommendation", "Point coverage at this package"),
        **kw,
    )


def state_with(*findings, read=(), seen=(), history=()):
    s = AuditState(findings=list(findings))
    s.read_files, s.seen_files, s.history_files = set(read), set(seen), set(history)
    return s


def test_correct_finding_kept_unchanged():
    kept, notes = verify_findings(state_with(finding("F1"), read={"pyproject.toml"}), read_text)
    assert [(f.id, f.line) for f in kept] == [("F1", 5)] and notes == []


def test_unobserved_file_dropped():
    kept, notes = verify_findings(state_with(finding("F1")), read_text)
    assert kept == [] and notes[0].action == "dropped"


def test_existence_finding_on_listed_file_kept_without_line():
    f = finding("F1", rule="HYG-004", file="big.bin", line=1, category="repository_hygiene")
    kept, notes = verify_findings(state_with(f, seen={"big.bin"}), read_text)
    assert kept[0].file == "big.bin" and kept[0].line is None
    assert notes[0].action == "line_removed"


def test_content_claim_on_file_only_listed_is_dropped():
    kept, notes = verify_findings(state_with(finding("F1"), seen={"pyproject.toml"}), read_text)
    assert kept == [] and notes[0].action == "dropped"
    assert "content was never shown" in notes[0].reason


def test_wrong_line_corrected_using_longest_snippet():
    f = finding("F1", line=2)  # model cited the wrong line
    kept, notes = verify_findings(state_with(f, read={"pyproject.toml"}), read_text)
    assert kept[0].line == 5
    assert notes[0].action == "line_corrected" and "2 -> 5" in notes[0].reason


def test_line_kept_when_snippet_not_found():
    f = finding("F1", line=4, evidence="Section [tool.coverage.run] is misconfigured")
    kept, notes = verify_findings(state_with(f, read={"pyproject.toml"}), read_text)
    assert kept[0].line == 4 and notes == []


def test_duplicates_merged_with_extra_lines():
    a = finding(
        "F1",
        rule="TST-003",
        file="ci.yml",
        line=2,
        evidence="`pytest || true` on line 2",
        category="testing",
    )
    b = finding(
        "F2",
        rule="TST-003",
        file="ci.yml",
        line=3,
        evidence="`ruff check . || true`",
        category="testing",
    )
    kept, notes = verify_findings(state_with(a, b, read={"ci.yml"}), read_text)
    assert len(kept) == 1 and kept[0].id == "F1"
    assert kept[0].evidence.endswith("Also at line(s) 3.")
    assert [n.action for n in notes] == ["merged"]


def test_repository_wide_findings_merge_by_rule():
    a = finding("F1", rule="TST-001", file=None, line=None, category="testing")
    b = finding("F2", rule="TST-001", file=None, line=None, category="testing")
    c = finding("F3", rule="HYG-001", file=None, line=None, category="repository_hygiene")
    kept, _ = verify_findings(state_with(a, b, c), read_text)
    assert [f.id for f in kept] == ["F1", "F3"]


def test_history_only_file_accepted():
    f = finding("F1", rule="SEC-004", file=".env", line=None, category="security")
    kept, _ = verify_findings(state_with(f, history={".env"}), read_text)
    assert kept[0].file == ".env"


def test_retracted_findings_excluded_and_original_not_mutated():
    keep = finding("F1", line=2)
    gone = finding("F2", rule="CFG-001")
    gone.retracted = True
    kept, _ = verify_findings(state_with(keep, gone, read={"pyproject.toml"}), read_text)
    assert [f.id for f in kept] == ["F1"] and keep.line == 2  # agent's record untouched


def test_text_cleaned_redacted_and_capped():
    f = finding(
        "F1",
        evidence="token = 'q8Zr2LmX9vB4nT7kP1wY6cD3'   found\n\non   line 5" + " x" * 400,
    )
    kept, _ = verify_findings(state_with(f, read={"pyproject.toml"}), read_text)
    ev = kept[0].evidence
    assert "q8Zr2" not in ev and "[REDACTED:" in ev
    assert "\n" not in ev and "  " not in ev and len(ev) <= 600 and ev.endswith("...")


def test_two_rules_at_same_location_merged_keeping_more_severe():
    a = finding("F1", rule="CFG-005", line=5, severity="medium")
    b = finding("F2", rule="TST-005", line=5, severity="high", category="testing")
    c = finding("F3", rule="SEC-009", line=None, severity="low", category="security")
    kept, notes = verify_findings(state_with(a, b, c, read={"pyproject.toml"}), read_text)
    assert [f.id for f in kept] == ["F2", "F3"]
    assert notes[-1].action == "merged" and notes[-1].finding_id == "F1"
