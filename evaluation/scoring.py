"""Precision / recall scoring of a report against a fixture's answer key (assignment section 6).

- A prediction is a **true positive** when it matches a not-yet-matched expected violation
  (rule in the expected rules, file matching one of the expected file patterns).
- A prediction matching an *optional* (debatable) item is **neutral**: not counted either way.
- Everything else is a **false positive** - including a second prediction for an already matched
  violation (duplicates earn nothing).
- Precision = TP / (TP + FP); recall = TP / len(expected).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from evaluation.fixtures import Expected, Fixture


@dataclass
class Score:
    fixture: str
    tp: list[tuple[str, dict]] = field(default_factory=list)  # (expected id, prediction)
    fp: list[dict] = field(default_factory=list)
    neutral: list[tuple[str, dict]] = field(default_factory=list)
    fn: list[Expected] = field(default_factory=list)

    @property
    def precision(self) -> float:
        reported = len(self.tp) + len(self.fp)
        return len(self.tp) / reported if reported else 1.0

    @property
    def recall(self) -> float:
        total = len(self.tp) + len(self.fn)
        return len(self.tp) / total if total else 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "tp": len(self.tp),
            "fp": len(self.fp),
            "fn": len(self.fn),
            "neutral": len(self.neutral),
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "missed": [e.id for e in self.fn],
            "false_positives": [f"{p['rule_id']} {p['file']}:{p['line']}" for p in self.fp],
        }


def _file_matches(file: str | None, patterns: tuple[str | None, ...]) -> bool:
    for pattern in patterns:
        if pattern is None and file is None:
            return True
        if pattern is not None and file is not None and fnmatch.fnmatch(file, pattern):
            return True
    return False


def matches(prediction: dict, expected: Expected) -> bool:
    return prediction["rule_id"] in expected.rules and _file_matches(
        prediction["file"], expected.files
    )


def score_report(fixture: Fixture, report: dict) -> Score:
    score = Score(fixture.name)
    unmatched = list(fixture.expected)
    for prediction in report["violations"]:
        candidates = [e for e in unmatched if matches(prediction, e)]
        # Prefer an item whose known lines include the cited line (two items in one file).
        same_line = [e for e in candidates if prediction.get("line") in e.lines]
        hit = (same_line or candidates or [None])[0]
        if hit is not None:
            unmatched.remove(hit)
            score.tp.append((hit.id, prediction))
            continue
        opt = next((o for o in fixture.optional if matches(prediction, o)), None)
        if opt is not None:
            score.neutral.append((opt.id, prediction))
        else:
            score.fp.append(prediction)
    score.fn = unmatched
    return score


def summarize(scores: list[Score]) -> dict[str, Any]:
    tp = sum(len(s.tp) for s in scores)
    fp = sum(len(s.fp) for s in scores)
    fn = sum(len(s.fn) for s in scores)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
    }
