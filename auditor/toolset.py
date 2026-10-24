"""The tools offered to the LLM: JSON schemas, descriptions and control tools (S7, D23).

``build_registry()`` wires the inspection tools of ``RepoTools`` and the control tools
(``record_finding``, ``retract_finding``, ``finish_audit``) into a ``ToolRegistry``. It also
records which files each tool showed to the model, so findings can later be checked against
what was actually observed (D9).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from auditor.findings import CATEGORIES, RULE_ID_RE, SEVERITIES, AuditState, Finding
from auditor.registry import ToolRegistry
from auditor.rules import RULES, is_known_or_custom
from tools import RepoTools, ToolError, normalize_repo_path

_HIT_RE = re.compile(r"^([^\s:][^:\n]*?):(\d+):", re.MULTILINE)  # search_code / run_ruff lines
_WORKFLOW_RE = re.compile(r"^=== (\S+)", re.MULTILINE)
_LISTED_RE = re.compile(r"^(\S[^\n]*?)  \((?:\d+ B|unreadable)\)$", re.MULTILINE)
_EXISTS_RE = re.compile(r"^(\S[^:\n]*): EXISTS \(file\)$", re.MULTILINE)
_HISTORY_RE = re.compile(r"^(\S[^\n]*?) \[(?:still tracked|NOT tracked now)", re.MULTILINE)

STR = {"type": "string"}
INT = {"type": "integer"}


def build_registry(repo_tools: RepoTools, state: AuditState) -> ToolRegistry:
    registry = ToolRegistry()
    tracked = set(repo_tools.tracked_files())

    def observe(name: str, func: Callable[..., str]) -> Callable[..., str]:
        """Wrap a tool so the files it shows are recorded in the audit state."""

        def wrapper(**kwargs) -> str:
            output = func(**kwargs)
            state.tool_calls += 1
            if output.startswith("ERROR"):
                return output
            shown: set[str] = set()
            if name == "read_file":
                shown.add(normalize_repo_path(kwargs["path"]))
                state.read_files |= shown
            elif name in ("search_code", "run_ruff"):
                shown = {m.group(1) for m in _HIT_RE.finditer(output)}
                state.read_files |= shown & tracked
            elif name == "inspect_workflows":
                shown = {m.group(1) for m in _WORKFLOW_RE.finditer(output)}
                state.read_files |= shown & tracked
            elif name == "list_files":
                shown = {m.group(1) for m in _LISTED_RE.finditer(output)}
            elif name == "file_exists":
                shown = {m.group(1).lstrip("./") for m in _EXISTS_RE.finditer(output)}
            elif name == "git_file_history":
                names = {m.group(1) for m in _HISTORY_RE.finditer(output)}
                state.history_files |= names - tracked
                shown = names
            state.seen_files |= shown & (tracked | state.history_files)
            return output

        return wrapper

    # -- inspection tools ----------------------------------------------------------------------
    registry.register(
        "list_files",
        "List the files tracked in the repository with their sizes. Large trees are summarised "
        "per directory; call again with path=<dir> to expand a directory. Start here.",
        {
            "properties": {
                "path": {**STR, "description": "Directory relative to the repo root (default '.')"},
                "max_depth": {**INT, "minimum": 1, "description": "Limit listing depth"},
            }
        },
        observe("list_files", repo_tools.list_files),
    )
    registry.register(
        "read_file",
        "Read a file with line numbers (max 400 lines per call; use start_line/end_line for "
        "more). Cite these line numbers in findings.",
        {
            "properties": {
                "path": {**STR, "description": "File path relative to the repo root"},
                "start_line": {**INT, "minimum": 1},
                "end_line": {**INT, "minimum": 1},
            },
            "required": ["path"],
        },
        observe("read_file", repo_tools.read_file),
    )
    registry.register(
        "search_code",
        "Regex search (Python syntax) over all tracked text files. Returns 'file:line: text'. "
        "Use path_glob (e.g. '*.py', '.github/*') to narrow the search.",
        {
            "properties": {
                "pattern": {**STR, "description": "Python regular expression"},
                "path_glob": {**STR, "description": "Optional glob on path or file name"},
                "case_insensitive": {"type": "boolean"},
                "max_results": {**INT, "minimum": 1},
            },
            "required": ["pattern"],
        },
        observe("search_code", repo_tools.search_code),
    )
    registry.register(
        "git_log",
        "Recent commits (newest first): hash, date, author, subject and added/modified/deleted "
        "files. Optionally only commits touching one path.",
        {
            "properties": {
                "max_count": {**INT, "minimum": 1, "description": "Default 20, max 50"},
                "path": {**STR, "description": "Only commits touching this path"},
            }
        },
        observe("git_log", repo_tools.git_log),
    )
    registry.register(
        "inspect_workflows",
        "Structured summary of GitHub Actions workflows: triggers, permissions, jobs, steps "
        "(test/lint/build/deploy), action pinning, secrets used, and 'signals to verify'. Also "
        "reports other CI systems. Confirm details with read_file before reporting.",
        {"properties": {"path": {**STR, "description": "Optional single workflow file"}}},
        observe("inspect_workflows", repo_tools.inspect_workflows),
    )
    registry.register(
        "file_exists",
        "Check many paths or glob patterns at once (e.g. ['requirements.txt', '*.lock', "
        "'tests']). Use it to look for acceptable alternatives before claiming something is "
        "missing.",
        {
            "properties": {"paths": {"type": "array", "items": STR}},
            "required": ["paths"],
        },
        observe("file_exists", repo_tools.file_exists),
    )
    registry.register(
        "git_file_history",
        "Files matching a glob that were ever committed in the available history, with "
        "add/modify/delete events and whether they are still tracked (e.g. '.env', '*.pem').",
        {"properties": {"pattern": STR}, "required": ["pattern"]},
        observe("git_file_history", repo_tools.git_file_history),
    )
    registry.register(
        "search_history",
        "Commits whose changes added or removed lines matching a regex (git log -G). Returns "
        "commit ids and file names only, never the content.",
        {
            "properties": {"pattern": STR, "max_count": {**INT, "minimum": 1}},
            "required": ["pattern"],
        },
        observe("search_history", repo_tools.search_history),
    )
    registry.register(
        "run_ruff",
        "Run the ruff Python linter (static only). Returns issue counts per rule and the first "
        "issues as 'file:line:col: CODE message'.",
        {"properties": {"path": {**STR, "description": "Optional sub-directory"}}},
        observe("run_ruff", repo_tools.run_ruff),
        timeout=180,
    )
    registry.register(
        "git_status",
        "Confirm the cloned repository has not been modified by the audit.",
        {"properties": {}},
        observe("git_status", repo_tools.git_status),
    )

    # -- control tools ---------------------------------------------------------------------
    def record_finding(
        rule_id: str,
        category: str,
        severity: str,
        description: str,
        evidence: str,
        recommendation: str,
        file: str | None = None,
        line: int | None = None,
    ) -> str:
        rule_id = rule_id.strip().upper()
        if not RULE_ID_RE.match(rule_id):
            return (
                "ERROR: rule_id must look like SEC-001 (prefix HYG|DEP|CQ|TST|CI|SEC|CFG|COL|BLD)"
            )
        if not rule_id.startswith(CATEGORIES[category] + "-"):
            return (
                f"ERROR: rule_id prefix does not match category '{category}' "
                f"(expected {CATEGORIES[category]}-NNN)"
            )
        if not is_known_or_custom(rule_id):
            same = [r for r in RULES if r.startswith(CATEGORIES[category] + "-")]
            return (
                f"ERROR: {rule_id} is not in the rule catalogue. Use one of {', '.join(same)}, "
                f"or {CATEGORIES[category]}-901..999 for an issue the catalogue does not cover."
            )
        for label, text in (
            ("description", description),
            ("evidence", evidence),
            ("recommendation", recommendation),
        ):
            if len(text.strip()) < 10:
                return f"ERROR: {label} is too short; be specific"

        rel: str | None = None
        if file is not None and file.strip() not in ("", "null", "None"):
            try:
                rel = normalize_repo_path(file)
            except ToolError as exc:
                return f"ERROR: {exc}"
            if rel not in tracked and rel not in state.history_files:
                return (
                    f"ERROR: '{rel}' is not a file in the repository. Use the exact path from "
                    "list_files, or omit file for findings about something missing."
                )
        if rel is None and line is not None:
            return "ERROR: line given without file"
        if rel is not None and line is not None and rel in tracked:
            text = repo_tools._read_text(rel)
            total = len(text.splitlines()) if text is not None else None
            if total is not None and not 1 <= line <= total:
                return f"ERROR: line {line} is outside '{rel}' (1-{total})"

        dup = state.find_duplicate(rule_id, rel, line)
        if dup is not None:
            return f"Not recorded: duplicate of {dup.id} ({dup.rule_id} at {dup.file}:{dup.line})."

        finding = Finding(
            id=state.next_id(),
            rule_id=rule_id,
            category=category,
            severity=severity,
            file=rel,
            line=line if rel is not None else None,
            description=description.strip(),
            evidence=evidence.strip(),
            recommendation=recommendation.strip(),
        )
        state.findings.append(finding)
        where = f"{rel}:{finding.line}" if rel and finding.line else (rel or "repository-wide")
        warn = ""
        if rel is not None and rel not in state.read_files | state.seen_files:
            warn = " Warning: you have not inspected this file yet; verify before finishing."
        return (
            f"Recorded {finding.id}: {rule_id} [{severity}] at {where}. "
            f"Active findings: {len(state.active_findings())}.{warn}"
        )

    registry.register(
        "record_finding",
        "Record one DevOps violation once you have evidence from a tool result. One call per "
        "distinct problem. Omit file/line for repository-wide issues (e.g. something missing). "
        "Never include secret values in any field.",
        {
            "properties": {
                "rule_id": {**STR, "description": "Catalogue id (e.g. SEC-001) or PREFIX-9NN"},
                "category": {**STR, "enum": list(CATEGORIES)},
                "severity": {**STR, "enum": list(SEVERITIES)},
                "file": {**STR, "description": "Repo-relative path, omit if not applicable"},
                "line": {**INT, "minimum": 1, "description": "1-based line, omit if N/A"},
                "description": {**STR, "description": "What is wrong (one sentence)"},
                "evidence": {**STR, "description": "What you observed, without secret values"},
                "recommendation": {**STR, "description": "Concrete, actionable fix"},
            },
            "required": [
                "rule_id",
                "category",
                "severity",
                "description",
                "evidence",
                "recommendation",
            ],
        },
        record_finding,
        kind="control",
    )

    def retract_finding(finding_id: str, reason: str) -> str:
        for f in state.findings:
            if f.id == finding_id.strip().upper() and not f.retracted:
                f.retracted, f.retract_reason = True, reason.strip()
                return f"Retracted {f.id}. Active findings: {len(state.active_findings())}."
        active = ", ".join(f.id for f in state.active_findings()) or "none"
        return f"ERROR: no active finding '{finding_id}'. Active: {active}"

    registry.register(
        "retract_finding",
        "Withdraw a finding you recorded earlier when later evidence shows it is not a real "
        "violation (e.g. an acceptable alternative exists).",
        {
            "properties": {"finding_id": {**STR, "description": "e.g. F3"}, "reason": STR},
            "required": ["finding_id", "reason"],
        },
        retract_finding,
        kind="control",
    )

    def finish_audit(summary: str, categories_examined: list[str]) -> str:
        state.categories_declared |= set(categories_examined)
        missing = [c for c in CATEGORIES if c not in state.categories_declared]
        if missing and not state.coverage_nudged:
            # D5: one reminder only; the model decides whether to inspect more.
            state.coverage_nudged = True
            return (
                "Not finished yet: these categories have not been examined: "
                f"{', '.join(missing)}. Inspect them with tools (record findings if you find "
                "evidence), then call finish_audit again. If one truly does not apply, call "
                "finish_audit again and say why in the summary."
            )
        state.finished = True
        state.summary = summary.strip()
        return f"Audit finished with {len(state.active_findings())} active finding(s)."

    registry.register(
        "finish_audit",
        "End the audit when every inspection category has been examined and all findings are "
        "recorded. List the categories you examined and summarise what was checked.",
        {
            "properties": {
                "summary": STR,
                "categories_examined": {
                    "type": "array",
                    "items": {**STR, "enum": list(CATEGORIES)},
                },
            },
            "required": ["summary", "categories_examined"],
        },
        finish_audit,
        kind="control",
    )
    return registry
