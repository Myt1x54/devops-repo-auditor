# DECISION.md — Design Decisions Log

Format: each decision has an ID (`D<n>`), status, context, decision, rationale, and consequences.
IDs are never reused or renumbered; a changed decision is marked *Superseded by Dn*.
Status: **Proposed** (awaiting user verification) · **Accepted** · **Superseded**.

---

## D0 — Hand-written agent loop, no frameworks
- **Status:** Accepted
- **Context:** Assignment forbids LangChain, LangGraph, n8n and similar; grading gives 20% to agent
  architecture.
- **Decision:** Implement the loop, tool registry, dispatch and stopping logic in plain Python using
  only the provider's official SDK for HTTP calls. Python 3.10+.
- **Consequences:** Full control over tracing/budgets; slightly more code to write and test.

## D1 — LLM provider: Anthropic Claude via native tool use (pluggable)
- **Status:** Superseded by D22 (Gemini free tier is now the default; Anthropic stays optional)
- **Context:** Model choice doesn't affect the grade; the evaluator supplies API credentials, so the
  provider must be documented clearly.
- **Decision:** Use the `anthropic` SDK with native tool-use (`tools=[...]`, `tool_use` /
  `tool_result` blocks). Default model configurable via `--model` / `AUDITOR_MODEL` env var.
  Keep a small `LLMClient` interface so an OpenAI-compatible provider can be added in one file if
  needed.
- **Alternatives:** OpenAI SDK (also fine), Groq/Gemini (free tiers, weaker tool use).
- **Consequences:** Needs `ANTHROPIC_API_KEY` in `.env`. If you only have an OpenAI/Gemini key, we
  swap the provider in S6 and record a superseding decision.

## D2 — Input: public GitHub HTTPS URLs only
- **Status:** Accepted
- **Decision:** Accept `https://github.com/<owner>/<repo>[.git][/]` (also `/tree/<branch>` stripped
  to the repo). Reject other hosts, SSH URLs, local paths, and owner/repo names with illegal chars
  before any network call. Report `repository` exactly as the user passed it (normalised without
  trailing `.git`/slash).
- **Consequences:** Clear early errors; prevents argument injection into `git`.

## D3 — Acquisition by shallow `git clone` (no GitHub API)
- **Status:** Accepted
- **Context:** `git_log()` needs history; GitHub API has rate limits and needs tokens.
- **Decision:** `git clone --depth 50 --single-branch --no-tags` into a fresh temp dir, with
  `GIT_TERMINAL_PROMPT=0`, `core.hooksPath` pointed at an empty dir, `GIT_LFS_SKIP_SMUDGE=1`, a
  timeout (default 120 s). Temp dir always removed in `finally`.
- **Consequences:** History limited to 50 commits (enough for hygiene/secret-history checks);
  remote settings (branch protection, repo secrets) are invisible — consistent with the evidence rule.

## D4 — Strict read-only, no execution of target code
- **Status:** Accepted
- **Context:** "Leave repository unchanged"; "do not execute untrusted code without isolation".
- **Decision:** Tools only read files and run read-only git commands with fixed argument lists
  (`subprocess.run([...], shell=False)`). No `pip install`, `pytest`, `make`, or scripts from the target.
  `run_pytest` is therefore **not** implemented; "failing tests" is only judged from static evidence
  (e.g. CI config). All paths resolved and confined to the clone root.
- **Consequences:** Safe by construction; cannot detect test failures that only appear at runtime
  (documented as a limitation).

## D5 — LLM-directed investigation with a light coverage nudge
- **Status:** Accepted
- **Context:** A fixed checklist + LLM summary is insufficient; but recall matters (30%).
- **Decision:** The LLM chooses every inspection tool call. The harness only (a) seeds the first
  observation with the file tree, and (b) if the LLM calls `finish_audit` while some of the 9
  categories were never examined, sends one reminder listing them. Findings are recorded through a
  `record_finding` tool so they're tied to the investigation.
- **Consequences:** Genuine agent behaviour visible in traces, while reducing missed categories.

