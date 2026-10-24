# DevOps Repository Auditing Agent

An AI agent that inspects a public GitHub repository, finds DevOps and software-engineering
best-practice violations, and writes an evidence-based JSON report with actionable
recommendations.

The LLM drives the investigation: it decides which files to read, what to search for and which
leads to follow, through a hand-written Python agent loop (no LangChain/LangGraph/n8n or other
agent frameworks). The harness keeps it honest: it never runs the target's code, never modifies
it, hides secret values, and checks every finding against what the tools actually showed before
it reaches the report.

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

- **Default LLM:** Google Gemini on the **free tier** (`gemini-3.5-flash-lite`), no payment needed.
- **Optional:** Anthropic Claude (`claude-opus-5-5`) with a paid key.
- **Output:** `report.json` (the required contract) plus `report.trace.jsonl` (every reasoning step
  and tool call, secrets redacted).

---

## Contents
1. [Quick start](#1-quick-start)
2. [Configuration](#2-configuration)
3. [Usage](#3-usage)
4. [Output](#4-output)
5. [How it works](#5-how-it-works)
6. [Inspection tools](#6-inspection-tools)
7. [What it checks](#7-what-it-checks)
8. [Safety guarantees](#8-safety-guarantees)
9. [Testing and evaluation](#9-testing-and-evaluation)
10. [Project layout](#10-project-layout)
11. [Troubleshooting](#11-troubleshooting)
12. [Limitations](#12-limitations)

---

## 1. Quick start

**Requirements:** Python **3.10+** and **git** on `PATH`. Internet access to GitHub and the LLM API.

```bash
git clone https://github.com/Myt1x54/devops-repo-auditor.git
cd devops-repo-auditor
python -m venv .venv
# Windows (Git Bash): source .venv/Scripts/activate   |   PowerShell: .venv\Scripts\Activate.ps1
# macOS / Linux:      source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then put your API key in .env (see below)
python agent.py --repo https://github.com/pallets/itsdangerous --output report.json
```

A typical audit takes 20–120 seconds and 10–25 LLM requests.

## 2. Configuration

All settings come from environment variables, normally set in a `.env` file in the project folder
(`.env` is git-ignored; `.env.example` lists every variable).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AUDITOR_PROVIDER` | no | `gemini` | `gemini` (free tier) or `anthropic` (paid) |
| `GEMINI_API_KEY` | **yes, for gemini** | – | Free key: <https://aistudio.google.com> → *Get API key* → *Create API key* (no card needed). `GOOGLE_API_KEY` also works. |
| `ANTHROPIC_API_KEY` | yes, for anthropic | – | <https://console.anthropic.com> → *Settings* → *API Keys* |
| `AUDITOR_MODEL` | no | `gemini-3.5-flash-lite` / `claude-opus-5-5` | Override the model |
| `AUDITOR_EFFORT` | no | `medium` (Gemini) / `high` (Anthropic) | Reasoning effort: `low`, `medium`, `high` (Anthropic also `xhigh`, `max`) |
| `AUDITOR_MAX_STEPS` | no | `40` | Maximum agent steps (LLM turns) |

Notes:
- **Precedence:** a **non-empty** value in `.env` overrides the same variable exported in your
  shell; empty values in `.env` are ignored (so an untouched copy of `.env.example` never hides a
  key you exported). To switch provider for one run, edit `.env` or use a clean shell without
  `.env`.
- **Gemini free tier:** quotas are per model and per day. If the default model's daily quota is
  used up (or it is overloaded), the agent automatically switches to `gemini-3.1-flash-lite` for
  the rest of the run. `gemini-3.6-flash` works too but its free quota (20 requests/day) is below
  one audit.
- Free-tier prompts may be used by Google to improve its products. Only public repositories are
  audited and secret values are redacted before anything is sent.
- No GitHub token is needed: public repositories are cloned anonymously.

## 3. Usage

```text
python agent.py --repo <github_repository_url> --output <report.json>
                [--max-steps N] [--model MODEL_ID] [--trace PATH] [--verbose]
```

| Option | Description |
|---|---|
| `--repo` | Public GitHub HTTPS URL (`https://github.com/owner/repo`, optional `.git`, trailing `/` or `/tree/<branch>`) |
| `--output` | Path of the JSON report (parent directories are created) |
| `--max-steps` | Step budget (default 40 or `AUDITOR_MAX_STEPS`) |
| `--model` | Model id for this run (overrides `AUDITOR_MODEL`) |
| `--trace` | Trace file path (default: next to the report, `report.trace.jsonl`) |
| `--verbose` | Debug logging, including every HTTP request |

Progress is logged to **stderr** (the agent's stated reasoning and each tool call); stdout stays
empty. The run is fully unattended.

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | Audit completed; report written (it may contain zero violations) |
| `1` | Fatal runtime error: repository not found or not public, network/clone failure, missing or rejected API key, LLM failure after retries, report could not be written, interrupted. No report is written. |
| `2` | Invalid input: bad repository URL, missing arguments, `--output` is a directory |

## 4. Output

### `report.json` (output contract)

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "config.py line 17 assigns a literal key to `API_KEY = '`",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

- Every violation has all eight fields. `severity` ∈ `critical | high | medium | low`.
- `file` / `line` are `null` when not applicable (e.g. "no tests at all").
- `category` ∈ `repository_hygiene, dependency_management, code_quality, testing, ci_cd, security,
  configuration, collaboration, build_release`.
- `rule_id` comes from a catalogue of 49 generic rules (`auditor/rules.py`), e.g. `SEC-006`
  *pull_request_target runs untrusted PR code*; `<PREFIX>-9NN` for issues outside the catalogue.
- Violations are ordered by severity, then category, file and line. Secret values never appear.

See [`sample_report.json`](sample_report.json) for an authentic report produced by the agent, and
[`sample_report.trace.jsonl`](sample_report.trace.jsonl) for the trace of the same run.

### `report.trace.jsonl` (audit trail)

One JSON object per line, all strings redacted:

| Event | Content |
|---|---|
| `start` / `loop_start` | repository, provider, model, commit, tools offered, budgets |
| `llm` | each LLM turn: visible reasoning sentence, **reasoning summary**, tool calls, tokens |
| `tool` | tool name, arguments, error flag, duration, output size, output preview |
| `nudge` | reminders the harness sent (e.g. budget warnings) |
| `verify` | findings dropped, lines corrected, duplicates merged — with reasons |
| `model_switch` | provider model changes (quota/overload) |
| `end` / `error` | stop reason, steps, findings recorded/reported, repo unchanged, token totals / fatal error |

## 5. How it works

```text
agent.py ── validate URL / output / key (before any network call)
   │
   ▼
RepoWorkspace ── shallow `git clone --depth 50` into a temp dir (hooks, symlinks, prompts, LFS off)
   │
   ▼
AuditAgent loop (auditor/loop.py) — hand-written, no framework
   Observe   harness seeds the first turn with list_files + recent git_log (through the registry)
   Reason    the LLM states what it will check and why
   Select    the LLM requests one or more tool calls
   Execute   ToolRegistry: validate args → run with timeout → redact secrets → cap length
   Interpret results go back to the LLM, which decides the next step
   Record    record_finding / retract_finding; finish_audit (checks all 9 categories were examined)
   Budgets   40 steps, 30 min; warnings near the end; nudges if the model stalls
   │
   ▼
Evidence verification (auditor/verify.py)
   drop findings on files no tool showed · content claims need the file's content
   correct cited lines from quoted code · merge duplicates (same rule+file, or same file+line)
   │
   ▼
Report (auditor/report.py) ── order, validate against the contract, atomic write · trace closed
   │
   ▼
temporary clone deleted (also on errors and Ctrl-C)
```

Key design points (all decisions are recorded in [DECISION.md](DECISION.md)):
- **The LLM chooses every inspection.** The system prompt (`auditor/prompts.py`) gives a method,
  an investigation playbook, precision rules (acceptable alternatives, no remote-setting
  guesses, no derived duplicates) and the rule catalogue — guidance, not a fixed checklist.
- **Tools report facts, not verdicts.** E.g. `inspect_workflows` lists triggers, permissions,
  pinning and "signals to verify"; the LLM decides what is a violation and confirms with
  `read_file`.
- **Provider-neutral.** `auditor/llm.py` defines one interface; `llm_gemini.py` and
  `llm_anthropic.py` implement it with the official SDKs. The conversation history is
  append-only (reasoning signatures are preserved).

More detail: [AGENT.md](AGENT.md) (architecture), [INSTRUCTION.md](INSTRUCTION.md) (requirements),
[PLAN.md](PLAN.md) (build steps S0–S15).

## 6. Inspection tools

All tools are read-only, confined to the clone, size-capped and return errors as text so the
LLM can adapt.

| Tool | Purpose |
|---|---|
| `list_files(path, max_depth)` | Tracked files with sizes; large trees summarised per directory |
| `read_file(path, start_line, end_line)` | Line-numbered content (400 lines/call); binary/huge files described |
| `search_code(pattern, path_glob, case_insensitive, max_results)` | Regex search → `file:line: text` |
| `git_log(max_count, path)` | Recent commits with author, date and added/modified/deleted files |
| `inspect_workflows(path)` | Structured GitHub Actions summary + signals; detects other CI systems |
| `file_exists(paths)` | Batch existence check for paths/globs (to look for acceptable alternatives) |
| `git_file_history(pattern)` | Files ever committed matching a glob, incl. deleted ones (e.g. `.env`) |
| `search_history(pattern)` | Commits that added/removed matching lines (ids and files only, never content) |
| `run_ruff(path)` | Static Python lint with ruff (`--no-cache`, never executes code) |
| `git_status()` | Proves the clone was not modified |
| `record_finding` / `retract_finding` / `finish_audit` | Control tools for the agent's findings |

## 7. What it checks

Nine areas from the assignment, each with catalogue rules (see `auditor/rules.py`):

| Category | Examples |
|---|---|
| Repository hygiene | missing README/.gitignore, committed caches, build artefacts, OS files, venvs |
| Dependency management | unconstrained versions, missing lockfile for the toolchain, inconsistent specs |
| Code quality | no linter/formatter, linting not enforced, obvious maintainability problems |
| Testing | no tests, tests not run in CI, ignored test failures, misconfigured test/coverage paths |
| GitHub Actions / CI | missing CI, wrong triggers, gates that cannot fail, branch-pinned actions, invalid YAML |
| Security | hardcoded credentials, committed keys/.env (also in history), broad permissions, `pull_request_target` misuse, script injection |
| Configuration | machine-specific paths, committed local settings, no environment separation, config from another project |
| Collaboration | unsafe auto-merge, missing contribution guidance where relevant |
| Build and release | no build validation, ungated deploys, long-lived publishing tokens, risky Dockerfiles |

Out of scope (per the assignment): ML training, datasets, model validation, experiment tracking.
Acceptable alternatives are not flagged (e.g. `uv.lock`/`poetry.lock` instead of
`requirements.txt`, ruff config in `pyproject.toml`, GitLab CI instead of GitHub Actions,
tag-pinned actions). Remote settings such as branch protection are never inferred from files.

## 8. Safety guarantees

- **Never executes repository code**: no installs, tests or scripts from the target; only
  read-only git commands (argument lists, no shell) and static ruff.
- **Never modifies the repository**: work happens in a temporary clone that is deleted afterwards;
  `git status` is checked at the end and recorded in the trace.
- **Hardened clone**: hooks disabled, symlinks checked out as plain files, no credential prompts,
  no submodules/LFS, 120 s timeout, 1 GiB size limit; path traversal (`..`, absolute paths, `.git/`)
  rejected by every tool.
- **Never exposes secrets**: tool output, trace, logs and report pass through a redactor
  (provider token formats, URL passwords, secret-named assignments, private keys, high-entropy
  strings); the agent sees `[REDACTED:kind]`, and the report validator rejects secret-looking text.
- **Never invents results**: findings citing files no tool showed are dropped; cited lines are
  checked; unavailable checks (e.g. ruff on a non-Python repo) say so.
- **Graceful failures**: invalid input, missing key, API errors (retries with backoff, quota-aware
  model fallback), timeouts and Ctrl-C all end with a clear message and a non-zero exit code.

## 9. Testing and evaluation

```bash
pytest                                              # ~310 tests, ~45 s
pytest --cov=auditor --cov=tools --cov=agent        # coverage (~97 %)
ruff check . && ruff format --check .               # lint
```

The test suite needs **no API key and no network**: the LLM is scripted (`FakeLLM`), repositories
are local git fixtures, and an autouse fixture fails any test that opens a network connection.
It covers every tool, redaction, the registry, the loop (budgets, nudges, refusals), verification,
the report contract, tracing, CLI exit codes and an end-to-end audit of a repository with injected
defects. CI runs it on every push (Ubuntu with Python 3.10/3.12/3.13 and Windows with 3.12).

**Precision / recall evaluation** — three fixture repositories mirroring the hidden evaluation
(A obvious issues, B subtle CI/security/config defects, C healthy) with answer keys:

```bash
python scripts/evaluate.py                  # live run on all fixtures (uses LLM quota)
python scripts/evaluate.py --score-only --out runs/eval/round5   # re-score saved reports
```

Latest live results (gemini-3.5-flash-lite, free tier): **precision 1.00, recall 1.00** on 21 known
defects, no false positives on the healthy repository. These fixtures were also used for tuning
and results vary between runs, so treat them as a sanity check rather than a guarantee
(history of all rounds in DECISION.md, D30).

Developer helpers in `scripts/`: `try_tool.py` (call one tool on a real repo), `llm_smoke.py`
(one real tool-call round trip to test a key), `scan_redactions.py` (see what the redactor masks).

## 10. Project layout

```text
agent.py                 CLI entry point (validation, wiring, exit codes)
tools.py                 Inspection tools (RepoTools)
auditor/
  workspace.py           URL validation, safe clone, read-only git runner
  workflows.py           GitHub Actions analysis for inspect_workflows
  redact.py              Secret redaction and output truncation
  llm.py                 Provider-neutral LLM interface, history types, FakeLLM
  llm_gemini.py          Gemini client (default, free tier)
  llm_anthropic.py       Anthropic client (optional)
  registry.py            Tool dispatcher: validation, timeouts, redaction
  toolset.py             Tool schemas, control tools, observation tracking
  loop.py                The agent loop
  prompts.py             System prompt and harness messages
  rules.py               Rule catalogue
  findings.py            Finding model and audit state
  verify.py              Evidence verification
  report.py              Report ordering, validation, atomic write
  trace.py               JSONL trace writer
  env.py, errors.py      .env loading, error types / exit codes
evaluation/              Fixture repositories, answer keys and scoring (never used by the agent)
scripts/                 evaluate.py and developer helpers
tests/                   Automated tests (offline)
sample_report.json       Authentic example output (+ sample_report.trace.jsonl)
CLAUDE.md, AGENT.md, INSTRUCTION.md, PLAN.md, DECISION.md   Project documentation
```

## 11. Troubleshooting

| Message | Fix |
|---|---|
| `GEMINI_API_KEY is not set` | Create `.env` from `.env.example` and paste your key (no quotes) |
| `Gemini API key was rejected` | Key typo or revoked key; create a new one in AI Studio |
| `Gemini free-tier daily quota exhausted ... for every configured model` | Quotas reset at midnight Pacific time; use another key, or `AUDITOR_PROVIDER=anthropic` |
| `repository not found or not public` | Check the URL; private repositories are not supported |
| `Unsupported repository URL` | Use `https://github.com/<owner>/<repo>` |
| Slow runs / `retrying` warnings | Free-tier rate limits or overload; the agent waits and retries automatically |

## 12. Limitations

- Static analysis only: failures that appear only at runtime (e.g. a test that fails when run) are
  judged from configuration, not by running anything.
- Shallow history (50 commits): older history is not inspected; the trace says so.
- Remote GitHub settings (branch protection, repository secrets, required reviews) are invisible.
- The free Gemini model is small; results vary somewhat between runs. A stronger model (e.g.
  `AUDITOR_PROVIDER=anthropic`) is a one-line change.
- Public GitHub repositories only.
