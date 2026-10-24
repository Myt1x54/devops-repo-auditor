from __future__ import annotations

import json

from auditor.trace import OUTPUT_PREVIEW_CHARS, TraceWriter, read_trace, redact_value

SECRET = "q8Zr2LmX9vB4nT7kP1wY6cD3sF0hJ5gA8eR2uI9o"


def test_events_written_as_jsonl(tmp_path):
    path = tmp_path / "t" / "run.trace.jsonl"
    with TraceWriter(path) as trace:
        trace("start", repository="https://github.com/a/b", max_steps=3)
        trace("tool", name="read_file", args={"path": "x"}, is_error=False)
    events = read_trace(path)
    assert [e["event"] for e in events] == ["start", "tool"]
    assert events[0]["repository"] == "https://github.com/a/b" and events[0]["max_steps"] == 3
    assert {"ts", "t", "event"} <= set(events[1])
    assert trace.events == 2


def test_secrets_redacted_everywhere(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as trace:
        trace(
            "llm",
            text=f"found API_KEY = '{SECRET}'",
            tool_calls=[{"name": "search_code", "args": {"pattern": f"token: {SECRET}"}}],
            nested={"deep": [f"password={SECRET}"]},
        )
    raw = path.read_text(encoding="utf-8")
    assert SECRET not in raw and "[REDACTED:" in raw


def test_long_tool_output_is_previewed(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as trace:
        trace("tool", output="x" * (OUTPUT_PREVIEW_CHARS + 500))
    out = read_trace(path)[0]["output"]
    assert out.endswith(f"[{OUTPUT_PREVIEW_CHARS + 500} chars total]")
    assert len(out) < OUTPUT_PREVIEW_CHARS + 50


def test_unwritable_trace_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    trace = TraceWriter(blocker / "sub" / "trace.jsonl")  # parent is a file
    trace("start")  # silently disabled
    trace.close()
    assert trace.events == 0


def test_non_json_values_are_stringified(tmp_path):
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as trace:
        trace("x", obj=object(), items={"b", "a"} and ["a"])
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["obj"].startswith("<object object")


def test_redact_value_keeps_types():
    assert redact_value({"n": 1, "f": 1.5, "b": True, "none": None, "l": ["x"]}) == {
        "n": 1,
        "f": 1.5,
        "b": True,
        "none": None,
        "l": ["x"],
    }
