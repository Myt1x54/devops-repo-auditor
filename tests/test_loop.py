from __future__ import annotations

import pytest

from auditor.errors import AuditError
from auditor.findings import CATEGORIES, AuditState
from auditor.llm import (
    STOP_MALFORMED,
    STOP_MAX_TOKENS,
    STOP_REFUSAL,
    AssistantTurn,
    FakeLLM,
    ToolResultsTurn,
    UserTurn,
    call,
    fake_response,
)
from auditor.loop import AuditAgent
from auditor.prompts import CONTINUE_NUDGE, FINAL_STEP_NOTE, MALFORMED_NUDGE, MAX_TOKENS_NUDGE
from auditor.toolset import build_registry
from auditor.workspace import RepoWorkspace
from tools import RepoTools

ALL = list(CATEGORIES)
FINISH = call("finish_audit", {"summary": "done", "categories_examined": ALL})


@pytest.fixture
def env(local_repo_url):
    with RepoWorkspace(local_repo_url) as ws:
        state = AuditState()
        registry = build_registry(RepoTools(ws.root, ws.run_git), state)
        yield registry, state
        registry.close()


def make_agent(env, script, **kwargs):
    registry, state = env
    events = []
    llm = FakeLLM(script)
    agent = AuditAgent(llm, registry, state, trace=lambda e, **d: events.append((e, d)), **kwargs)
    return agent, llm, events


def last_results(history):
    return next(t for t in reversed(history) if isinstance(t, ToolResultsTurn)).results


def test_full_cycle_observe_select_execute_interpret_record(env):
    def react_to_listing(history):
        # Interpret: decide the next tool from what the previous tool returned.
        listing = last_results(history)[0].content
        assert "requirements.txt" in listing
        return fake_response(
            "requirements.txt has no version pins; reading it.",
            call("read_file", {"path": "requirements.txt"}),
        )

    def record_from_file(history):
        content = last_results(history)[0].content
        assert "1| requests" in content
        return fake_response(
            call(
                "record_finding",
                {
                    "rule_id": "DEP-001",
                    "category": "dependency_management",
                    "severity": "medium",
                    "file": "requirements.txt",
                    "line": 1,
                    "description": "Dependency declared without a version constraint",
                    "evidence": "requirements.txt line 1 lists 'requests' with no version",
                    "recommendation": "Pin or constrain versions and add a lockfile",
                },
            )
        )

    agent, llm, events = make_agent(
        env,
        [
            fake_response("Start by listing files.", call("list_files")),
            react_to_listing,
            record_from_file,
            fake_response(FINISH),
        ],
    )
    result = agent.run("https://github.com/x/y", "README.md  (7 B)\nrequirements.txt  (9 B)")
    assert result.stop_reason == "finished" and result.steps == 4
    assert [f.rule_id for f in result.state.active_findings()] == ["DEP-001"]
    # history: initial user turn, then (assistant, tool results) per step
    kinds = [type(t).__name__ for t in result.history]
    assert kinds == ["UserTurn"] + ["AssistantTurn", "ToolResultsTurn"] * 4
    assert "Audit this repository: https://github.com/x/y" in result.history[0].text
    assert "requirements.txt  (9 B)" in result.history[0].text  # seeded observation
    # every LLM call got the full, growing history and all tool specs
    assert [len(c["history"]) for c in llm.calls] == [1, 3, 5, 7]
    assert len(llm.calls[0]["tools"]) == 13
    names = [d["name"] for e, d in events if e == "tool"]
    assert names == ["list_files", "read_file", "record_finding", "finish_audit"]
    assert events[0][0] == "loop_start" and events[-1][0] == "end"
    assert len(events[0][1]["tools"]) == 13


def test_parallel_tool_calls_answered_in_one_turn(env):
    agent, _, _ = make_agent(
        env,
        [
            fake_response(call("read_file", {"path": "README.md"}), call("git_log", {})),
            fake_response(FINISH),
        ],
    )
    result = agent.run("u", "tree")
    results = result.history[2].results
    assert [r.name for r in results] == ["read_file", "git_log"]
    assert results[0].call_id != results[1].call_id


def test_tool_errors_are_returned_not_raised(env):
    agent, _, _ = make_agent(
        env,
        [
            fake_response(call("delete_everything", {}), call("read_file", {"path": "../x"})),
            fake_response(FINISH),
        ],
    )
    result = agent.run("u", "tree")
    errors = result.history[2].results
    assert all(r.is_error for r in errors)
    assert result.stop_reason == "finished"


