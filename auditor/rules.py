"""Generic DevOps rule catalogue (D6, D26).

Stable ids give consistent ``rule_id`` values across runs and make duplicates easy to spot.
Nothing here is specific to any repository: each rule describes a *kind* of problem and how to
confirm it. Severities are defaults; the agent may adjust them with justification.
Issues outside the catalogue use ``<PREFIX>-9NN`` (e.g. ``CI-901``).
"""

from __future__ import annotations

from dataclasses import dataclass

from auditor.findings import CATEGORIES


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    title: str
    severity: str
    check: str  # how to confirm / when NOT to flag


_RULES = [
    # -- repository hygiene ------------------------------------------------------------------
    Rule("HYG-001", "repository_hygiene", "Missing README", "medium",
         "No README.* at the repository root."),
    Rule("HYG-002", "repository_hygiene", "README lacks setup or usage instructions", "low",
         "README exists but gives no way to install, configure or run the project."),
    Rule("HYG-003", "repository_hygiene", "Missing or inadequate .gitignore", "low",
         "No .gitignore, or it misses the language's standard entries (e.g. __pycache__/, "
         ".env, venv/, node_modules/, dist/)."),
    Rule("HYG-004", "repository_hygiene", "Generated, build or cache artefacts committed", "medium",
         "Tracked files such as *.pyc, __pycache__/, .pytest_cache/, dist/, build/, *.egg-info, "
         "node_modules/, coverage reports, log files."),
    Rule("HYG-005", "repository_hygiene", "OS or editor files committed", "low",
         "Tracked .DS_Store, Thumbs.db, *.swp, .idea/ workspace files."),
    Rule("HYG-006", "repository_hygiene", "Virtual environment committed", "medium",
         "Tracked venv/, .venv/, env/ with site-packages or activate scripts."),
    Rule("HYG-007", "repository_hygiene", "Large binary or data files committed", "low",
         "Big archives, binaries or datasets tracked in git without Git LFS."),
    # -- dependency management ---------------------------------------------------------------
    Rule("DEP-001", "dependency_management", "Dependencies without version constraints", "medium",
         "Runtime dependencies listed with no version or bound at all (e.g. 'requests'), and no "
         "lockfile pins them."),
    Rule("DEP-002", "dependency_management", "No dependency manifest", "medium",
         "Code imports third-party packages but there is no requirements*.txt, pyproject.toml, "
         "setup.py/cfg, Pipfile, package.json or equivalent."),
    Rule("DEP-003", "dependency_management", "Missing lockfile", "medium",
         "The toolchain in use expects a lockfile and none is committed (package.json without "
         "package-lock/yarn.lock/pnpm-lock; Poetry/Pipenv/uv projects without their lock). Not "
         "for plain libraries using version ranges."),
    Rule("DEP-004", "dependency_management", "Inconsistent dependency specifications", "medium",
         "The same package declared with conflicting versions in different files, or a lockfile "
         "that contradicts the manifest."),
    Rule("DEP-005", "dependency_management", "Dependency from mutable or insecure source", "medium",
         "Dependencies installed from a git branch, unpinned URL, or plain-http/untrusted index."),
    Rule("DEP-006", "dependency_management", "Dev/test dependencies mixed into runtime", "low",
         "pytest, linters etc. listed as runtime requirements with no separate dev group/file."),
    # -- code quality --------------------------------------------------------------------------
    Rule("CQ-001", "code_quality", "No linter or formatter configured", "medium",
         "No ruff/flake8/pylint/black/eslint/prettier/... config in any config file and no "
         "pre-commit hooks."),
    Rule("CQ-002", "code_quality", "Linting not enforced", "low",
         "A linter is configured but never run in CI or pre-commit."),
    Rule("CQ-003", "code_quality", "Obvious maintainability problems", "low",
         "Concrete evidence such as many unused imports, bare except: pass, or large blocks of "
         "commented-out code (cite ruff output or the lines)."),
    Rule("CQ-004", "code_quality", "Quality checks globally disabled", "low",
         "Lint rules or type checking switched off wholesale (e.g. ignore everything, "
         "# noqa / type: ignore everywhere)."),
    # -- testing -------------------------------------------------------------------------------
    Rule("TST-001", "testing", "No automated tests", "high",
         "No test files anywhere (check tests/, test/, *_test.py, test_*.py, *.spec.*)."),
    Rule("TST-002", "testing", "Tests not run in CI", "medium",
         "Tests exist and CI exists, but no CI job runs them."),
    Rule("TST-003", "testing", "Test failures ignored", "high",
         "A step that runs the TESTS cannot fail: '|| true', continue-on-error, exit 0. Record "
         "this separately from ignored lint/type-check steps (CI-003)."),
    Rule("TST-004", "testing", "Tests skipped or disabled wholesale", "medium",
         "Test job disabled (if: false), whole suites skipped, or test command commented out."),
    Rule("TST-005", "testing", "Test or coverage configuration ineffective", "medium",
         "testpaths / coverage source / test discovery points at the wrong package or path, so "
         "tests or coverage silently miss the project's code."),
    # -- CI/CD ---------------------------------------------------------------------------------
    Rule("CI-001", "ci_cd", "No continuous integration", "medium",
         "No GitHub Actions workflows and no other CI configuration (GitLab, CircleCI, ...)."),
    Rule("CI-002", "ci_cd", "Incorrect or missing workflow triggers", "medium",
         "CI never runs on pull requests or on the default branch (wrong branch names, only "
         "workflow_dispatch, paths filters that exclude the code)."),
    Rule("CI-003", "ci_cd", "Ineffective CI gate", "high",
         "A non-test validation step or job (lint, format, type-check, security scan) that can "
         "never fail: continue-on-error: true, '|| true', set +e, --exit-zero. Ignored test "
         "steps are TST-003 - record both when both occur."),
    Rule("CI-004", "ci_cd", "Third-party action pinned to a mutable branch", "medium",
         "uses: owner/action@main, @master or no ref. Version tags (@v4) are acceptable."),
    Rule("CI-005", "ci_cd", "Invalid workflow file", "high",
         "Workflow YAML that does not parse or has no jobs/triggers."),
    Rule("CI-006", "ci_cd", "Validation conditionally skipped", "medium",
         "Checks skipped by conditions such as if: false, if: github.event_name == 'never', or "
         "steps commented out."),
    # -- security ------------------------------------------------------------------------------
    Rule("SEC-001", "security", "Hardcoded credential", "critical",
         "Literal API key, token, password or connection-string password in source or config "
         "(shown as [REDACTED:...] in tool output), including development/test config the "
         "application loads (dev keys still leak). Not placeholders or env lookups."),
    Rule("SEC-002", "security", "Private key committed", "critical",
         "PEM/SSH private key or keystore tracked in the repository - including self-signed or "
         "'dev' TLS keys the application loads. Exclude only obviously fake keys inside test "
         "fixture folders (tests/, fixtures/) that only tests use."),
    Rule("SEC-003", "security", "Environment or secrets file committed", "high",
         "Tracked .env, *.env, credentials.json, secrets.yml etc. containing real values."),
    Rule("SEC-004", "security", "Secret left in git history", "high",
         "A secrets file or credential that was committed and later deleted (git_file_history / "
         "search_history) - it is still retrievable."),
    Rule("SEC-005", "security", "Overly broad workflow permissions", "high",
         "permissions: write-all, or write scopes granted to jobs that only need read."),
    Rule("SEC-006", "security", "pull_request_target runs untrusted PR code", "critical",
         "pull_request_target workflow that checks out the PR head ref/sha and runs it."),
    Rule("SEC-007", "security", "Script injection from untrusted event data", "high",
         "${{ github.event.pull_request.title/body }}, issue/comment bodies or head_ref "
         "interpolated directly into run: scripts."),
    Rule("SEC-008", "security", "Secret exposed in CI logs or artefacts", "high",
         "Secrets echoed/printed, written to artefacts, or passed to untrusted actions."),
    Rule("SEC-009", "security", "Insecure security setting", "medium",
         "TLS verification disabled (verify=False), DEBUG=True or ALLOWED_HOSTS=['*'] in "
         "production settings, permissive CORS '*'."),
    # -- configuration -------------------------------------------------------------------------
    Rule("CFG-001", "configuration", "Machine-specific absolute path", "medium",
         "Hardcoded paths like C:\\Users\\name\\..., /home/user/..., /Users/name/... in code or "
         "config."),
    Rule("CFG-002", "configuration", "Committed local settings", "low",
         "Personal/local configuration tracked: .vscode/settings.json with local paths, "
         "*.local.*, settings_local.py, docker-compose.override.yml with local values."),
    Rule("CFG-003", "configuration", "No environment separation", "medium",
         "Environment-specific values (URLs, hosts, credentials, debug flags) hardcoded with no "
         "env vars or per-environment config."),
    Rule("CFG-004", "configuration", "Undocumented required configuration", "low",
         "Code reads environment variables but no .env.example/.env.sample or docs list them."),
    Rule("CFG-005", "configuration", "Tool configuration references the wrong project", "medium",
         "Config copied from another project: package names, module paths or URLs that do not "
         "match this repository."),
    # -- collaboration -------------------------------------------------------------------------
    Rule("COL-001", "collaboration", "Unsafe pull-request automation", "high",
         "Auto-merge or auto-approve workflows that act without required checks or human review."),
    Rule("COL-002", "collaboration", "No contribution guidance", "low",
         "Project accepts outside contributions (issues/PR templates, many contributors) but has "
         "no CONTRIBUTING guide. Do not flag small personal projects."),
    Rule("COL-003", "collaboration", "No review ownership or templates", "low",
         "Multi-maintainer project without CODEOWNERS or PR template. Do not flag small projects."),
    # -- build and release ---------------------------------------------------------------------
    Rule("BLD-001", "build_release", "No build validation", "medium",
         "Package/container is never built in CI before release (no build step, no "
         "docker build test)."),
    Rule("BLD-002", "build_release", "Ungated release or deployment", "high",
         "Deploy/publish runs on every push or without tests, environment protection or manual "
         "approval."),
    Rule("BLD-003", "build_release", "Long-lived publishing credentials", "medium",
         "Publishing with stored tokens/passwords where trusted publishing/OIDC is available, or "
         "secrets passed as build args."),
    Rule("BLD-004", "build_release", "Risky container build", "medium",
         "Dockerfile using an unpinned :latest base image, running as root, or copying secrets."),
]  # fmt: skip

RULES: dict[str, Rule] = {r.id: r for r in _RULES}

# Rules whose evidence is that a file *exists / is tracked* (seeing its name in a listing or in
# history is enough). Every other rule makes a claim about content, so verification requires
# that the file's content was actually shown by a tool (D30).
EXISTENCE_RULES = frozenset(
    {"HYG-004", "HYG-005", "HYG-006", "HYG-007", "SEC-002", "SEC-003", "SEC-004", "CFG-002"}
)


def is_known_or_custom(rule_id: str) -> bool:
    """Catalogue id, or a custom id in the 900 range (``SEC-901``)."""
    return rule_id in RULES or rule_id.split("-")[-1].startswith("9")


def catalogue_text() -> str:
    """Compact catalogue for the system prompt, grouped by category."""
    lines = []
    for category, prefix in CATEGORIES.items():
        lines.append(f"{category} ({prefix}):")
        for r in _RULES:
            if r.category == category:
                lines.append(f"  {r.id} [{r.severity}] {r.title} - {r.check}")
    return "\n".join(lines)
