"""Harness-side evidence verification of recorded findings (D9, D26).

Runs after the agent loop, before the report is written. Unsupported claims count as false
positives and duplicates earn nothing, so every active finding is checked against what the tools
actually showed:

1. the cited file must have been observed by a tool in this run (else: dropped);
2. a cited line needs the file's content to have been shown (else: line -> null);
3. the cited line is checked against snippets quoted in the evidence/description; when the snippet
   is on a different line it is corrected, when it is nowhere in the file the line is kept as-is;
4. findings with the same rule and file are merged (other lines listed in the evidence);
5. all text fields are redacted and length-capped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from auditor.findings import SEVERITIES, AuditState, Finding
from auditor.redact import redact
from auditor.rules import EXISTENCE_RULES

MAX_TEXT = {"description": 300, "evidence": 600, "recommendation": 400}
_SNIPPET_RES = (
    re.compile(r"`([^`\n]{4,200})`"),  # `code`
    re.compile(r"\"([^\"\n]{4,120})\""),  # "text"
    re.compile(r"'([^'\n]{4,120})'"),  # 'text'
)


@dataclass
class VerificationNote:
    finding_id: str
    rule_id: str
    action: str  # dropped | line_removed | line_corrected | merged | kept
    reason: str


def _snippets(*texts: str) -> list[str]:
    found: list[str] = []
    for text in texts:
        for rx in _SNIPPET_RES:
            for m in rx.finditer(text):
                s = m.group(1).strip()
                if len(s) >= 4 and "REDACTED" not in s and s not in found:
                    found.append(s)
    return found


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _locate(lines: list[str], snippets: list[str]) -> list[int]:
    """Lines containing the most specific snippet that occurs in the file.

    Snippets are tried longest first, so `source = ["jinja2", "tests"]` wins over "tests".
    Matching ignores case and whitespace differences.
    """
    normalized = [_norm(line) for line in lines]
    for snippet in sorted(snippets, key=len, reverse=True):
        wanted = _norm(snippet)
        hits = [n for n, line in enumerate(normalized, start=1) if wanted and wanted in line]
        if hits:
            return hits
    return []


def verify_findings(
    state: AuditState, read_text: Callable[[str], str | None]
) -> tuple[list[Finding], list[VerificationNote]]:
    """Return verified, merged findings and notes explaining every change."""
    notes: list[VerificationNote] = []
    observed = state.read_files | state.seen_files | state.history_files
    checked: list[Finding] = []

    for f in state.active_findings():
        f = replace(f)  # never mutate the agent's record
        if f.file is not None and f.file not in observed:
            notes.append(
                VerificationNote(f.id, f.rule_id, "dropped", f"'{f.file}' was never observed")
            )
            continue
        if (
            f.file is not None
            and f.file not in state.read_files
            and f.rule_id not in EXISTENCE_RULES
        ):
            notes.append(
                VerificationNote(
                    f.id,
                    f.rule_id,
                    "dropped",
                    f"claim about the content of '{f.file}', but its content was never shown",
                )
            )
            continue
        if f.file is not None and f.line is not None:
            if f.file not in state.read_files:
                notes.append(
                    VerificationNote(
                        f.id, f.rule_id, "line_removed", "file content was never shown"
                    )
                )
                f.line = None
            else:
                text = read_text(f.file)
                if text is not None:
                    _check_line(f, text.splitlines(), notes)
        checked.append(f)

    merged = _merge(checked, notes)
    return [_clean(f) for f in merged], notes


def _check_line(f: Finding, lines: list[str], notes: list[VerificationNote]) -> None:
    snippets = _snippets(f.evidence, f.description)
    if not snippets or f.line is None:
        return
    hits = _locate(lines, snippets)
    if not hits or f.line in hits:
        return
    near = [h for h in hits if abs(h - f.line) <= 2]
    best = near[0] if near else hits[0]
    notes.append(
        VerificationNote(
            f.id,
            f.rule_id,
            "line_corrected",
            f"line {f.line} -> {best} (quoted text found there)",
        )
    )
    f.line = best


def _merge(findings: list[Finding], notes: list[VerificationNote]) -> list[Finding]:
    """One finding per (rule_id, file); extra lines are mentioned in the evidence."""
    groups: dict[tuple[str, str | None], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.rule_id, f.file), []).append(f)
    out = []
    for group in groups.values():
        group.sort(key=lambda f: (f.line is None, f.line or 0))
        keep = group[0]
        extra_lines = sorted({g.line for g in group[1:] if g.line and g.line != keep.line})
        for dup in group[1:]:
            notes.append(VerificationNote(dup.id, dup.rule_id, "merged", f"duplicate of {keep.id}"))
        if extra_lines:
            keep.evidence = (
                f"{keep.evidence.rstrip('.')}. Also at line(s) {', '.join(map(str, extra_lines))}."
            )
        out.append(keep)
    return _merge_same_location(out, notes)


def _merge_same_location(findings: list[Finding], notes: list[VerificationNote]) -> list[Finding]:
    """Two rules cited for the exact same file and line describe one problem: keep the more
    severe one (first recorded on ties). Findings without a line are never merged here."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    by_location: dict[tuple[str, int], Finding] = {}
    out: list[Finding] = []
    for f in findings:
        if f.file is None or f.line is None:
            out.append(f)
            continue
        key = (f.file, f.line)
        other = by_location.get(key)
        if other is None:
            by_location[key] = f
            out.append(f)
            continue
        keep, drop = (other, f) if order[other.severity] <= order[f.severity] else (f, other)
        if keep is f:
            out[out.index(other)] = f
            by_location[key] = f
        notes.append(
            VerificationNote(
                drop.id, drop.rule_id, "merged", f"same location as {keep.id} ({keep.rule_id})"
            )
        )
    return out


def _clean(f: Finding) -> Finding:
    for name, limit in MAX_TEXT.items():
        value = redact(" ".join(getattr(f, name).split()))
        if len(value) > limit:
            value = value[: limit - 3].rstrip() + "..."
        setattr(f, name, value)
    return f