## D6 — Stable rule catalogue with category-prefixed IDs
- **Status:** Accepted
- **Decision:** IDs `<PREFIX>-<NNN>`: `HYG` hygiene, `DEP` dependencies, `CQ` code quality,
  `TST` testing, `CI` GitHub Actions/CI, `SEC` security, `CFG` configuration, `COL` collaboration,
  `BLD` build & release. A catalogue (~40 rules, e.g. `SEC-001` hardcoded credential, `CI-003`
  `pull_request_target` with checkout of PR head) is given to the LLM; it may use `<PREFIX>-900+`
  for issues not in the catalogue. `category` values are snake_case: `repository_hygiene`,
  `dependency_management`, `code_quality`, `testing`, `ci_cd`, `security`, `configuration`,
  `collaboration`, `build_release`.
- **Consequences:** Consistent IDs across runs, easier dedup and evaluation.

## D7 — Tool output limits
- **Status:** Accepted
- **Decision:** Every tool result is capped (default 8 000 chars), files > 1 MB or binary are not
  returned (only metadata), `search_code` returns ≤ 100 hits, `list_files` ≤ 1 000 entries with
  directory summarisation, `read_file` supports line ranges. Errors are returned as text.
- **Consequences:** Bounded token cost; LLM must request specific ranges for big files.

## D8 — Extra tools chosen
- **Status:** Accepted
- **Decision:** Add `inspect_workflows` (structured CI analysis — CI/security is where subtle defects
  hide), `file_exists` (to check acceptable alternatives cheaply), `git_file_history` (secrets/`.env`
  committed then deleted), `run_ruff` (static lint, optional, reports "unavailable" if ruff missing),
  `git_status` (post-run proof target is unchanged). Skip `run_pytest` (D4).

## D9 — Harness-side evidence verification for precision
- **Status:** Accepted
- **Context:** Unsupported claims count as false positives; precision is half of 30%.
- **Decision:** Before writing the report, each finding is checked: cited file exists in the clone;
  line is within the file; the file (or search hit) was actually observed by a tool during the run;
  for "missing X" findings `file` is `null`. Failing findings are dropped or have `line` set to
  `null` (logged in the trace). Duplicates by `(rule_id, file, line)` are merged.
  Optionally a final LLM self-review turn lets the agent retract weak findings.
- **Consequences:** Fewer hallucinated findings; slightly more code.

## D10 — Severity rubric
- **Status:** Accepted
- **Decision:**
  - **critical** — exploitable now: committed live secrets/private keys, `pull_request_target`
    running untrusted PR code with secrets/write token.
  - **high** — serious risk or broken gate: `permissions: write-all`, CI tests skipped/`|| true`,
    deploy on every push without gates, `.env` committed, no tests at all.
  - **medium** — best-practice gaps with real impact: unpinned deps / no lockfile, unpinned
    third-party actions, no CI test run, missing linting, machine-specific paths.
  - **low** — hygiene/documentation: missing README sections, minor `.gitignore` gaps, missing
    CONTRIBUTING/PR template where relevant.

## D11 — Secret handling
- **Status:** Accepted
- **Decision:** A single `redact()` function masks secret-looking values in every tool output, LLM
  message trace, log line and the final report (keeps a short non-sensitive prefix such as `AKIA…`
  so the LLM still knows what kind of secret it is). Evidence describes the secret, never quotes it.
- **Consequences:** Satisfies "never reproduce secret values" even if the LLM tries.

## D12 — Exit codes & error policy
- **Status:** Accepted
- **Decision:** `0` audit completed (report written, even with 0 violations); `1` fatal runtime error
  (clone failure, LLM auth/API failure after retries, unexpected exception); `2` invalid CLI usage /
  invalid URL. Messages go to stderr. If the LLM fails mid-run after findings were recorded, we still
  exit `1` (no partial report pretending to be complete) — *alternative: write partial report with
  exit 0; tell me if you prefer that.*

## D13 — Tool-call trace file
- **Status:** Accepted
- **Context:** Graders may inspect tool-call traces.
- **Decision:** `--trace <path>` (default `<output>.trace.jsonl`) writes one JSON line per event:
  LLM turn (reasoning text, tool calls), tool execution (name, args, duration, truncated+redacted
  result), finding recorded/dropped, final stats.

## D14 — Scope guard & acceptable alternatives list
- **Status:** Accepted
- **Decision:** The system prompt explicitly lists out-of-scope topics (ML training/data/model
  tracking) and a table of equivalents that must not be flagged as missing (e.g. Poetry/PDM/uv lock
  files, `pyproject.toml` tool configs, pre-commit hooks as lint setup, non-GitHub CI such as
  GitLab CI/CircleCI when present, `tests/` vs `test/` vs co-located tests, `.env.example` vs
  `.env.sample`).
