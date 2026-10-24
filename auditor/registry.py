"""Tool registry and dispatcher (S7, D7, D23).

The single gate between the LLM's tool calls and our Python functions:

1. look the tool up by name (unknown name -> error result listing the valid tools),
2. validate and coerce the arguments against the tool's JSON schema,
3. run the function in a worker thread with a timeout,
4. turn any exception into an ``ERROR: ...`` result (the loop never crashes on a tool),
5. redact secrets from the output, then cap its length (D7, D18),
6. return a ``ToolResult`` plus a ``DispatchRecord`` for the trace.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from auditor.llm import ToolCall, ToolResult, ToolSpec
from auditor.redact import DEFAULT_OUTPUT_LIMIT, redact, truncate

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT = 120.0  # seconds; individual tools have tighter internal limits


@dataclass
class Tool:
    spec: ToolSpec
    func: Callable[..., str]
    timeout: float = DEFAULT_TOOL_TIMEOUT
    kind: str = "inspection"  # "inspection" or "control"


@dataclass
class DispatchRecord:
    """What happened for one tool call (written to the trace in S11)."""

    call_id: str
    name: str
    args: dict[str, Any]
    is_error: bool
    seconds: float
    output_chars: int  # before truncation
    truncated: bool
    notes: list[str] = field(default_factory=list)


class ArgumentError(ValueError):
    pass


_JSON_TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _coerce(name: str, value: Any, schema: dict[str, Any]) -> Any:
    """Check/coerce one argument value against a (simple) JSON schema."""
    expected = schema.get("type")
    if expected == "integer":
        if isinstance(value, bool):
            raise ArgumentError(f"'{name}' must be an integer")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        if not isinstance(value, int):
            raise ArgumentError(f"'{name}' must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgumentError(f"'{name}' must be >= {schema['minimum']}")
    elif expected == "boolean":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ArgumentError(f"'{name}' must be true or false")
    elif expected == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ArgumentError(f"'{name}' must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ArgumentError(f"'{name}' must be one of {schema['enum']}")
    elif expected == "array":
        if isinstance(value, str):
            value = [value]  # be lenient: a single path instead of a list
        if not isinstance(value, list):
            raise ArgumentError(f"'{name}' must be an array")
        item_schema = schema.get("items", {})
        value = [_coerce(f"{name}[{i}]", v, item_schema) for i, v in enumerate(value)]
    elif expected in _JSON_TYPES and not isinstance(value, _JSON_TYPES[expected]):
        raise ArgumentError(f"'{name}' must be of type {expected}")
    return value


def validate_args(spec: ToolSpec, args: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate ``args`` against ``spec.parameters``. Returns (clean_args, notes)."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ArgumentError("arguments must be a JSON object")
    props: dict[str, Any] = spec.parameters.get("properties", {})
    required: list[str] = spec.parameters.get("required", [])
    notes: list[str] = []
    clean: dict[str, Any] = {}
    for key, value in args.items():
        if key not in props:
            notes.append(f"ignored unknown argument '{key}'")
            continue
        if value is None and key not in required:
            continue  # explicit null for an optional argument = not given
        clean[key] = _coerce(key, value, props[key])
    missing = [k for k in required if k not in clean]
    if missing:
        raise ArgumentError(f"missing required argument(s): {', '.join(missing)}")
    return clean, notes


class ToolRegistry:
    def __init__(self, output_limit: int = DEFAULT_OUTPUT_LIMIT, max_workers: int = 4) -> None:
        self._tools: dict[str, Tool] = {}
        self.output_limit = output_limit
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="tool"
        )
        self.records: list[DispatchRecord] = []

    # -- registration ------------------------------------------------------------------------
    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable[..., str],
        *,
        timeout: float = DEFAULT_TOOL_TIMEOUT,
        kind: str = "inspection",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        schema = {"type": "object", "properties": {}, **parameters}
        self._tools[name] = Tool(ToolSpec(name, description, schema), func, timeout, kind)

    def specs(self) -> list[ToolSpec]:
        """Tool specs in a stable order (keeps the prompt prefix cacheable)."""
        return [t.spec for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # -- dispatch ----------------------------------------------------------------------------
    def dispatch(self, call: ToolCall) -> tuple[ToolResult, DispatchRecord]:
        started = time.monotonic()
        notes: list[str] = []
        tool = self._tools.get(call.name)
        args: dict[str, Any] = call.input if isinstance(call.input, dict) else {}
        if tool is None:
            output = f"ERROR: unknown tool '{call.name}'. Available tools: {', '.join(self._tools)}"
        else:
            try:
                args, notes = validate_args(tool.spec, call.input)
                output = self._run(tool, args)
            except ArgumentError as exc:
                props = ", ".join(tool.spec.parameters.get("properties", {})) or "none"
                output = f"ERROR: invalid arguments for {call.name}: {exc}. Parameters: {props}"

        if not isinstance(output, str):
            output = str(output)
        if notes:
            output += "\n(note: " + "; ".join(notes) + ")"
        is_error = output.startswith("ERROR")
        raw_chars = len(output)
        limit = self.output_limit if (tool is None or tool.kind == "inspection") else 4000
        redacted = redact(output)
        content = truncate(redacted, limit)
        record = DispatchRecord(
            call_id=call.id,
            name=call.name,
            args=_redacted_args(args),
            is_error=is_error,
            seconds=round(time.monotonic() - started, 3),
            output_chars=raw_chars,
            truncated=content != redacted,
            notes=notes,
        )
        self.records.append(record)
        return ToolResult(call.id, call.name, content, is_error), record

    def _run(self, tool: Tool, args: dict[str, Any]) -> str:
        future = self._executor.submit(tool.func, **args)
        try:
            return future.result(timeout=tool.timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return f"ERROR: {tool.spec.name} timed out after {tool.timeout:.0f}s"
        except TypeError as exc:
            return f"ERROR: bad arguments for {tool.spec.name}: {exc}"
        except Exception as exc:  # a bug in a tool must not stop the audit
            log.debug("Tool %s raised", tool.spec.name, exc_info=True)
            return f"ERROR: {tool.spec.name} failed: {type(exc).__name__}: {exc}"

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _redacted_args(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as recorded in the trace: string values redacted (a search pattern could
    contain a secret the model copied)."""
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            out[key] = redact(value)
        elif isinstance(value, list):
            out[key] = [redact(v) if isinstance(v, str) else v for v in value]
        else:
            out[key] = value
    return out
