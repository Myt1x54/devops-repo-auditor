"""JSONL trace of an audit run (D13, D28).

One JSON object per line: ``{"ts", "t", "event", ...data}``. Events written by the agent:

    start        repository, provider, model, budgets, cloned HEAD
    llm          step, model, stop reason, visible text, reasoning summary, tool calls, tokens
    tool         step, tool name, redacted arguments, error flag, duration, output preview
    nudge        why the harness added a reminder
    verify       evidence-verification changes (dropped / line corrected / merged)
    model_switch provider model changes (overload or daily quota)
    end          stop reason, steps, findings, token totals, repository unchanged?
    error        fatal error message (the run exits non-zero)

Every string is passed through ``redact()`` before it is written, so the trace never contains
secret values (D11). A trace that cannot be written never stops the audit.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auditor.redact import redact

log = logging.getLogger(__name__)

OUTPUT_PREVIEW_CHARS = 2000  # tool outputs are long; the trace keeps a preview


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists/tuples."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


class TraceWriter:
    """Callable trace sink: ``trace("tool", name=..., ...)``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._started = time.monotonic()
        self._fh = None
        self.events = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            log.warning("Trace disabled: cannot write %s (%s)", self.path, exc)

    def __call__(self, event: str, **data: Any) -> None:
        if self._fh is None:
            return
        if "output" in data and isinstance(data["output"], str):
            out = data["output"]
            if len(out) > OUTPUT_PREVIEW_CHARS:
                data["output"] = out[:OUTPUT_PREVIEW_CHARS] + f"...[{len(out)} chars total]"
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "t": round(time.monotonic() - self._started, 2),
            "event": event,
            **redact_value(data),
        }
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
            self.events += 1
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Trace disabled after write error: %s", exc)
            self.close()

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    """Load a trace file (used by tests and the evaluation script)."""
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
