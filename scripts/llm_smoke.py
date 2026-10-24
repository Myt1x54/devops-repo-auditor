"""Developer helper: one real LLM round trip with a tool, to check the API key and model.

Uses the configured provider (AUDITOR_PROVIDER, default gemini). Offers a ``list_files`` tool,
expects the model to call it, sends a fake result back and prints the answer plus token usage.
Free on the Gemini free tier; under one cent on Anthropic.

    python scripts/llm_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auditor.env import load_env  # noqa: E402
from auditor.errors import AuditError  # noqa: E402
from auditor.llm import (  # noqa: E402
    AssistantTurn,
    ToolResult,
    ToolResultsTurn,
    ToolSpec,
    UserTurn,
    create_llm,
)

SYSTEM = "You inspect software repositories using the tools provided."
TOOLS = [
    ToolSpec(
        name="list_files",
        description="List the files in the repository being inspected.",
        parameters={"type": "object", "properties": {}},
    )
]


def main() -> int:
    load_env()
    try:
        llm = create_llm(effort="low")
        print(f"provider={llm.provider} model={llm.model}")
        history = [
            UserTurn("Use the list_files tool, then say in one sentence whether a README exists.")
        ]
        first = llm.complete(SYSTEM, history, TOOLS)
        print(f"turn 1: stop={first.stop_reason} tool_calls={[c.name for c in first.tool_calls]}")
        if not first.tool_calls:
            print("model did not call the tool:", first.text)
            return 1
        history.append(AssistantTurn(first))
        history.append(
            ToolResultsTurn(
                [
                    ToolResult(c.id, c.name, "README.md (120 B)\napp.py (40 B)")
                    for c in first.tool_calls
                ]
            )
        )
        second = llm.complete(SYSTEM, history, TOOLS)
        print(f"turn 2: stop={second.stop_reason} text={second.text!r}")
        print("usage:", llm.usage.as_dict())
        return 0
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