- **Consequences:** Directly targets false positives on the "healthy" hidden repo.

## D15 — Single pinned `requirements.txt`; course PDF not committed
- **Status:** Accepted
- **Context:** The evaluator must "clone, install dependencies, run". Tests must also be runnable.
- **Decision:** One `requirements.txt` with exact `==` pins (runtime: `anthropic`, `python-dotenv`,
  `PyYAML`; dev: `pytest`, `ruff`). `pyproject.toml` holds only ruff/pytest config and
  `requires-python >= 3.10` (developed on Python 3.14). The assignment PDF is git-ignored so the
  public repo doesn't redistribute course material.
- **Consequences:** One install command for everything; ruff is guaranteed present for `run_ruff` (S5).

## D16 — Extra clone hardening and limits
- **Status:** Accepted
- **Context:** S2 implementation details beyond D3/D4.
- **Decision:**
  - `core.symlinks=false`: symlinks in the target are checked out as plain text files, so no tool can
    follow a link outside the clone (defence in depth with the S3 path guard).
  - Hooks pointed at an empty directory, credential helpers disabled, `GIT_CONFIG_NOSYSTEM=1`,
    `protocol.ext.allow=never`, no submodules, `stdin` closed (git can never wait for input).
  - Clone timeout 120 s; repos larger than 1 GiB after checkout are refused (exit 1).
  - Clone errors are mapped to plain messages ("repository not found or not public",
    "network error ...").
  - The same safe `-c` options are reused by `RepoWorkspace.run_git()`, which every git-based tool
    (S3/S5) must use.
  - Tests clone a local fixture repo via a `file://` URL, so the suite needs no network.
- **Consequences:** Can't audit symlink targets' contents (rarely relevant to DevOps checks).

## D17 — Inspection tool design details
- **Status:** Accepted
- **Context:** S3 implementation choices for the four mandatory tools.
- **Decision:**
  - Tools live in a `RepoTools` class bound to one clone; methods return plain text for the LLM.
  - **Tracked files only:** `list_files` / `search_code` use `git ls-files`, so the agent sees exactly
    what is committed (the relevant fact for hygiene/secret findings).
  - **Path guard:** reject absolute paths, drive letters, `~`, `..` and `.git/`; then resolve and
    require the result to stay inside the clone root.
  - **Errors are text:** bad input returns `ERROR: ...` (via a `tool_safe` wrapper) so the LLM can
    correct itself; nothing raises into the loop.
  - **Limits:** `read_file` ≤ 400 lines/call with a "call again with start_line=N" hint, files
    > 1 MB or binary not shown, lines cut at 500 chars; `search_code` ≤ 100 hits, 10 s budget,
    skips binary/large files; `list_files` collapses into per-directory counts above 400 entries;
    `git_log` ≤ 50 commits, ≤ 15 files per commit.
  - `git_log` shows hash, date, author **name** (no e-mail addresses — personal data) and A/M/D
    file changes, so committed-then-deleted files (e.g. `.env`) are visible. It says when history
    is shallow.
  - `search_code` globs use `fnmatch` (`*` also matches `/`), on full path or file name.
  - Tool text is ASCII-only (Windows consoles mangle `…`/`—`).
  - `scripts/try_tool.py` lets a developer call any tool by hand against a real repo.
