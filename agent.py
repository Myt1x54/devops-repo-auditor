"""CLI entry point for the DevOps repository auditing agent.

Usage:
    python agent.py --repo <github_repository_url> --output report.json
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from auditor.env import load_env
from auditor.errors import EXIT_FATAL, EXIT_OK, AuditError, InvalidInputError
from auditor.findings import AuditState
from auditor.llm import create_llm
from auditor.loop import DEFAULT_MAX_STEPS, AuditAgent
from auditor.report import build_report, write_report
from auditor.toolset import build_registry
from auditor.trace import TraceWriter
from auditor.verify import verify_findings
from auditor.workspace import RepoWorkspace, parse_github_url
from tools import RepoTools

log = logging.getLogger("auditor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Audit a public GitHub repository for DevOps best-practice violations.",
    )
    parser.add_argument("--repo", required=True, help="Public GitHub repository URL.")
    parser.add_argument("--output", required=True, help="Path of the JSON report to write.")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"Maximum agent steps / LLM turns (default {DEFAULT_MAX_STEPS} or AUDITOR_MAX_STEPS).",
    )
    parser.add_argument("--model", default=None, help="LLM model id (default AUDITOR_MODEL).")
    parser.add_argument(
        "--trace",
        default=None,
        help="Path for the JSONL tool-call trace (default <output>.trace.jsonl).",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging to stderr.")
    return parser


def resolve_max_steps(cli_value: int | None) -> int:
    if cli_value is not None:
        value = cli_value
    else:
        env = os.environ.get("AUDITOR_MAX_STEPS", "").strip()
        try:
            value = int(env) if env else DEFAULT_MAX_STEPS
        except ValueError:
            raise AuditError(f"AUDITOR_MAX_STEPS must be an integer, got {env!r}.") from None
    if value < 1:
        raise AuditError("--max-steps must be at least 1.")
    return value


def resolve_output(path_arg: str) -> Path:
    output = Path(path_arg)
    if output.is_dir():
        raise InvalidInputError(f"--output must be a file path, but {output} is a directory.")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvalidInputError(
            f"Cannot create the output directory {output.parent}: {exc}"
        ) from None
    return output


def default_trace_path(output: Path) -> Path:
    """report.json -> report.trace.jsonl, next to the report."""
    return output.with_name(output.stem + ".trace.jsonl")


def run(args: argparse.Namespace) -> int:
    # Validate everything cheap before any network call (D12).
    repo = parse_github_url(args.repo)
    output = resolve_output(args.output)
    max_steps = resolve_max_steps(args.max_steps)
    llm = create_llm(model=args.model)  # fail fast on a missing key, before cloning
    trace_path = Path(args.trace) if args.trace else default_trace_path(output)
    log.info("Auditing %s with %s/%s (max %d steps)", repo.url, llm.provider, llm.model, max_steps)

    with TraceWriter(trace_path) as trace:
        try:
            return _audit(repo, output, max_steps, llm, trace)
        except KeyboardInterrupt:
            trace("error", message="interrupted by user")
            raise
        except AuditError as exc:
            trace("error", message=str(exc))
            raise
        except Exception as exc:
            trace("error", message=f"unexpected failure: {type(exc).__name__}: {exc}")
            raise


def _audit(repo: Any, output: Path, max_steps: int, llm: Any, trace: TraceWriter) -> int:
    with RepoWorkspace(repo.clone_url) as workspace:
        log.debug("Workspace root: %s", workspace.root)
        trace(
            "start",
            repository=repo.url,
            provider=llm.provider,
            model=llm.model,
            max_steps=max_steps,
            head=workspace.head,
        )
        repo_tools = RepoTools(workspace.root, workspace.run_git)
        state = AuditState()
        registry = build_registry(repo_tools, state)
        try:
            agent = AuditAgent(llm, registry, state, max_steps=max_steps, trace=trace)
            result = agent.run(repo.url)  # the loop makes the first observation itself
            unchanged = repo_tools.git_status().startswith("Working tree clean")
            if not unchanged:
                log.warning("Inspected clone was modified during the audit")
            # Report: verify each finding against what the tools actually showed (D9, D26).
            findings, notes = verify_findings(state, repo_tools._read_text)
        finally:
            registry.close()

    for switch in getattr(llm, "model_switches", []):
        trace("model_switch", change=switch)
    for note in notes:
        log.info("verify %s %s: %s (%s)", note.finding_id, note.rule_id, note.action, note.reason)
        trace("verify", **note.__dict__)

    report = build_report(repo.url, findings)
    try:
        write_report(output, report)
    except OSError as exc:
        raise AuditError(f"Could not write the report to {output}: {exc}") from None

    usage = llm.usage
    trace(
        "end",
        stop_reason=result.stop_reason,
        steps=result.steps,
        tool_calls=state.tool_calls,
        findings_recorded=len(state.findings),
        findings_retracted=sum(f.retracted for f in state.findings),
        violations_reported=len(report["violations"]),
        repository_unchanged=unchanged,
        seconds=round(result.elapsed, 1),
        usage=usage.as_dict(),
    )
    log.info(
        "Done: %d violation(s), %d step(s), %d tool call(s), stop=%s, %.0fs, "
        "LLM tokens in/out %d/%d",
        len(report["violations"]),
        result.steps,
        state.tool_calls,
        result.stop_reason,
        result.elapsed,
        usage.input_tokens,
        usage.output_tokens,
    )
    log.info("Report written to %s (trace: %s)", output, trace.path)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # exits with code 2 on usage errors

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if not args.verbose:  # one line per HTTP request is noise at INFO level
        for noisy in ("httpx", "httpx2", "google_genai", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    load_env()

    try:
        return run(args)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return EXIT_FATAL
    except Exception as exc:  # last-resort guard: never crash with a raw traceback
        log.debug("Unexpected error", exc_info=True)
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
