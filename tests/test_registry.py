from __future__ import annotations

import time

import pytest

from auditor.llm import ToolCall, ToolSpec
from auditor.registry import ArgumentError, ToolRegistry, validate_args

SPEC = ToolSpec(
    "demo",
    "demo tool",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "count": {"type": "integer", "minimum": 1},
            "flag": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["a", "b"]},
            "paths": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["path"],
    },
)


@pytest.fixture
def registry():
    reg = ToolRegistry(output_limit=200)
    reg.register(
        "echo", "Echo the args", SPEC.parameters, lambda **kw: f"args={sorted(kw.items())}"
    )
    reg.register("boom", "Always fails", {"properties": {}}, lambda: 1 / 0)
    reg.register("slow", "Sleeps", {"properties": {}}, lambda: time.sleep(2) or "done", timeout=0.2)
    reg.register("big", "Long output", {"properties": {}}, lambda: "line\n" * 500)
    reg.register(
        "leaky",
        "Prints a secret",
        {"properties": {}},
        lambda: "API_KEY = 'q8Zr2LmX9vB4nT7kP1wY6cD3'",
    )
    reg.register("err", "Returns an error string", {"properties": {}}, lambda: "ERROR: nope")
    yield reg
    reg.close()


# -- validate_args ---------------------------------------------------------------------------
def test_validate_coerces_and_ignores_unknown():
    clean, notes = validate_args(
        SPEC, {"path": "x", "count": "5", "flag": "true", "paths": "one", "junk": 1}
    )
    assert clean == {"path": "x", "count": 5, "flag": True, "paths": ["one"]}
    assert notes == ["ignored unknown argument 'junk'"]


def test_validate_null_optional_is_dropped():
    clean, _ = validate_args(SPEC, {"path": "x", "count": None})
    assert clean == {"path": "x"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "missing required"),
        ({"path": "x", "count": 0}, ">= 1"),
        ({"path": "x", "count": "many"}, "integer"),
        ({"path": "x", "flag": "maybe"}, "true or false"),
        ({"path": "x", "mode": "c"}, "one of"),
        ({"path": ["x"]}, "string"),
        ("not a dict", "JSON object"),
    ],
)
def test_validate_rejects_bad_args(args, message):
    with pytest.raises(ArgumentError, match=message):
        validate_args(SPEC, args)


# -- dispatch --------------------------------------------------------------------------------
def test_dispatch_success_and_record(registry):
    result, record = registry.dispatch(ToolCall("c1", "echo", {"path": "README.md", "count": 2}))
    assert result.call_id == "c1" and result.name == "echo" and not result.is_error
    assert result.content == "args=[('count', 2), ('path', 'README.md')]"
    assert record.name == "echo" and record.args == {"path": "README.md", "count": 2}
    assert record.seconds >= 0 and not record.truncated
    assert registry.records == [record]


def test_unknown_tool(registry):
    result, _ = registry.dispatch(ToolCall("c", "nope", {}))
    assert result.is_error and "unknown tool 'nope'" in result.content and "echo" in result.content


def test_invalid_arguments_are_reported_not_raised(registry):
    result, _ = registry.dispatch(ToolCall("c", "echo", {"count": 3}))
    assert result.is_error
    assert "missing required argument(s): path" in result.content
    assert "Parameters: path, count, flag, mode, paths" in result.content


def test_exception_in_tool_becomes_error(registry):
    result, _ = registry.dispatch(ToolCall("c", "boom", {}))
    assert result.is_error and "ZeroDivisionError" in result.content


def test_timeout(registry):
    result, record = registry.dispatch(ToolCall("c", "slow", {}))
    assert result.is_error and "timed out" in result.content and record.seconds < 1.5


def test_output_truncated_and_redacted(registry):
    big, record = registry.dispatch(ToolCall("c", "big", {}))
    assert "[truncated" in big.content and len(big.content) < 400 and record.truncated
    leaky, _ = registry.dispatch(ToolCall("c", "leaky", {}))
    assert "q8Zr2" not in leaky.content and "[REDACTED:" in leaky.content


def test_error_string_marks_is_error(registry):
    result, record = registry.dispatch(ToolCall("c", "err", {}))
    assert result.is_error and record.is_error


def test_unknown_argument_note_shown(registry):
    result, _ = registry.dispatch(ToolCall("c", "echo", {"path": "x", "colour": "red"}))
    assert "(note: ignored unknown argument 'colour')" in result.content


def test_trace_args_are_redacted(registry):
    _, record = registry.dispatch(
        ToolCall("c", "echo", {"path": "password=hunter2secret", "paths": ["token: abcd1234efgh"]})
    )
    assert "hunter2secret" not in str(record.args) and "abcd1234efgh" not in str(record.args)


def test_specs_and_duplicate_registration(registry):
    assert [s.name for s in registry.specs()][:2] == ["echo", "boom"]
    assert registry.specs()[0].parameters["type"] == "object"
    with pytest.raises(ValueError):
        registry.register("echo", "again", {}, lambda: "")