- **Consequences:** Untracked/ignored files are invisible (they don't exist in a fresh clone anyway).

## D18 — Redaction strategy details
- **Status:** Accepted
- **Context:** S4 implementation of D11 ("never expose secrets in logs or output").
- **Decision:**
  - **What is masked:** (1) known token shapes (AWS, GitHub, GitLab, Slack, Google, Stripe,
    OpenAI/Anthropic, npm, SendGrid, JWT); (2) passwords inside URLs (`scheme://user:PW@host`) and
    `Bearer/Basic` auth values; (3) values of `key = value` / `key: value` pairs whose key names a
    secret (`password`, `secret`, `token`, `api_key`, `PGPASSWORD`, ...); (4) private-key block
    bodies; (5) long high-entropy strings (≥ 32 chars, > 4.3 bits/char, mixed case + digits).
  - **Mask format:** `[REDACTED:<kind>]`, keeping the key name, quotes and a safe provider prefix
    (`AKIA[REDACTED:aws_access_key_id]`, `sk_live_[REDACTED:stripe_key]`). Line numbers are kept,
    so findings can still cite the exact line.
  - **Not masked (so the agent can judge correctly):** env-var references (`os.getenv`, `${VAR}`,
    `${{ secrets.X }}`), placeholders (`changeme`, `your_api_key_here`, `<key>`, `xxxx`), empty
    values, numbers, type hints, dotted references, GitHub Actions permission levels (`read`/`write`),
    hex digests (commit SHAs, pinned action SHAs), lock-file integrity hashes, URL paths.
  - **Err on the side of masking:** fake credentials in test fixtures are masked too; the agent
    still sees the file path (`tests/...`) and key name to judge whether it's a real problem.
  - **Linear-time matching:** the `key = value` scan is split into a boundary-anchored key regex plus
    an anchored value match, so a 200 000-char minified line is processed in milliseconds (the
    first version was quadratic and hung on real repositories; a regression test guards this).
  - `sanitize()` = `redact()` then `truncate()` (8 000 chars, cut at a line end, with a
    "narrow the request" hint). It will be the single gate in the tool dispatcher (S7); the trace
    (S11) and final report (S9) also pass through `redact()`.
  - Test fixtures build fake tokens by string concatenation so the repo never contains anything
    that GitHub push protection would block.
  - `scripts/scan_redactions.py` lists every line the redactor would mask in a real repo, for
    checking false positives.
- **Consequences:** The LLM sees *that* a secret exists and where, never its value. Some
  over-masking in test fixtures/certificates is accepted.

## D19 — Extra tools: facts plus neutral "signals", the LLM decides
- **Status:** Accepted
- **Context:** S5. The assignment rejects "static scanner + LLM summary", yet subtle CI/security
  defects (hidden repo B) are easy to miss by reading YAML alone.
- **Decision:**
  - `inspect_workflows` returns a structured *summary* (triggers, permissions, jobs, steps classified
    as test/lint/build/deploy, action pinning, secrets referenced, line numbers) and a list of
    **"signals to verify (facts, not verdicts)"**. It never emits violations, rule IDs or
    severities; the LLM decides whether a signal is a real problem in context and should confirm with
    `read_file`. Invalid YAML and non-GitHub CI systems (GitLab, CircleCI, Jenkins, ...) are reported
    so the agent doesn't claim "no CI" when another CI exists (evidence rule).
  - `file_exists(paths)` checks many paths/globs in one call, to look for acceptable alternatives
    cheaply before claiming something is missing.
  - `git_file_history(pattern)` and `search_history(pattern)` cover secrets/files committed and later
    removed. `search_history` returns commit ids and file names only, never content.
  - `run_ruff` uses our pinned ruff with `--no-cache --exit-zero --no-fix`, so it writes nothing
    into the clone; a test asserts `git status` stays clean afterwards. Uses the project's own ruff
    config if present and says so.
  - `git_status` provides proof that the inspected repository was not modified.
  - Workflow analysis lives in `auditor/workflows.py` (pure function on text, easy to test);
    `tools.py` exposes it as a tool.
- **Consequences:** Strong CI/security recall support while keeping the LLM in charge of judgement.
  Line numbers in the workflow summary are best-effort text matches (folded YAML handled).

## D20 — LLM client details (Anthropic Messages API)
- **Status:** Accepted (now applies to the optional Anthropic provider only, see D22)
- **Context:** S6, implementing D1 with the installed `anthropic` 2.3.0 SDK.
- **Decision:**
  - **Model:** `claude-opus-5-5` by default (current Opus); override with `--model` / `AUDITOR_MODEL`.
  - **Effort:** `output_config.effort = "high"` (agentic investigation benefits from it; this model's
    default would be `medium`); override with `AUDITOR_EFFORT`. Thinking is always on for this model
    (adaptive), so no `thinking` parameter is sent; no `temperature` (no longer accepted).
  - **Own loop, not Tool Runner:** we call `messages.create` ourselves each turn (D0). `tool_choice`
    stays `auto` (forced tool choice is rejected by this model).
  - **Refusal fallback:** `fallbacks: "default"` with beta `server-side-fallback-2026-07-01`, so a
    false-positive safety refusal on security-related content is re-run on another model instead of
    killing the audit. Disabled automatically for Haiku models (not supported there).
  - **Prompt caching:** top-level `cache_control: {"type": "ephemeral"}` (automatic caching): the
    growing history is read from cache each turn, which cuts the cost of a 40-step run a lot.
  - **Append-only history:** the raw response content (including reasoning blocks) is appended back
    unchanged, and earlier turns are **never edited or trimmed**: this model ties its reasoning
    blocks to the exact history. This supersedes the "summarise/trim older tool results" idea in
    the original S8 plan; tool outputs are already capped at 8 000 chars (D7), so a 40-step run
    stays far below the 1M-token context.
  - **Reliability:** SDK retries (5 attempts, exponential backoff) for 408/409/429/5xx/connection
    errors, 300 s timeout per request. Remaining failures become `LLMError` (exit 1) with a clear
    message (bad key, permission, unknown model, bad request, rate limit, network, timeout).
  - **Usage accounting:** calls, input/output tokens, cache reads/writes and seconds are summed for
    the trace and run summary.
  - `FakeLLM` (scripted responses) lets the agent loop be tested without network or API key.
  - `scripts/llm_smoke.py` does one real tool-call round trip to check a key/model (< 1 cent).
- **Consequences:** Runs cost real money per audit; effort/model are tunable via env without code
  changes.

## D21 — `.env` values override the shell, but empty values never do
- **Status:** Accepted
- **Context:** During the S6 smoke test a stale `ANTHROPIC_API_KEY` in the shell environment was
  rejected (401). With python-dotenv's default, that stale value would silently win over a correct
  key placed in `.env`.
- **Decision:** `auditor/env.py:load_env()` copies only **non-empty** `.env` values into the
  environment, overriding the shell. Empty values (e.g. an untouched copy of `.env.example`) are
  ignored, so an evaluator who exports the key in the shell is not broken either.
- **Consequences:** Predictable configuration in both setups; documented in `.env.example`.

## D22 — Default LLM provider: Google Gemini free tier; Anthropic optional
- **Status:** Accepted
- **Context:** The user has no budget for a paid API key. The assignment states the provider/model
  does not affect the grade, and allows basic provider SDKs.
- **Decision:**
  - `AUDITOR_PROVIDER=gemini` (default) uses the official `google-genai` SDK (2.33.0) with a free
    key from Google AI Studio (`GEMINI_API_KEY`). `AUDITOR_PROVIDER=anthropic` keeps the D20 client.
  - **Provider-neutral interface:** the loop keeps a neutral history (`UserTurn`, `AssistantTurn`,
    `ToolResultsTurn`) and neutral `ToolSpec`s; `auditor/llm_gemini.py` and
    `auditor/llm_anthropic.py` convert them. Stop reasons are normalised (`end_turn`, `tool_use`,
    `max_tokens`, `refusal`, `malformed_tool_call`).
  - **Append-only:** each model turn is resent exactly as received (Gemini thought signatures and
    Anthropic reasoning blocks preserved).
  - **Gemini settings:** automatic function calling disabled (our loop runs the tools),
    `thinking_level` from `AUDITOR_EFFORT` (default `medium`), 16 000 max output tokens.
  - **Model choice by measurement (Oct 2026, this key):** `gemini-3.8-flash` 60-134 s per call
    (overloaded, 503s); `gemini-3.6-flash` 1-4 s with correct tool calls; `gemini-3.5-flash-lite`
    ~1 s; `gemini-2.5-flash` 404. Default `gemini-3.6-flash`; after 3 consecutive 503s the client
    switches to `gemini-3.5-flash-lite` for the rest of the run (recorded for the trace).
  - **Free-tier rate limits:** 429 → wait the server's `retryDelay` (or exponential backoff up to
    90 s), up to 8 attempts; a *daily* quota 429 fails fast with a clear message; 5xx and network
    errors retried with backoff; bad key / unknown model / bad request are fatal (exit 1).
  - **Privacy:** free-tier prompts may be used by Google to improve products. Acceptable: only
    public repositories are audited and secret values are redacted before sending (D11, D18).
