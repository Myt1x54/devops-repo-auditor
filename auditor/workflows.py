"""Structured summary of GitHub Actions workflow files for the ``inspect_workflows`` tool.

The summary lists *facts* (triggers, permissions, actions and how they are pinned, what each
step does) plus neutral "signals" worth a closer look. It does not decide what is a violation:
the LLM interprets the summary and confirms details with ``read_file`` (D5, D19).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^v?\d+(\.\d+)*([.-][0-9A-Za-z.-]+)?$")
_UNTRUSTED_CONTEXT = re.compile(
    r"\$\{\{\s*github\.(?:head_ref|event\.(?:issue|pull_request|comment|review|review_comment|"
    r"discussion|head_commit|commits|pages)[^}]*?(?:title|body|ref|label|message|name|email|"
    r"page_name)[^}]*)\}\}"
)
_SECRET_REF = re.compile(r"\$\{\{\s*secrets\.([A-Za-z0-9_]+)\s*\}\}")
_ERROR_SUPPRESSION = [
    (re.compile(r"\|\|\s*(true|:|exit 0)\b"), "'|| true' style error suppression"),
    (re.compile(r"(^|\s)set \+e\b"), "'set +e' disables exit-on-error"),
    (re.compile(r"--exit-zero\b"), "'--exit-zero' makes the linter never fail"),
    (re.compile(r"(^|[\s;&])exit 0\s*$", re.MULTILINE), "explicit 'exit 0'"),
]
_STEP_KINDS = [
    (
        "test",
        re.compile(
            r"\b(pytest|py\.test|unittest|tox|nox|npm (run )?test|yarn test|pnpm test|go test|"
            r"cargo test|mvn\b.*\b(test|verify)|gradlew?\b.*\btest|make test|jest|vitest|"
            r"phpunit|rspec|ctest|dotnet test)\b"
        ),
    ),
    (
        "lint",
        re.compile(
            r"\b(ruff|flake8|pylint|black|isort|mypy|pyright|pre-commit|eslint|prettier|"
            r"golangci-lint|clippy|rubocop|shellcheck|hadolint|yamllint|actionlint)\b"
        ),
    ),
    (
        "build",
        re.compile(
            r"\b(python -m build|pip wheel|poetry build|uv build|npm run build|yarn build|"
            r"docker build|docker buildx|go build|cargo build|mvn\b.*\bpackage|gradlew?\b.*\bbuild|"
            r"make( build)?$)"
        ),
    ),
    (
        "deploy/publish",
        re.compile(
            r"\b(deploy|twine upload|poetry publish|uv publish|npm publish|docker push|"
            r"gh release|kubectl|helm (upgrade|install)|terraform apply|aws s3|gcloud|az webapp|"
            r"heroku|flyctl|vercel|netlify|gh-action-pypi-publish|ghcr\.io)\b"
        ),
    ),
]


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _line_of(lines: list[str], needle: str, start: int = 0) -> int | None:
    """1-based line number of the first line at/after ``start`` containing ``needle``."""
    needle = needle.strip()
    if not needle:
        return None
    # Folded/multi-line YAML scalars may not appear verbatim: retry with shorter prefixes.
    for candidate in dict.fromkeys((needle, needle[:40].strip(), needle[:20].strip())):
        for i in range(start, len(lines)):
            if candidate and candidate in lines[i]:
                return i + 1
    return None


def pin_status(uses: str) -> str:
    """How an action reference is pinned: full SHA, version tag, or a mutable ref."""
    if uses.startswith("./") or uses.startswith("../"):
        return "local action"
    if uses.startswith("docker://"):
        return "docker image (digest-pinned)" if "@sha256:" in uses else "docker image (tag)"
    if "@" not in uses:
        return "NO REF (unpinned)"
    ref = uses.rsplit("@", 1)[1]
    if _SHA_RE.match(ref):
        return "pinned to full commit SHA"
    if _TAG_RE.match(ref):
        return f"version tag '{ref}' (mutable)"
    return f"branch/other ref '{ref}' (mutable)"


def _describe_triggers(on: Any) -> tuple[list[str], set[str]]:
    names: set[str] = set()
    out: list[str] = []
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        names = {str(t) for t in on}
        out = sorted(names)
    elif isinstance(on, dict):
        for name, cfg in on.items():
            name = str(name)
            names.add(name)
            if isinstance(cfg, dict) and cfg:
                details = ", ".join(f"{k}={_short(v, 60)}" for k, v in cfg.items())
                out.append(f"{name}({details})")
            else:
                out.append(name)
    return out, names


def _describe_permissions(perms: Any) -> str:
    if perms is None:
        return "not set (falls back to repository/org default token permissions)"
    if isinstance(perms, str):
        return perms
    if isinstance(perms, dict):
        if not perms:
            return "{} (no permissions)"
        return ", ".join(f"{k}: {v}" for k, v in perms.items())
    return _short(perms)


def _has_write(perms: Any) -> bool:
    if isinstance(perms, str):
        return perms == "write-all"
    if isinstance(perms, dict):
        return any(str(v) == "write" for v in perms.values())
    return False


def analyze_workflow(rel: str, text: str) -> str:
    """Summarise one workflow file."""
    lines = text.splitlines()
    out = [f"=== {rel} ({len(lines)} lines)"]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        out.append(f"  INVALID YAML{where}: {_short(str(exc).splitlines()[0], 200)}")
        out.append("  (GitHub will refuse to run this workflow)")
        return "\n".join(out)
    if not isinstance(data, dict):
        out.append("  Not a mapping - not a valid workflow file.")
        return "\n".join(out)

    signals: list[str] = []
    # YAML 1.1 parses the key `on` as boolean True.
    on = data.get("on", data.get(True))
    triggers, trigger_names = _describe_triggers(on)
    out.append(f"  name: {_short(data.get('name', '(none)'))}")
    out.append(f"  triggers: {', '.join(triggers) if triggers else 'NONE (workflow never runs)'}")
    if "pull_request_target" in trigger_names:
        signals.append(
            f"line {_line_of(lines, 'pull_request_target')}: pull_request_target trigger "
            "(runs with base-repo secrets/write token on fork PRs)"
        )
    if "workflow_run" in trigger_names:
        signals.append("workflow_run trigger (privileged follow-up workflow)")

    top_perms = data.get("permissions")
    out.append(f"  permissions (workflow level): {_describe_permissions(top_perms)}")
    if _has_write(top_perms):
        signals.append(
            f"line {_line_of(lines, 'permissions')}: workflow-level write permissions "
            f"({_describe_permissions(top_perms)})"
        )

    if isinstance(data.get("env"), dict):
        out.append(f"  env (workflow level): {', '.join(map(str, data['env']))}")
    if "concurrency" in data:
        out.append(f"  concurrency: {_short(data['concurrency'], 80)}")

    secrets_used: set[str] = set(_SECRET_REF.findall(text))
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        out.append("  jobs: NONE")
    else:
        for job_id, job in jobs.items():
            out.extend(_describe_job(str(job_id), job, lines, trigger_names, signals))

    if secrets_used:
        out.append(f"  secrets referenced: {', '.join(sorted(secrets_used))}")
    if signals:
        out.append("  signals to verify (facts, not verdicts):")
        out.extend(f"    - {s}" for s in signals)
    return "\n".join(out)


def _describe_job(
    job_id: str, job: Any, lines: list[str], triggers: set[str], signals: list[str]
) -> list[str]:
    out: list[str] = []
    if not isinstance(job, dict):
        return [f"  job '{job_id}': invalid definition"]
    job_line = _line_of(lines, f"{job_id}:") or 1
    header = f"  job '{job_id}' (line {job_line})"
    attrs = []
    for key in ("runs-on", "needs", "if", "environment", "timeout-minutes"):
        if key in job:
            attrs.append(f"{key}={_short(job[key], 60)}")
    if job.get("continue-on-error") is True:
        attrs.append("continue-on-error=true")
        signals.append(f"job '{job_id}': continue-on-error: true (failures don't fail the run)")
    if "permissions" in job:
        attrs.append(f"permissions=[{_describe_permissions(job['permissions'])}]")
        if _has_write(job["permissions"]):
            signals.append(
                f"job '{job_id}': write permissions ({_describe_permissions(job['permissions'])})"
            )
    if "uses" in job:  # reusable workflow call
        attrs.append(f"calls reusable workflow {job['uses']} [{pin_status(str(job['uses']))}]")
        if job.get("secrets") == "inherit":
            attrs.append("secrets=inherit")
    out.append(header + (": " + "; ".join(attrs) if attrs else ""))

    kinds_in_job: set[str] = set()
    for idx, step in enumerate(_as_list(job.get("steps")), start=1):
        if not isinstance(step, dict):
            continue
        label = step.get("name") or ""
        parts = []
        text_for_kind = ""
        if "uses" in step:
            uses = str(step["uses"])
            ln = _line_of(lines, uses, job_line - 1)
            pin = pin_status(uses)
            parts.append(f"uses {uses} [{pin}] (line {ln})")
            if pin.startswith(("branch/other ref", "NO REF")):
                signals.append(f"job '{job_id}' step {idx} (line {ln}): action {uses} [{pin}]")
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            if uses.startswith("actions/checkout"):
                ref = str(with_.get("ref", ""))
                if with_.get("persist-credentials") is False:
                    parts.append("persist-credentials=false")
                if ref:
                    parts.append(f"ref={ref}")
                if "pull_request_target" in triggers and re.search(
                    r"pull_request\.head|head_ref|refs/pull", ref
                ):
                    signals.append(
                        f"job '{job_id}' step {idx} (line {ln}): checks out the PR head under "
                        "pull_request_target (untrusted code with privileged token)"
                    )
            if with_:
                parts.append("with: " + ", ".join(map(str, with_)))
            text_for_kind = uses
        if "run" in step:
            run = str(step["run"])
            first = next((ln for ln in run.splitlines() if ln.strip()), "")
            ln = _line_of(lines, first, job_line - 1)
            parts.append(f"run (line {ln}): {_short(run, 160)}")
            for pattern, what in _ERROR_SUPPRESSION:
                if pattern.search(run):
                    signals.append(f"job '{job_id}' step {idx} (line {ln}): {what}")
            for m in _UNTRUSTED_CONTEXT.finditer(run):
                signals.append(
                    f"job '{job_id}' step {idx} (line {ln}): untrusted event data "
                    f"'{_short(m.group(0), 80)}' interpolated directly into a shell script"
                )
            text_for_kind = run
        if step.get("continue-on-error") is True:
            parts.append("continue-on-error=true")
            signals.append(f"job '{job_id}' step {idx} '{label}': continue-on-error: true")
        if "if" in step:
            parts.append(f"if={_short(step['if'], 60)}")

        kinds = [k for k, rx in _STEP_KINDS if rx.search(f"{label}\n{text_for_kind}".lower())]
        kinds_in_job.update(kinds)
        kind_txt = f" <{'/'.join(kinds)}>" if kinds else ""
        out.append(
            f"    step {idx}{' ' + repr(label) if label else ''}{kind_txt}: " + "; ".join(parts)
        )
    if kinds_in_job:
        out.append(f"    job runs: {', '.join(sorted(kinds_in_job))}")
    return out


OTHER_CI_FILES = [
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    ".travis.yml",
    "bitbucket-pipelines.yml",
    ".drone.yml",
    "buildkite.yml",
    ".buildkite/pipeline.yml",
    "appveyor.yml",
    "cloudbuild.yaml",
]
