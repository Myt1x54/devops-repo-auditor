# CLAUDE.md — Working Guide for Claude Code

This file tells Claude Code (and any human contributor) how to work in this repository.

## Project in one line
A Python CLI agent (`python agent.py --repo <github_url> --output report.json`) that clones a public
GitHub repository, lets an LLM drive an investigation through inspection tools, and writes an
evidence-based JSON report of DevOps best-practice violations.

Source of truth for requirements: `Assignment_2_DevOps_Repository_Auditing_Agent_Updated.pdf`
(distilled in [INSTRUCTION.md](INSTRUCTION.md)).

## Companion documents
| File | Purpose |
|------|---------|
| [INSTRUCTION.md](INSTRUCTION.md) | Distilled assignment requirements, output contract, grading, constraints |
| [AGENT.md](AGENT.md) | Architecture and behaviour spec of the auditing agent (loop, tools, prompts, rules) |
| [PLAN.md](PLAN.md) | Numbered implementation steps `S0, S1, ...` with status |
| [DECISION.md](DECISION.md) | Numbered design decisions `D0, D1, ...` with rationale |

## Workflow rules (must follow)
1. **Work one step at a time** from [PLAN.md](PLAN.md), in order, unless the user says otherwise.
2. **After every step**: update the step's status in PLAN.md, explain to the user what was built and
   why, show how to verify it (command to run / file to look at), and **stop and ask the user to
   verify** before starting the next step.
3. **Every non-trivial choice gets a decision entry** in [DECISION.md](DECISION.md) with the next free
   number (`D<n>`), context, options considered, decision, consequences. Reference the decision ID
   in the PLAN step that uses it. Never renumber existing decisions; supersede them instead.
4. If a step reveals the plan is wrong, update PLAN.md (and add a decision) rather than silently
   deviating.

## Hard constraints (from the assignment — never violate)
- **No agent/orchestration frameworks**: no LangChain, LangGraph, n8n, CrewAI, AutoGen, LlamaIndex
  agents, etc. Only a basic LLM provider SDK. The agent loop, tool dispatch and control flow are
  hand-written Python.
- **The LLM must direct the investigation** (choose tools, make follow-up calls). A static scanner +
  LLM summary is not acceptable.
- **Never modify the inspected repository** and **never execute its code** (no `pip install`, no
  `pytest`, no scripts from the target). Static analysis only.
- **Never print, log, or report secret values.** Redact them.
- **No hardcoded repository-specific findings.** Rules must be generic.
- **Never invent results**: if a check could not run, say so (or omit), don't guess.
- Run unattended: no interactive prompts. Valid JSON on success (empty `violations` array allowed);
  non-zero exit with a clear message on fatal error.

## Coding conventions
- Python 3.10+, type hints everywhere, `from __future__ import annotations`.
- Format/lint with `ruff` (`ruff check .` and `ruff format .`). Tests with `pytest`.
- Keep modules small: `agent.py` (CLI) · `tools.py` (inspection tools) · `auditor/` package
  (LLM client, loop, prompts, findings, report).
- Tools return plain strings/dicts that are **truncated** and **redacted** before reaching the LLM.
- Every tool must fail gracefully: return an error string to the LLM, never raise into the loop.
- Secrets/config only via environment variables (`.env`, never committed). `.env.example` lists names.

## Common commands
```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
pytest -q
ruff check .
```
