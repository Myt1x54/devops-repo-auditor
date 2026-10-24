# PLAN.md — Implementation Plan

Status legend: ⬜ pending · 🔄 in progress · 🟡 awaiting verification · ✅ done (verified by user)

Rule: after each step Claude explains what was done + how to verify, then **waits for the user to
verify** before starting the next step. Decisions are in [DECISION.md](DECISION.md).

| Step | Feature | Status |
|------|---------|--------|
| S0 | Planning docs (CLAUDE.md, AGENT.md, INSTRUCTION.md, PLAN.md, DECISION.md) | ✅ |
| S1 | Project scaffolding & CLI skeleton | ✅ |
| S2 | Safe repository acquisition (clone workspace) | ✅ |
| S3 | Mandatory inspection tools | ✅ |
| S4 | Secret redaction & output truncation | ✅ |
| S5 | Extra inspection tools (workflows, ruff, history, git_status) | ✅ |
| S6 | LLM client wrapper | ✅ |
| S7 | Tool registry & dispatcher (JSON schemas) | ✅ |
| S8 | Agent loop (Observe → Reason → Select → Execute → Interpret) | ✅ |
| S9 | Findings, rule catalogue, evidence verification, report builder | ✅ |
| S10 | System prompt & DevOps knowledge | ✅ |
| S11 | Tracing, logging & error handling hardening | ✅ |
| S12 | Automated tests | ✅ |
| S13 | Evaluation fixtures (A/B/C repos) & precision/recall tuning | ✅ |
| S14 | README, sample_report.json, final polish | ✅ |
| S15 | Publish to public GitHub | ✅ |

---

## S0 — Planning docs
- Create `CLAUDE.md` (working rules), `INSTRUCTION.md` (distilled assignment), `AGENT.md`
  (architecture spec), `PLAN.md`, `DECISION.md`.
- **Verify:** read the five files; agree with the decisions D0–D14 or request changes.

## S1 — Project scaffolding & CLI skeleton
- `git init`, `.gitignore` (venv, `.env`, `__pycache__`, reports, traces), `requirements.txt`
  (pinned), `pyproject.toml` (ruff + pytest config), `.env.example`.
- Package layout: `agent.py`, `tools.py`, `auditor/__init__.py`, `tests/`.
- `agent.py`: argparse (`--repo` required, `--output` required, `--max-steps`, `--model`, `--trace`,
  `--verbose`), GitHub URL validation (D2), `.env` loading, exit codes (D12), writes an empty valid
  report as a placeholder.
- **Verify:** `python agent.py --repo https://github.com/psf/requests --output r.json` writes
  `{"repository": ..., "violations": []}`; invalid URL exits non-zero with a message.
- Decisions: D0, D2, D12, D15.

## S2 — Safe repository acquisition
- `auditor/workspace.py`: `RepoWorkspace` context manager → clone into `tempfile.mkdtemp()`,
  `git clone --depth 50 --no-tags --single-branch`, hooks disabled, no credential prompts,
  LFS smudge off, timeout, size guard; always cleaned up (Windows read-only file handling).
- Handle: repo not found / private, network error, timeout → clear fatal error.
- **Verify:** clone a real small repo, temp dir is removed afterwards; bad repo → exit code 1.
- Decisions: D3, D4, D16.

## S3 — Mandatory inspection tools (`tools.py`)
- `list_files`, `read_file`, `search_code`, `git_log` operating on the workspace root only.
- Path traversal guard (resolve + must stay inside root), binary detection, size caps,
  line-numbered output, regex errors returned as messages, result caps.
- **Verify:** small script / REPL calls each tool against a cloned repo; unit tests come in S12.
- Decisions: D4, D7, D17.

## S4 — Secret redaction & output truncation
- `auditor/redact.py`: regexes for common secrets (AWS keys, GitHub tokens, private keys, generic
  `password|secret|token|api_key = "..."`, high-entropy strings, connection strings with passwords).
- Matched value → masked (`AKIA****[REDACTED]`) but the *fact* of a secret stays visible to the LLM.
- Applied to every tool output, the trace, and the final report.
- Truncation helper with "…[truncated N chars]" marker.
- **Verify:** feed sample strings → secrets masked, normal code untouched.
- Decisions: D11, D18.

## S5 — Extra inspection tools
- `inspect_workflows()` (PyYAML parse of `.github/workflows/*`), `file_exists(paths)`,
  `git_file_history(pattern)` (incl. deleted files like a removed `.env`), `search_history(pattern)`,
  `run_ruff()` (static,
  optional, gracefully "unavailable"), `git_status()`.
- **Verify:** run on a repo with GitHub Actions; output shows triggers/permissions/unpinned actions.
- Decisions: D7, D8, D19.

## S6 — LLM client wrapper
- `auditor/llm.py`: provider-neutral `LLMClient` interface (`complete(system, history, tools)`),
  neutral history/tool types, normalised stop reasons, `create_llm()` factory, `FakeLLM`.
- `auditor/llm_gemini.py` (default, free tier) and `auditor/llm_anthropic.py` (optional, paid):
  retries/backoff for rate limits and server errors, clear fatal errors, token usage counted.
- **Verify:** `scripts/llm_smoke.py` does one real tool-call round trip with the configured provider.
- Decisions: D1 (superseded), D20, D21, D22.

## S7 — Tool registry & dispatcher
- `ToolRegistry`: name → (callable, JSON schema, description). Validates arguments, catches all
  exceptions, applies timeout + redaction + truncation, returns string results.