- **Consequences:** Zero-cost runs; slower than paid tiers and subject to daily quotas. The
  evaluator can use either provider by setting one env var.

## D23 — Tool registry, argument handling and control-tool validation
- **Status:** Accepted
- **Context:** S7. Every LLM tool call passes through one dispatcher; LLMs (especially smaller free
  models) often send slightly wrong argument types or invent tools.
- **Decision:**
  - `auditor/registry.py` `ToolRegistry.dispatch()` is the single gate: unknown tool → error listing
    valid tools; arguments validated against the JSON schema with **lenient coercion** (`"5"` → 5,
    `"true"` → true, single string → one-element list), unknown arguments ignored with a visible
    note, explicit `null` for optional arguments treated as "not given"; tool runs in a worker
    thread with a timeout (120 s default, 180 s for ruff); any exception becomes `ERROR: ...`;
    output is redacted then capped (8 000 chars for inspection tools, 4 000 for control tools).
  - Results starting with `ERROR` are sent back with `is_error=true` so the model can correct itself.
  - Each call produces a `DispatchRecord` (name, redacted args, duration, size, truncated, error)
    for the trace (S11). Tool specs are registered in a fixed order so the prompt prefix stays
    cacheable.
  - **Observation tracking:** wrappers record which files each tool showed: `read_files` (content
    seen via read_file / search_code / inspect_workflows / run_ruff), `seen_files` (named in any
    output incl. listings) and `history_files` (only in git history, e.g. a deleted `.env`). Used by
    S9 evidence verification (D9).
  - **`record_finding` validates immediately** (fast feedback beats silent dropping later):
    rule-id format and prefix must match the category; category/severity enums; description,
    evidence and recommendation ≥ 10 chars; `file` must be a tracked file or a file seen in history
    (omit it for repository-wide findings); `line` must lie inside the file; duplicates
    `(rule_id, file, line)` are refused. Recording a finding on a file the agent never inspected is
    allowed but returns a warning.
  - `retract_finding` and `finish_audit` complete the control set; findings carry internal ids
    (F1, F2, ...) that never appear in the report.
  - Schemas avoid exotic JSON-schema features (no `additionalProperties`, nullable types expressed
    as optional fields) so both Gemini and Anthropic accept them — verified live: Gemini accepted
    all 13 tool schemas.
