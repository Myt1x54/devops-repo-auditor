"""Inspection tools used by the auditing agent (AGENT.md section 3).

Every tool reads the cloned repository only. Tools never raise into the agent loop: on bad input
they return a string starting with ``ERROR:`` so the LLM can adapt its next call (D7).
Secret redaction and the global output cap are applied on top of these results (S4).
"""

from __future__ import annotations

import fnmatch
import functools
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from auditor.errors import AuditError
from auditor.workflows import OTHER_CI_FILES, analyze_workflow

# Limits (D7)
MAX_LIST_ENTRIES = 400  # list_files: files shown before collapsing into directory summaries
MAX_READ_BYTES = 1_000_000  # read_file: larger files are not returned
MAX_READ_LINES = 400  # read_file: lines per call
MAX_LINE_CHARS = 500  # read_file / search_code: long lines are cut
MAX_SEARCH_RESULTS = 100
SEARCH_TIME_BUDGET = 10.0  # seconds
MAX_GIT_LOG = 50
BINARY_SNIFF_BYTES = 8192
RUFF_TIMEOUT = 120  # seconds


class ToolError(Exception):
    """Invalid tool input; reported back to the LLM as an ``ERROR:`` string."""


def tool_safe(func: Callable[..., str]) -> Callable[..., str]:
    """Turn expected failures into ``ERROR: ...`` strings instead of exceptions."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> str:
        try:
            return func(*args, **kwargs)
        except (ToolError, AuditError) as exc:
            return f"ERROR: {exc}"
        except re.error as exc:
            return f"ERROR: invalid regular expression: {exc}"
        except OSError as exc:
            return f"ERROR: could not read from repository: {exc.strerror or exc}"

    return wrapper


def normalize_repo_path(path: str | None) -> str:
    """Validate a repo-relative path from the LLM and return it in POSIX form ('' = root)."""
    if path is None:
        return ""
    raw = str(path).strip().replace("\\", "/")
    if raw in ("", ".", "./", "/"):
        return ""
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith("~"):
        raise ToolError(f"path must be relative to the repository root: {path!r}")
    parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ToolError(f"path may not contain '..': {path!r}")
    if parts and parts[0] == ".git":
        raise ToolError("the .git directory is not part of the repository contents")
    return "/".join(parts)


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def _cut(line: str, limit: int = MAX_LINE_CHARS) -> str:
    return line if len(line) <= limit else line[:limit] + " ...[line truncated]"


class RepoTools:
    """Inspection tools bound to one cloned repository.

    ``run_git`` executes a read-only git command inside the clone and returns stdout
    (``RepoWorkspace.run_git`` in production).
    """

    def __init__(self, root: Path, run_git: Callable[..., str]) -> None:
        self.root = Path(root).resolve()
        self._run_git = run_git
        self._tracked: list[str] | None = None

    # -- helpers ---------------------------------------------------------------------------
    def resolve(self, path: str | None) -> Path:
        """Map a repo-relative path to an absolute path guaranteed to stay inside the clone."""
        rel = normalize_repo_path(path)
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError(f"path escapes the repository: {path!r}")
        return target

    def tracked_files(self) -> list[str]:
        """All files tracked by git (POSIX paths), cached for the run."""
        if self._tracked is None:
            out = self._run_git("ls-files", "-z")
            self._tracked = sorted(p for p in out.split("\0") if p)
        return self._tracked

    def _read_text(self, rel: str) -> str | None:
        """Text of a tracked file, or None if it is missing, binary or too large."""
        target = self.resolve(rel)
        if not target.is_file() or target.stat().st_size > MAX_READ_BYTES:
            return None
        data = target.read_bytes()
        if is_binary(data):
            return None
        return data.decode("utf-8", errors="replace")

    # -- mandatory tools -------------------------------------------------------------------
    @tool_safe
    def list_files(self, path: str = ".", max_depth: int | None = None) -> str:
        """List tracked files under ``path`` with sizes; large trees are summarised by directory."""
        prefix = normalize_repo_path(path)
        files = self.tracked_files()
        if prefix:
            files = [f for f in files if f == prefix or f.startswith(prefix + "/")]
            if not files:
                target = self.resolve(prefix)
                if target.exists():
                    return f"No tracked files under '{prefix}' (directory exists but is untracked)."
                return f"ERROR: path not found in repository: '{prefix}'"
        if max_depth is not None and max_depth < 1:
            raise ToolError("max_depth must be >= 1")

        base_depth = len(PurePosixPath(prefix).parts) if prefix else 0
        shown: list[str] = []
        collapsed: Counter[str] = Counter()
        for f in files:
            parts = PurePosixPath(f).parts
            rel_depth = len(parts) - base_depth
            if max_depth is not None and rel_depth > max_depth:
                collapsed["/".join(parts[: base_depth + max_depth]) + "/"] += 1
            else:
                shown.append(f)

        if len(shown) > MAX_LIST_ENTRIES:
            # Too many: show shallow files and summarise directories at the next level.
            depth = base_depth + 1
            shallow = [f for f in shown if len(PurePosixPath(f).parts) <= depth]
            for f in shown:
                parts = PurePosixPath(f).parts
                if len(parts) > depth:
                    collapsed["/".join(parts[:depth]) + "/"] += 1
            shown = shallow[:MAX_LIST_ENTRIES]

        lines = [f"{len(files)} tracked file(s) under '{prefix or '.'}':"]
        for f in shown:
            try:
                size = self.resolve(f).stat().st_size
                lines.append(f"{f}  ({size} B)")
            except (OSError, ToolError):
                lines.append(f"{f}  (unreadable)")
        if collapsed:
            lines.append("")
            lines.append("Directories not expanded (call list_files with path=<dir> to expand):")
            for d, n in sorted(collapsed.items()):
                lines.append(f"{d}  ({n} files)")
        return "\n".join(lines)

    @tool_safe
    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Return line-numbered content of a file (at most MAX_READ_LINES lines per call)."""
        rel = normalize_repo_path(path)
        if not rel:
            raise ToolError("a file path is required")
        target = self.resolve(rel)
        if not target.exists():
            return f"ERROR: file not found: '{rel}'"
        if target.is_dir():
            return f"ERROR: '{rel}' is a directory; use list_files(path='{rel}')"
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return (
                f"'{rel}' is {size} bytes (> {MAX_READ_BYTES}); not returned. "
                "Use search_code with path_glob to inspect it."
            )
        data = target.read_bytes()
        if is_binary(data):
            return f"'{rel}' is a binary file ({size} bytes); content not shown."

        lines = data.decode("utf-8", errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            return f"'{rel}' is empty (0 lines)."
        start = max(1, int(start_line or 1))
        if start > total:
            return f"ERROR: start_line {start} is beyond the end of '{rel}' ({total} lines)"
        end = total if end_line is None else min(int(end_line), total)
        if end < start:
            raise ToolError("end_line must be >= start_line")
        end = min(end, start + MAX_READ_LINES - 1)

        width = len(str(end))
        body = "\n".join(f"{n:>{width}}| {_cut(lines[n - 1])}" for n in range(start, end + 1))
        header = f"{rel} (lines {start}-{end} of {total})"
        footer = ""
        if end < total:
            footer = f"\n...[{total - end} more lines; call read_file with start_line={end + 1}]"
        return f"{header}\n{body}{footer}"

    @tool_safe
    def search_code(
        self,
        pattern: str,
        path_glob: str | None = None,
        case_insensitive: bool = False,
        max_results: int = MAX_SEARCH_RESULTS,
    ) -> str:
        """Regex search over tracked text files. Returns ``file:line: text`` hits."""
        if not pattern:
            raise ToolError("pattern must not be empty")
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))

        files = self.tracked_files()
        if path_glob:
            glob = path_glob.replace("\\", "/")
            files = [
                f
                for f in files
                if fnmatch.fnmatch(f, glob) or fnmatch.fnmatch(PurePosixPath(f).name, glob)
            ]

        hits: list[str] = []
        total_hits = 0
        searched = 0
        skipped = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        timed_out = False
        for f in files:
            if time.monotonic() > deadline:
                timed_out = True
                break
            text = self._read_text(f)
            if text is None:
                skipped += 1
                continue
            searched += 1
            for n, line in enumerate(text.splitlines(), start=1):
                if regex.search(line[:2000]):
                    total_hits += 1
                    if len(hits) < limit:
                        hits.append(f"{f}:{n}: {_cut(line.strip(), 200)}")

        summary = f"{total_hits} match(es) in {searched} searched file(s)"
        if path_glob:
            summary += f" matching '{path_glob}'"
        if skipped:
            summary += f"; {skipped} binary/large file(s) skipped"
        if total_hits > len(hits):
            summary += f"; showing first {len(hits)}"
        if timed_out:
            summary += "; search stopped early (time budget) - narrow it with path_glob"
        return summary + (":\n" + "\n".join(hits) if hits else ".")

    @tool_safe
    def git_log(self, max_count: int = 20, path: str | None = None) -> str:
        """Recent commits (newest first) with date, author name, subject and changed files."""
        count = max(1, min(int(max_count), MAX_GIT_LOG))
        args = [
            "log",
            f"--max-count={count}",
            "--date=short",
            "--format=%x1e%h %ad %an%x1f%s",
            "--name-status",
            "--no-renames",
        ]
        rel = normalize_repo_path(path) if path else ""
        if rel:
            args += ["--", rel]
        out = self._run_git(*args)

        commits = []
        for chunk in out.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            head, _, rest = chunk.partition("\n")
            meta, _, subject = head.partition("\x1f")
            changes = [ln.replace("\t", " ") for ln in rest.splitlines() if ln.strip()]
            shown = changes[:15]
            more = f"\n    ...(+{len(changes) - 15} more files)" if len(changes) > 15 else ""
            files = "".join(f"\n    {c}" for c in shown) + more
            commits.append(f"{meta} | {_cut(subject, 200)}{files}")

        scope = f" touching '{rel}'" if rel else ""
        header = f"{len(commits)} most recent commit(s){scope}"
        if self._is_shallow():
            header += " (shallow clone: older history is not available)"
        if not commits:
            return header + "."
        return (
            header
            + " - format: <hash> <date> <author> | <subject>, then A/M/D files:\n"
            + ("\n".join(commits))
        )

    # -- extra tools (S5, D8, D19) ---------------------------------------------------------
    @tool_safe
    def inspect_workflows(self, path: str | None = None) -> str:
        """Structured summary of GitHub Actions workflows (all, or one file via ``path``)."""
        workflow_files = [
            f
            for f in self.tracked_files()
            if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml"))
        ]
        if path:
            rel = normalize_repo_path(path)
            if rel not in workflow_files:
                return f"ERROR: '{rel}' is not a tracked workflow file. Workflows: {workflow_files}"
            workflow_files = [rel]

        tracked = set(self.tracked_files())
        other_ci = [f for f in OTHER_CI_FILES if f in tracked]
        if not workflow_files:
            msg = "No GitHub Actions workflows (.github/workflows/*.yml) in the repository."
            if other_ci:
                msg += f" Other CI configuration found: {', '.join(other_ci)}."
            else:
                msg += " No other known CI configuration files found either."
            return msg

        parts = [f"{len(workflow_files)} workflow file(s)."]
        if other_ci:
            parts.append(f"Other CI configuration also present: {', '.join(other_ci)}.")
        for rel in workflow_files:
            text = self._read_text(rel)
            if text is None:
                parts.append(f"=== {rel}: unreadable (binary or too large)")
                continue
            parts.append(analyze_workflow(rel, text))
        parts.append(
            "Line numbers refer to the workflow file; confirm with read_file before reporting."
        )
        return "\n".join(parts)

    @tool_safe
    def file_exists(self, paths: list[str]) -> str:
        """Check several paths or glob patterns at once (tracked files only)."""
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise ToolError("paths must be a non-empty list")
        tracked = self.tracked_files()
        tracked_set = set(tracked)
        lines = []
        for raw in paths[:50]:
            pattern = str(raw).strip().replace("\\", "/")
            if pattern.startswith("./"):
                pattern = pattern[2:]
            if any(ch in pattern for ch in "*?["):
                matches = [f for f in tracked if _glob_match(f, pattern)]
                if matches:
                    shown = ", ".join(matches[:8]) + (" ..." if len(matches) > 8 else "")
                    lines.append(f"{raw}: {len(matches)} match(es): {shown}")
                else:
                    lines.append(f"{raw}: no match")
                continue
            rel = normalize_repo_path(pattern)
            if rel in tracked_set:
                lines.append(f"{raw}: EXISTS (file)")
            elif rel and any(f.startswith(rel + "/") for f in tracked):
                count = sum(1 for f in tracked if f.startswith(rel + "/"))
                lines.append(f"{raw}: EXISTS (directory, {count} tracked files)")
            else:
                lines.append(f"{raw}: missing")
        return "\n".join(lines)

    @tool_safe
    def git_file_history(self, pattern: str) -> str:
        """Files matching ``pattern`` that were ever added in the available history.

        Shows when each was added/modified/deleted, so files committed and later removed
        (e.g. ``.env``, ``*.pem``) are visible even though they are no longer tracked.
        """
        if not pattern or not str(pattern).strip():
            raise ToolError("pattern must not be empty")
        glob = str(pattern).strip().replace("\\", "/")
        out = self._run_git(
            "log",
            "--reverse",
            "--date=short",
            "--format=%x1e%h %ad",
            "--name-status",
            "--no-renames",
            timeout=60,
        )
        events: dict[str, list[str]] = {}
        for chunk in out.split("\x1e"):
            head, _, rest = chunk.strip("\n").partition("\n")
            for line in rest.splitlines():
                status, _, name = line.partition("\t")
                if name and _glob_match(name, glob):
                    events.setdefault(name, []).append(f"{status.strip()[:1]} {head}")
        if not events:
            return f"No file matching '{glob}' appears in the available history."
        tracked = set(self.tracked_files())
        lines = [
            f"{len(events)} file(s) matching '{glob}' in history "
            "(A=added, M=modified, D=deleted; oldest first):"
        ]
        for name in sorted(events)[:50]:
            state = "still tracked" if name in tracked else "NOT tracked now (deleted)"
            ev = events[name]
            shown = ev if len(ev) <= 6 else [ev[0], f"...{len(ev) - 5} more...", *ev[-4:]]
            lines.append(f"{name} [{state}, {len(ev)} change(s)]: {'; '.join(shown)}")
        if self._is_shallow():
            lines.append("(shallow clone: older history is not available)")
        return "\n".join(lines)

    @tool_safe
    def search_history(self, pattern: str, max_count: int = 20) -> str:
        """Commits whose diffs added or removed lines matching ``pattern`` (git log -G).

        Finds secrets or settings removed from the current files but still in history.
        Only commit ids and file names are returned, never the matching content.
        """
        if not pattern or len(pattern) > 200:
            raise ToolError("pattern must be 1-200 characters")
        re.compile(pattern)  # reject obviously broken patterns early
        count = max(1, min(int(max_count), MAX_GIT_LOG))
        out = self._run_git(
            "log",
            f"--max-count={count}",
            "--extended-regexp",
            f"-G{pattern}",
            "--date=short",
            "--format=%x1e%h %ad %an%x1f%s",
            "--name-only",
            timeout=60,
        )
        commits = []
        for chunk in out.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            head, _, rest = chunk.partition("\n")
            meta, _, subject = head.partition("\x1f")
            files = [f for f in rest.splitlines() if f.strip()][:10]
            commits.append(f"{meta} | {_cut(subject, 120)}\n    files: {', '.join(files)}")
        shallow = " (shallow clone: older history not searched)" if self._is_shallow() else ""
        if not commits:
            return (
                f"No commit in the available history changed lines matching '{pattern}'.{shallow}"
            )
        return (
            f"{len(commits)} commit(s) added/removed lines matching '{pattern}'{shallow}:\n"
            + "\n".join(commits)
        )

    @tool_safe
    def run_ruff(self, path: str = ".") -> str:
        """Run the ruff linter (static analysis only; never executes repository code)."""
        rel = normalize_repo_path(path)
        if not any(f.endswith((".py", ".pyi")) for f in self.tracked_files()):
            return "No Python files in the repository; ruff not applicable."
        target = self.resolve(rel) if rel else self.root
        cmd = [
            sys.executable, "-m", "ruff", "check", "--no-cache", "--exit-zero",
            "--output-format=concise", "--no-fix", str(target),
        ]  # fmt: skip
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUFF_TIMEOUT,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: ruff timed out after {RUFF_TIMEOUT}s"
        except OSError as exc:
            return f"ruff unavailable: {exc}"
        if proc.returncode != 0 and "No module named ruff" in proc.stderr:
            return "ruff unavailable in this environment; check skipped."
        if proc.returncode != 0:
            return f"ERROR: ruff failed: {_cut(proc.stderr.strip(), 300)}"

        root_prefix = str(self.root) + ("\\" if sys.platform == "win32" else "/")
        issues = [
            line.replace(root_prefix, "").replace("\\", "/")
            for line in proc.stdout.splitlines()
            if re.match(r"^.+:\d+:\d+: [A-Z]+\d+", line)
        ]
        codes = Counter(m.group(1) for i in issues if (m := re.search(r": ([A-Z]+\d+)", i)))
        tracked = set(self.tracked_files())
        own = [f for f in ("ruff.toml", ".ruff.toml", "pyproject.toml") if f in tracked]
        config = "project files " + ", ".join(own) if own else "ruff defaults"
        header = f"ruff check: {len(issues)} issue(s) (config: {config})"
        if not issues:
            return header + "."
        summary = ", ".join(f"{c}x{n}" for c, n in codes.most_common(15))
        more = f"\n... {len(issues) - 40} more" if len(issues) > 40 else ""
        return f"{header}\nby rule: {summary}\n" + "\n".join(issues[:40]) + more

    @tool_safe
    def git_status(self) -> str:
        """Working-tree status of the clone: proves the audit left the repository unchanged."""
        out = self._run_git("status", "--porcelain", "--untracked-files=all").strip()
        if not out:
            return "Working tree clean: the repository has not been modified."
        return "Working tree has changes:\n" + _cut(out, 2000)

    def _is_shallow(self) -> bool:
        return self._run_git("rev-parse", "--is-shallow-repository").strip() == "true"


def _glob_match(path: str, pattern: str) -> bool:
    """fnmatch on the full repo path or on the file name (``*`` also matches ``/``)."""
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(PurePosixPath(path).name, pattern)
