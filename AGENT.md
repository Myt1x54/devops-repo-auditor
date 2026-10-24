# AGENT.md — Auditing Agent Design Spec

How the DevOps Repository Auditing Agent works. Decisions referenced as `Dn` are in
[DECISION.md](DECISION.md).

## 1. High-level flow
```
agent.py (CLI)
  │  parse args, validate URL, load .env                          (S1)
  ▼
RepoWorkspace: safe shallow clone into temp dir (read-only use)  (S2, D3, D4)
  │
  ▼
AuditAgent.run()  ── hand-written loop, no frameworks ─────────── (S6, D0, D5)
  │   ┌──────────────────────────────────────────────────────┐
  │   │ OBSERVE   : seed context = repo tree summary          │
  │   │ REASON    : LLM thinks about what to check next       │
  │   │ SELECT    : LLM emits tool_use call(s)                │
  │   │ EXECUTE   : dispatcher runs tool (sandboxed, timeout) │
  │   │ INTERPRET : tool result (truncated+redacted) → LLM    │
  │   │ RECORD    : LLM calls record_finding(...) w/ evidence │
  │   └──────── repeat until finish_audit or budget hit ──────┘
  ▼
Verification pass: harness checks every finding's evidence        (S9, D9)
  ▼
Report builder: dedupe, normalise, schema-validate, redact → JSON (S9, D10, D11)
  ▼
Cleanup temp clone; exit 0 (or non-zero on fatal error)           (D12)
```

## 2. Modules
| Module | Responsibility |
|--------|---------------|
| `agent.py` | CLI entry: `--repo`, `--output`, optional `--max-steps`, `--model`, `--trace`, `--verbose` |
| `tools.py` | Inspection tools + `ToolRegistry` (JSON schemas + dispatch) |
| `auditor/workspace.py` | Clone / validate URL / temp-dir lifecycle |
| `auditor/llm.py` | Provider-neutral LLM interface, history types, `FakeLLM` (D22) |
| `auditor/llm_gemini.py` / `auditor/llm_anthropic.py` | Gemini (default, free) / Anthropic (optional) clients |
| `auditor/registry.py` | `ToolRegistry`: argument validation, timeout, error capture, redaction/truncation (D23) |
| `auditor/toolset.py` | Tool schemas/descriptions, control tools, observation tracking (D23) |
| `auditor/loop.py` | `AuditAgent` — the Observe→Report loop, budgets, error handling |
| `auditor/prompts.py` | System prompt, category guidance, acceptable-alternatives rules |
| `auditor/findings.py` | `Finding` model, categories/severities, `AuditState` |
| `auditor/rules.py` | Generic rule catalogue (49 rules) rendered into the prompt (D26) |
| `auditor/verify.py` | Post-loop evidence verification, line correction, merging (D26) |
| `auditor/report.py` | Report ordering, contract validation, atomic write (D26) |
| `auditor/redact.py` | Secret detection & masking used everywhere output leaves a tool |
| `auditor/trace.py` | Redacted JSONL trace: LLM turns with reasoning summaries, tool calls, verification, errors (D13, D28) |

## 3. Tools exposed to the LLM
Mandatory (assignment §3):
| Tool | Behaviour |
|------|-----------|
| `list_files(path=".", max_depth=…)` | Tree of tracked files (via `git ls-files`), with sizes; skips `.git` |
| `read_file(path, start_line=1, end_line=…)` | Line-numbered content, size-capped, binary-safe, path-traversal guarded |
| `search_code(pattern, path_glob=None, case_insensitive=False, max_results=100)` | Regex search across tracked files → `file:line: text` (capped hits) |
| `git_log(max_count=20, path=None)` | Recent commits: hash, date, author name, subject, A/M/D files changed |

Optional / extra (D7, D8, D19):
| Tool | Behaviour |
|------|-----------|
| `inspect_workflows(path=None)` | Parses `.github/workflows/*.yml` → triggers, permissions, jobs, steps (test/lint/build/deploy), action pinning, secrets referenced, plus neutral "signals to verify" (`pull_request_target` + PR-head checkout, write permissions, `\|\| true`, `--exit-zero`, `continue-on-error`, untrusted `${{ github.event... }}` in `run`, branch-pinned actions); invalid YAML reported; other CI systems detected |
| `file_exists(paths)` | Batch existence check for paths/globs, to look for acceptable alternatives (e.g. `uv.lock` vs `requirements.txt`) |
| `git_file_history(pattern)` | Files matching a glob ever added in history, with A/M/D events and whether still tracked (e.g. a `.env` committed then deleted) |
| `search_history(pattern)` | Commits whose diffs added/removed matching lines (`git log -G`) — commit ids + file names only, never content |
| `run_ruff(path=".")` | `ruff check --no-cache --exit-zero` (static only, never executes target code); issue counts by rule + first 40 issues |
| `git_status()` | Confirms the clone's working tree is untouched (safety proof at the end of a run) |

Control tools (agent bookkeeping, not inspection):
| Tool | Behaviour |
|------|-----------|
| `record_finding(rule_id, category, severity, file, line, description, evidence, recommendation)` | Stores a candidate violation |
| `retract_finding(id, reason)` | Lets the agent withdraw a finding after counter-evidence |
| `finish_audit(summary)` | Ends the loop |

## 4. Agent behaviour rules (encoded in the system prompt)
1. Start by observing structure (`list_files`) and form hypotheses per inspection category.
2. **Investigate before claiming**: every finding must be backed by a tool result seen in this run
   (file content, search hit, workflow parse, git log).
3. **Acceptable alternatives**: before flagging something missing, check for equivalents
   (e.g. `pyproject.toml`+`poetry.lock` instead of `requirements.txt`; `ruff` config in
   `pyproject.toml` instead of `.flake8`; tests under `src/**/test_*.py`). Absence of an optional
   file is not a violation.
4. **Do not infer** branch-protection or remote settings from files.
5. Never quote secret values; describe them (`"AWS secret key assigned to variable in settings.py"`).
6. Use the rule catalogue IDs (D6); one finding per distinct problem location; no duplicates.
7. Severity per rubric (D10). Recommendations must be concrete and actionable.
8. Stay in scope: DevOps/SE only, no ML-specific checks.
9. Cover all 9 inspection categories before finishing (the agent decides *how*; the harness only
   reminds it which categories remain unvisited — D5).

## 5. Budgets & reliability
- Max agent steps (default 40), max tool output chars (default 8 000), per-tool timeout, total
  wall-clock limit, LLM retry with exponential backoff on rate-limit / 5xx.
- If the budget runs out, the agent is asked once to finalise; recorded findings are still reported.
- Any tool error is returned to the LLM as text so it can adapt (never crashes the loop).

## 6. Output
Exactly the contract in [INSTRUCTION.md §5](INSTRUCTION.md). `rule_id`, `category` (snake-case of the
9 categories), `severity`, `file` (repo-relative or `null`), `line` (int or `null`), `description`,
`evidence`, `recommendation`. A tool-call trace is optionally written next to the report.

## 7. Evaluation
`scripts/evaluate.py` builds three fixture repositories (`evaluation/fixtures.py`: A obvious,
B subtle CI/security/config, C healthy), audits them through the same pipeline and scores the
reports against answer keys (`evaluation/scoring.py`): precision = TP / reported, recall =
TP / known, duplicates and unsupported claims count as false positives, debatable findings are
neutral. The agent never imports `evaluation/`. Results per round are in DECISION.md (D30).