def test_idle_model_is_nudged_then_stopped(env):
    agent, _, events = make_agent(env, [fake_response("thinking...")] * 3)
    result = agent.run("u", "tree")
    assert result.stop_reason == "idle" and result.steps == 3
    nudges = [t for t in result.history if isinstance(t, UserTurn)][1:]
    assert [n.text for n in nudges] == [CONTINUE_NUDGE, CONTINUE_NUDGE]
    assert sum(1 for e, _ in events if e == "nudge") == 2


@pytest.mark.parametrize(
    ("stop", "nudge"), [(STOP_MAX_TOKENS, MAX_TOKENS_NUDGE), (STOP_MALFORMED, MALFORMED_NUDGE)]
)
def test_truncated_or_malformed_reply_gets_specific_nudge(env, stop, nudge):
    agent, _, _ = make_agent(env, [fake_response("x", stop_reason=stop), fake_response(FINISH)])
    result = agent.run("u", "tree")
    assert result.history[2].text == nudge and result.stop_reason == "finished"


def test_repeated_refusal_is_fatal(env):
    refused = fake_response(stop_reason=STOP_REFUSAL)
    agent, _, _ = make_agent(env, [refused, refused])
    with pytest.raises(AuditError, match="declined"):
        agent.run("u", "tree")


def test_step_budget_warns_then_stops(env):
    agent, llm, _ = make_agent(env, [fake_response(call("git_log", {}))] * 6, max_steps=6)
    result = agent.run("u", "tree")
    assert result.stop_reason == "step_budget" and result.steps == 6 and len(llm.calls) == 6
    contents = [t.results[-1].content for t in result.history if isinstance(t, ToolResultsTurn)]
    assert "3 step(s) left" in contents[2]
    assert "2 step(s) left" in contents[3]
    assert contents[4].endswith(FINAL_STEP_NOTE)
    assert "[harness]" not in contents[0]


def test_time_budget_gives_one_final_turn_with_control_tools_only(env):
    now = [0.0]

    def clock():
        now[0] += 100.0
        return now[0]

    script = [
        fake_response(call("git_log", {})),
        fake_response(call("git_log", {})),  # time runs out after this step's tools
        fake_response(  # the one final turn: only control tools are executed
            call("read_file", {"path": "README.md"}),
            call(
                "record_finding",
                {
                    "rule_id": "TST-001",
                    "category": "testing",
                    "severity": "high",
                    "description": "No automated tests in the repository",
                    "evidence": "list_files shows no test files or test directories",
                    "recommendation": "Add a tests/ suite and run it in CI",
                },
            ),
        ),
    ]
    agent, _, _ = make_agent(env, script, time_budget=150.0, clock=clock)
    result = agent.run("u", "tree")
    assert result.stop_reason == "time_budget" and result.steps == 3
    assert result.history[4].results[-1].content.endswith(FINAL_STEP_NOTE)
    final = result.history[-1].results
    assert final[0].is_error and "budget exhausted" in final[0].content
    assert not final[1].is_error and len(result.state.active_findings()) == 1


def test_coverage_reminder_then_finish(env):
    partial = call("finish_audit", {"summary": "s", "categories_examined": ["security"]})
    agent, _, _ = make_agent(
        env,
        [
            fake_response(partial),
            fake_response(call("inspect_workflows", {})),
            fake_response(FINISH),
        ],
    )
    result = agent.run("u", "tree")
    assert "Not finished yet" in result.history[2].results[0].content
    assert result.stop_reason == "finished" and result.steps == 3


def test_history_is_append_only(env):
    seen = []

    def capture(history):
        seen.append([id(t) for t in history])
        return fake_response(FINISH) if len(seen) == 3 else fake_response(call("git_log", {}))

    agent, _, _ = make_agent(env, [capture, capture, capture])
    agent.run("u", "tree")
    # each turn's history starts with exactly the previous turn's history (nothing edited)
    assert seen[1][: len(seen[0])] == seen[0] and seen[2][: len(seen[1])] == seen[1]
    assert isinstance(agent.history[1], AssistantTurn)


def test_invalid_max_steps():
    with pytest.raises(AuditError):
        AuditAgent(FakeLLM([]), None, AuditState(), max_steps=0)
