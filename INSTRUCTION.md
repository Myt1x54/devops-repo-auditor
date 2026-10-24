# INSTRUCTION.md — Assignment Requirements (distilled)

Course: MLOps & Production AI Engineering · Language: Python · Submission: one **public GitHub repo URL**.

## 1. Objective
Build an **AI-powered DevOps auditing agent** that autonomously inspects a GitHub repository,
identifies DevOps best-practice violations, and produces an evidence-based report with actionable
recommendations.

Agent cycle: **Observe → Reason → Select Tool → Execute Tool → Interpret Results → Report.**

Scope: software engineering and DevOps only. **Excluded**: ML training, datasets, model validation,
experiment tracking, other MLOps-specific checks.

## 2. Functional requirements
- [ ] Accept a public GitHub repository URL and inspect it automatically.
- [ ] Use an LLM to select inspection tools based on observations, including follow-up tool calls.
- [ ] Identify genuine violations, distinguish acceptable alternatives, give evidence + remediation.
- [ ] Produce the machine-readable JSON report without interactive prompts.
- [ ] Leave the inspected repository unchanged.

> A fixed checklist / static scanner followed by an LLM summary is **insufficient**.

## 3. Required architecture
Explicit Python agent/tool loop. Mandatory tools:

| Tool | Purpose |
|------|---------|
| `list_files()` | Discover repository files and structure |
| `read_file(path)` | Inspect relevant source or configuration |
| `search_code(pattern)` | Search for suspicious patterns |
| `git_log()` | Inspect commit history |

Optional: `run_pytest()`, `run_ruff()`, `git_status()`, GitHub Actions inspection. All tool outputs
must be interpreted by the agent.

**Framework restriction:** n8n, LangChain, LangGraph and similar are prohibited. Basic LLM provider
SDKs are allowed.

## 4. Inspection areas
| Category | Example violations |
|----------|--------------------|
| Repository hygiene | Missing README, unsuitable .gitignore, tracked temp/generated files |
| Dependency management | Inconsistent specs, absent required lockfiles, weak version constraints |
| Code quality | Missing linting/formatting, obvious maintainability problems |
| Testing | Missing tests, failing tests, no CI test execution |
| GitHub Actions / CI | Incorrect triggers, ineffective gates, skipped validation |
| Security | Exposed credentials, unsafe workflow permissions |
| Configuration | Machine-specific paths, committed local settings, poor env separation |
| Collaboration | Unsafe PR automation, lack of review guidance where relevant |
| Build and release | Missing build validation, risky release/deployment automation |

**Evidence rule:** an absent optional file/tool is not a defect when an equivalent solution exists.
Do not infer remote branch-protection settings from repository files.

## 5. Execution interface & output contract
```bash
python agent.py --repo <github_repository_url> --output report.json
```
- Runs unattended. On completion writes valid JSON (empty `violations` array if none).
- On fatal error: exit **non-zero** with a useful message.

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
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```
- Every violation has **all** fields. `severity` ∈ `critical | high | medium | low`.
- `file` / `line` = JSON `null` when not applicable. **Never reproduce secret values.**

## 6. Hidden evaluation
Unseen repos with injected defects: (A) obvious issues, (B) subtler CI/security/config defects,
(C) mostly healthy repo (false-positive test).
- Precision = correct reported / all reported. Recall = correct reported / all known.
- Duplicates earn nothing; unsupported claims count as false positives.
- Tool-call traces may be inspected to verify genuine agent behaviour.

## 7. Grading
| Component | Weight |
|-----------|--------|
| Agent architecture & dynamic tool selection | 20% |
| Inspection-tool implementation | 15% |
| Hidden-repo detection (precision & recall) | 30% |
| Evidence, severity, recommendations | 15% |
| Reliability, error handling, safe execution | 10% |
| JSON compliance, tests, documentation | 10% |

LLM provider/model choice does not affect the grade.

## 8. Submission checklist
- [ ] `agent.py` — CLI entry point with required arguments
- [ ] `tools.py` — inspection tool implementations
- [ ] Agent modules — LLM interaction, tool selection, execution loop
- [ ] `requirements.txt` or `pyproject.toml`
- [ ] `.env.example` — variable names only, no real credentials
- [ ] `README.md` — exact install, setup, architecture, run instructions
- [ ] `tests/` — automated tests for tools and agent behaviour
- [ ] `sample_report.json` — authentic agent output

Evaluator must be able to: clone → install → set API key → run the command on unseen repos.
No ZIPs, screenshots or hand-written reports.

## 9. Constraints & safety
- Python; support public GitHub repos.
- Don't modify targets or execute untrusted code without isolation.
- Don't invent results when a check is unavailable.
- Never expose secrets in logs or output.
- Don't hardcode repo-specific findings or hidden answers.
- Handle missing files, API errors, invalid input and timeouts gracefully.

## 10. Success criterion
Independently investigates unfamiliar repos, identifies defensible DevOps violations, minimises false
positives, produces actionable recommendations.
