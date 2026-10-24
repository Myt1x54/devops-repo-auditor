"""Run the agent on the evaluation fixtures and score precision / recall (S13).

    python scripts/evaluate.py                       # all fixtures, live LLM (uses quota)
    python scripts/evaluate.py --fixtures b_subtle   # one fixture
    python scripts/evaluate.py --score-only          # re-score reports already in --out

Each fixture repository is built locally with git and audited through the same pipeline as
``agent.py`` (clone -> loop -> verification -> report + trace). Reports, traces and a summary
are written to ``runs/eval/`` (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
from auditor.env import load_env  # noqa: E402
from auditor.llm import create_llm  # noqa: E402
from auditor.trace import TraceWriter  # noqa: E402
from evaluation.fixtures import FIXTURES  # noqa: E402
from evaluation.scoring import Score, score_report, summarize  # noqa: E402

log = logging.getLogger("evaluate")


def run_fixture(fixture, out_dir: Path, max_steps: int, llm_factory=create_llm) -> Path:
    """Build the fixture repo and audit it; returns the report path."""
    report_path = out_dir / f"{fixture.name}.json"
    with tempfile.TemporaryDirectory(prefix="eval-fixture-") as tmp:
        repo_dir = fixture.build(Path(tmp))
        repo = SimpleNamespace(
            url=f"https://github.com/eval-fixtures/{fixture.name}", clone_url=repo_dir.as_uri()
        )
        with TraceWriter(out_dir / f"{fixture.name}.trace.jsonl") as trace:
            agent._audit(repo, report_path, max_steps, llm_factory(), trace)
    return report_path


def print_table(scores: list[Score]) -> None:
    print(f"\n{'fixture':12} {'TP':>3} {'FP':>3} {'FN':>3} {'neut':>4} {'prec':>6} {'recall':>6}")
    for s in scores:
        d = s.as_dict()
        print(
            f"{d['fixture']:12} {d['tp']:>3} {d['fp']:>3} {d['fn']:>3} {d['neutral']:>4} "
            f"{d['precision']:>6.2f} {d['recall']:>6.2f}"
        )
    total = summarize(scores)
    print(
        f"{'TOTAL':12} {total['tp']:>3} {total['fp']:>3} {total['fn']:>3} {'':>4} "
        f"{total['precision']:>6.2f} {total['recall']:>6.2f}"
    )
    for s in scores:
        for exp in s.fn:
            print(f"  missed  {s.fixture}/{exp.id}: {exp.note} ({'/'.join(exp.rules)})")
        for p in s.fp:
            print(
                f"  false+  {s.fixture}: {p['rule_id']} {p['file']}:{p['line']} {p['description']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures", nargs="*", default=[f.name for f in FIXTURES])
    parser.add_argument("--out", default="runs/eval")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--score-only", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stderr)  # fmt: skip
    for noisy in ("httpx", "httpx2", "google_genai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_env()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = [f for f in FIXTURES if f.name in args.fixtures]
    scores = []
    for fixture in selected:
        report_path = out_dir / f"{fixture.name}.json"
        if not args.score_only:
            log.info("=== %s: %s", fixture.name, fixture.description)
            try:
                run_fixture(fixture, out_dir, args.max_steps)
            except Exception as exc:  # keep evaluating the other fixtures
                log.error("%s failed: %s", fixture.name, exc)
                continue
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            scores.append(score_report(fixture, report))

    print_table(scores)
    summary = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixtures": [s.as_dict() for s in scores],
        "total": summarize(scores),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