- **Consequences:** Robust against sloppy tool calls; the model gets actionable errors instead of
  the run failing.

## D24 — Agent loop control flow, budgets and nudges
- **Status:** Accepted
- **Context:** S8. The loop must let the LLM drive the investigation (D5) while staying robust with
  free-tier models that sometimes stop early, ramble, or produce broken tool calls.
- **Decision:**
  - **Observe:** the harness seeds the first user turn with the task and the repository tree
    (`list_files` depth 2, redacted). Everything after that is chosen by the LLM.
  - **Each step:** one LLM call → every requested tool runs through the registry (parallel calls are
    answered together in one turn) → results appended → next step. History is append-only.
  - **Stop conditions:** `finish_audit` accepted (normal); step budget (default 40, `--max-steps` /
    `AUDITOR_MAX_STEPS`); wall-clock budget (30 min); 3 consecutive replies without tool calls ("idle").
    Findings recorded so far are always reported when a budget ends the run.
  - **Budget awareness:** when ≤ 3 steps remain, a short `[harness]` note is appended to the last tool
    result telling the model to record findings and finish; at the last step a final note. On time
    budget the model gets one final turn in which only control tools are executed. Notes are appended
    to tool-result text (not as extra messages) so both providers accept the history.
  - **Nudges (harness "interpret" of non-tool replies):** plain stop → "continue with tools or
    finish" (max 2); output cut off → "shorter replies"; malformed tool call → "retry with valid
    JSON"; provider refusal → one retry, a second refusal is fatal (exit 1, D12).
  - **Coverage check (implements D5):** `finish_audit` requires `categories_examined`; if any of the
    9 categories is missing on the first attempt the harness refuses once and lists them. The model
    decides whether to inspect them or to finish anyway with a reason.
  - **Fail fast:** the LLM client is created before cloning, so a missing key fails in < 1 s.
  - **After the loop:** `git_status` confirms the clone is unchanged (warning if not).
  - **Trace hooks:** the loop emits `start`, `llm`, `tool`, `nudge`, `end` events (written to a file
    in S11); INFO logs show the agent's stated reasoning and each tool call.
  - **Test isolation:** an autouse fixture removes real API keys/settings and disables `.env`
    loading in tests, after a CLI test accidentally started a real audit.
- **Consequences:** The LLM chooses every inspection; the harness only enforces budgets and gives
  generic reminders. Behaviour is fully testable with `FakeLLM`.

