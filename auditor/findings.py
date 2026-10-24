"""Findings recorded by the agent and the shared audit state (D6, D9).

S7 provides the data model, the allowed categories/severities and the bookkeeping the control
tools need (record / retract / observed files). S9 adds the rule catalogue, evidence
verification and the final report.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Category value -> rule-id prefix (D6). Values match the assignment's nine inspection areas.
CATEGORIES: dict[str, str] = {
    "repository_hygiene": "HYG",
    "dependency_management": "DEP",
    "code_quality": "CQ",
    "testing": "TST",
    "ci_cd": "CI",
    "security": "SEC",
    "configuration": "CFG",
    "collaboration": "COL",
    "build_release": "BLD",
}
SEVERITIES = ("critical", "high", "medium", "low")
RULE_ID_RE = re.compile(r"^(HYG|DEP|CQ|TST|CI|SEC|CFG|COL|BLD)-\d{3}$")


@dataclass
class Finding:
    id: str  # F1, F2, ... (internal, not part of the report)
    rule_id: str
    category: str
    severity: str
    file: str | None
    line: int | None
    description: str
    evidence: str
    recommendation: str
    retracted: bool = False
    retract_reason: str = ""

    def report_dict(self) -> dict:
        """The eight fields required by the output contract."""
        d = asdict(self)
        return {
            k: d[k]
            for k in (
                "rule_id",
                "category",
                "severity",
                "file",
                "line",
                "description",
                "evidence",
                "recommendation",
            )
        }


@dataclass
class AuditState:
    """Everything the control tools and the loop share during one audit."""

    findings: list[Finding] = field(default_factory=list)
    read_files: set[str] = field(default_factory=set)  # content shown (read/search/workflows/ruff)
    seen_files: set[str] = field(default_factory=set)  # named in any tool output (incl. listings)
    history_files: set[str] = field(default_factory=set)  # seen only in git history output
    tool_calls: int = 0
    finished: bool = False
    summary: str = ""
    categories_declared: set[str] = field(default_factory=set)  # from finish_audit
    coverage_nudged: bool = False

    def next_id(self) -> str:
        return f"F{len(self.findings) + 1}"

    def active_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.retracted]

    def find_duplicate(self, rule_id: str, file: str | None, line: int | None) -> Finding | None:
        for f in self.active_findings():
            if (f.rule_id, f.file, f.line) == (rule_id, file, line):
                return f
        return None

    def categories_examined(self) -> set[str]:
        return {f.category for f in self.active_findings()}