- Control tools `record_finding`, `retract_finding`, `finish_audit`.
- Implemented in `auditor/registry.py` (dispatcher) and `auditor/toolset.py` (schemas, control
  tools, observation tracking); `auditor/findings.py` holds the finding model and audit state.
- **Verify:** dispatch valid/invalid calls → structured results, never exceptions.
- Decisions: D5, D7, D23.

## S8 — Agent loop
- `auditor/loop.py` `AuditAgent.run()`: seeds observation (repo tree + key file presence), then loops:
  LLM response → execute every `tool_use` block → append `tool_result`s → repeat.
- Stops on `finish_audit`, `end_turn` with no tools, step budget, or wall-clock budget.
- Coverage nudge: harness tracks which categories have been examined; if the LLM tries to finish
  early it gets one reminder listing unvisited categories (LLM still decides what to do).
- History is append-only (D20): earlier turns are never edited or trimmed; tool outputs are
  already capped (D7) and the growing history is prompt-cached.
- **Verify:** run on a real repo with `--verbose`; observe the LLM choosing different tools based on
  what it finds (trace shows follow-up calls).
- Implemented in `auditor/loop.py` (`AuditAgent`), `auditor/prompts.py` (initial prompt), wired in
  `agent.py`. Live run on pallets/itsdangerous: 13 steps, 115 s, 1 genuine finding (coverage config
  pointing at the wrong package).
- Decisions: D5, D9, D20, D24, D25.

## S9 — Findings, rule catalogue, evidence verification, report
- `auditor/findings.py`: `Finding` dataclass; rule catalogue (D6) with default severities;
  normalisation (category names, severity enum, repo-relative paths, `null` file/line).
- **Evidence verification**: file must exist in the clone; line must be within range; if the LLM
  claims a line, harness checks the cited line relates to the evidence (else line → nearest match or
  `null`); findings referencing never-inspected files are dropped (anti-hallucination).
- Dedup by `(rule_id, file, line)` and near-duplicate merge.
- `auditor/report.py`: final JSON, schema validation, final redaction, atomic write.
- **Verify:** unit-level check: hallucinated file finding is dropped; duplicate merged; output valid.
- Implemented in `auditor/rules.py`, `auditor/verify.py`, `auditor/report.py`.
- Decisions: D6, D9, D10, D26.

## S10 — System prompt & DevOps knowledge
- `auditor/prompts.py`: role, Observe→Report method, 9 categories with what to look for,
  acceptable-alternatives list, evidence rule, severity rubric, secret-handling rule, scope limits,
  output-quality rules (concrete recommendations, one finding per issue).
- **Verify:** run on 2–3 real public repos, review the report quality together.
- Implemented in `auditor/prompts.py`; full file listing seeded in `agent.py`; guard tests in
  `tests/test_prompts.py`.
- Decisions: D10, D14, D27.

## S11 — Tracing, logging & hardening
- `--trace trace.jsonl`: every LLM turn (redacted), tool name, args, duration, result size.
- Capture the model's reasoning for the trace: Gemini thought summaries (`include_thoughts`),
  because Flash-Lite often calls tools without visible text (seen in the S8 live run).
- Logging to stderr only; stdout clean. Graceful handling: invalid input, missing API key, API
  errors, timeouts, Ctrl-C (cleanup still runs). Post-run `git_status` confirms target unchanged.
- **Verify:** simulate missing key / bad URL / network off → useful messages, correct exit codes.
- Implemented in `auditor/trace.py`, `agent.py` (`run`/`_audit`), reasoning capture in both LLM
  clients.
- Decisions: D12, D13, D28.

## S12 — Automated tests (`tests/`)
- Tools: each tool against a tmp git repo fixture (traversal blocked, binary skipped, regex error).
- Redaction tests. Findings/dedup/verification tests. Report schema tests.
- Agent tests with `FakeLLM` (scripted tool calls → expected report), budget exhaustion, tool errors.
- CLI tests: bad URL → non-zero, output JSON valid.
- No network or API key needed for the test suite.
- **Verify:** `pytest -q` all green; `ruff check .` clean.
- Done: network guard, coverage 97 %, edge-case and end-to-end tests, GitHub Actions CI.
- Decisions: D29.

## S13 — Evaluation fixtures & tuning
- Build 3 local/public test repos mirroring the hidden evaluation: (A) obvious defects,
  (B) subtle CI/security/config defects, (C) mostly healthy — each with an answer key.
- `scripts/evaluate.py` computes precision/recall vs answer key.
- Iterate on prompts/verification to raise precision without killing recall. Record changes as
  decisions. **No repo-specific findings hardcoded.**
- **Verify:** review the precision/recall table together.
- Done: `evaluation/` (fixtures + scoring), `scripts/evaluate.py`; 5 live rounds, P/R 1.00/0.86 →
  1.00/1.00. Decisions: D30.

## S14 — README, sample report, polish
- `README.md`: overview, architecture diagram, install, `.env` setup, run command, options, tools,
  safety guarantees, testing, limitations.
- `sample_report.json`: authentic output from a real run (and optional `sample_trace.jsonl`).
- Final check against the submission checklist in INSTRUCTION.md §8.
- **Verify:** fresh clone → install → run works by following README exactly.
- Done: README.md, sample_report.json + trace (real run), transport-error retry fix, checklist
  ticked. Decisions: D31.

## S15 — Publish
- Create public GitHub repo, push (only after user approval), double-check no `.env`/secrets
  committed. User submits the URL.
- Done: public repo https://github.com/Myt1x54/devops-repo-auditor, history scanned clean, required
  files present, CI green. Decisions: D32.