## D25 — Default Gemini model changed to Flash-Lite; model switch on daily quota
- **Status:** Accepted (amends D22)
- **Context:** The first live audit (S8) stopped after 4 LLM calls: `gemini-3.6-flash` has a free-tier
  quota of **20 requests per day per model** (from the 429 error's quota details), and one audit needs
  20-40 requests. Quotas are per model; Google's docs do not publish free-tier numbers.
- **Decision:**
  - Default model `gemini-3.5-flash-lite` (≈1 s per call, correct tool calls in tests, separate
    quota); fallback `gemini-3.1-flash-lite`.
  - When the current model's **daily** quota is exhausted, or it returns 503 three times in a row, the
    client switches to the next fallback model immediately (no waiting) and records the switch;
    only when no fallback is left does it fail with a clear message (quotas reset at midnight PT).
  - Stronger models stay one setting away (`AUDITOR_MODEL=gemini-3.6-flash` with spare quota, or
    `AUDITOR_PROVIDER=anthropic` with a paid key) — the grader may supply their own credentials.
- **Consequences:** Audits fit in the free tier. Flash-Lite is a smaller model, so detection quality
  depends more on the prompt and the harness checks (S9/S10/S13 tuning).

## D26 — Rule catalogue enforcement, evidence verification and report validation
- **Status:** Accepted
- **Context:** S9. Precision (unsupported claims = false positives) and recall are 30% of the grade,
  evidence/severity quality another 15%, JSON compliance part of 10%.
- **Decision:**
  - **Catalogue** (`auditor/rules.py`): 49 generic rules across all 9 categories, each with id,
    default severity, title and "how to confirm / when not to flag" guidance (acceptable
    alternatives built in, e.g. version tags are fine for actions, small projects need no
    CONTRIBUTING). Rendered into the system prompt. `record_finding` accepts catalogue ids or
    `<PREFIX>-9NN` for uncovered issues and rejects anything else with the list of valid ids.
  - **Verification** (`auditor/verify.py`, runs after the loop, never mutates the agent's record):
    (1) drop a finding whose file no tool showed in this run; (2) remove the line if the file's
    content was never shown; (3) check the line against code quoted in the evidence/description,
    using the longest snippet that occurs in the file — correct the line if the snippet is
    elsewhere, keep it if the snippet is nowhere (can't prove either way); (4) merge findings with
    the same `(rule_id, file)` into one, listing extra lines in the evidence (duplicates earn no
    credit and could cost precision); (5) collapse whitespace, redact and cap text fields.
    Every change is logged with a reason (and traced in S11).
  - **Report** (`auditor/report.py`): ordered by severity → category → file → line; validated
    against the exact contract before writing (8 fields in order, enums, positive int or null line,
    line requires file, no secret-looking values); atomic write. An invalid report is a fatal
    error rather than a silently malformed file.
  - Optional LLM self-review pass (D9) is deferred to S13 tuning: only added if measurements show
    it improves precision.
- **Consequences:** Hallucinated or unverifiable claims are filtered by code, not by trusting the
  model; output always matches the contract.

## D27 — System prompt: investigation playbook, precision rules, visible reasoning
- **Status:** Accepted
- **Context:** S10. Two live runs showed: 4 steps wasted listing directories (seeded tree only 2
  levels deep), git-history and alternative-checking tools never used, one tool per step, no
  visible reasoning text, no final self-review.
- **Decision:**
  - **Scoring-aware framing:** the prompt says precision and recall are both scored, so the model
    should be thorough but only claim what it can prove.
  - **Visible reasoning:** one short sentence before each batch of tool calls (feeds the trace).
  - **Parallel calls:** independent tools in the same turn to save steps.
  - **Playbook (9 steps, one per category):** what to look for and *which tools* help (e.g.
    `git_file_history` for `*.env*`/`*.pem`, `search_code` regexes for credentials and machine paths,
    `inspect_workflows` then `read_file`). It is guidance, not a fixed checklist: the model adapts
    it to the repository and decides every call (keeps D5 / the "no static scanner" rule).
  - **Precision rules:** explicit acceptable-equivalents list (manifests, lockfiles, linter config
    locations, other CI systems, test layouts, env templates, tag-pinned actions), dummy
    credentials in fixtures/docs, no remote-setting inference, no padding with style opinions.
  - **Evidence format:** quote the offending code in backticks so `verify.py` can check/correct the
    line (D26).
  - **Self-review:** before `finish_audit`, retract findings disproved by alternatives and remove
    duplicates.
  - **Full first observation:** the seeded file list is now the full `list_files()` output
    (directories collapsed only above 400 files) instead of depth 2.
  - **Guard tests:** every tool the prompt names must exist; the prompt's regexes must compile and
    match; the prompt stays static (cacheable).
- **Consequences:** Longer prompt (~13.6k chars, cached); more systematic coverage while the LLM
  keeps control of the investigation.

## D28 — Trace contents, reasoning capture and hardening
- **Status:** Accepted
- **Context:** S11. Graders may inspect tool-call traces; the S8 live run showed the free model often
  calls tools without visible text, which hides the "Reason" step.
- **Decision:**
  - **Trace file** (`auditor/trace.py`): JSONL next to the report (`report.json` →
    `report.trace.jsonl`, or `--trace PATH`). Events: `start` (repo, provider, model, HEAD),
    `loop_start` (tools offered, prompt size, budgets), `llm` (step, visible text, **reasoning
    summary**, tool calls, tokens), `tool` (name, redacted args, error, duration, size, truncated,
    2 000-char output preview), `nudge`, `verify`, `model_switch`, `end` (stop reason, steps, findings
    recorded/retracted/reported, repository unchanged, token totals), `error` (fatal message).
    Every string is redacted; a trace that cannot be written disables itself and never stops the run.
  - **Reasoning capture:** Gemini `include_thoughts=True` (thought summaries); Anthropic
    `thinking: {type: adaptive, display: summarized}`. Stored as `LLMResponse.reasoning` for the
    trace and INFO log only; the raw content is still resent unchanged (append-only). Live check:
    7 of 8 Gemini turns carried a summary.
  - **Hardening:** all cheap validation (URL, output path is not a directory / can be created,
    `AUDITOR_MAX_STEPS`, API key) happens before any network call; write errors become clear
    `AuditError`s; Ctrl-C and LLM failures write an `error` trace event, remove the temp clone and
    exit 1 without a report (D12); stdout stays empty (logs on stderr); per-request HTTP logs from
    httpx/SDKs are hidden unless `--verbose`.
  - Verified from the real CLI: bad URL / missing args / output is a directory → exit 2; missing
    repo / missing key / rejected key → exit 1; no leftover temp clones; nothing on stdout.
- **Consequences:** Self-contained evidence of genuine agent behaviour per run. Note (D21): a
  non-empty value in `.env` overrides the same variable exported in the shell — documented in the
  README (S14).

## D29 — Test strategy: offline by construction, coverage-guided, CI on GitHub Actions
- **Status:** Accepted
- **Context:** S12. "JSON compliance, tests, documentation" is 10% of the grade; the evaluator must be
  able to run the tests without our API key; a CLI test once accidentally reached a real API (S8).
- **Decision:**
  - **Offline by construction:** autouse fixtures remove API keys/settings, disable `.env` loading
    and block every non-local socket connection (a test that tries to go online fails with
    `NetworkAccessError`). LLMs are scripted with `FakeLLM`; repositories are local git fixtures
    cloned via `file://`.
  - **Coverage-guided gap filling:** `pytest-cov` measured 94 % → 97 % (2 001 statements) after adding
    tests for meaningful edge cases only (workflow_run/reusable workflows/job permissions, search
    time budget, shallow history, git missing, clone/command timeouts, Gemini transport/server
    errors, unexpected harness errors, report write failure, modified clone warning).
  - **End-to-end test:** a defect fixture repo (hardcoded key, deleted `.env` in history,
    `pytest || true`, branch-pinned action, unpinned dependency) audited through the real CLI by a
    scripted agent that reacts to tool output; asserts the exact report, severity ordering, contract
    fields, verification (hallucination dropped, duplicate merged, wrong line corrected) and that no
    secret value reaches the report or trace.
  - **CI** (`.github/workflows/ci.yml`): push to main + pull requests, `permissions: contents: read`,
    lint job (ruff check + format) and test matrix (Ubuntu × Python 3.10/3.12/3.13, Windows × 3.12)
    with coverage; actions tag-pinned, `persist-credentials: false`, timeouts, concurrency. Our own
    `inspect_workflows` reports no signals for it.
  - `pytest-cov` pinned in requirements.txt (dev section).
- **Consequences:** ~300 tests run in ~40 s with no key and no network; regressions caught on every
  push.
