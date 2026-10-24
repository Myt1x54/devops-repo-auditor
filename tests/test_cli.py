from __future__ import annotations

import json

import pytest

import agent
from auditor.errors import InvalidInputError
from auditor.findings import CATEGORIES
from auditor.llm import FakeLLM, call, fake_response
from auditor.trace import read_trace
from auditor.workspace import RepoWorkspace, parse_github_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/requests",
        "https://github.com/psf/requests/",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/requests/tree/main",
        "  https://www.github.com/psf/requests  ",
    ],
)
def test_parse_valid_urls(url):
    ref = parse_github_url(url)
    assert (ref.owner, ref.name) == ("psf", "requests")
    assert ref.url == "https://github.com/psf/requests"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "github.com/psf/requests",
        "http://github.com/psf/requests",
        "git@github.com:psf/requests.git",
        "https://gitlab.com/psf/requests",
        "https://github.com/psf",
        "https://github.com/-bad/requests",
        "https://github.com/psf/..",
        "/local/path",
    ],
)
def test_parse_invalid_urls(url):
    with pytest.raises(InvalidInputError):
        parse_github_url(url)


def test_cli_writes_valid_report(tmp_path, local_repo_url, monkeypatch):
    # Clone a local fixture repo instead of hitting GitHub, and script the LLM.
    monkeypatch.setattr(agent, "RepoWorkspace", lambda _url: RepoWorkspace(local_repo_url))
    fake = FakeLLM(
        [
            fake_response("Looking at the README.", call("read_file", {"path": "README.md"})),
            fake_response(
                call(
                    "finish_audit",
                    {"summary": "Small demo repo.", "categories_examined": list(CATEGORIES)},
                )
            ),
        ]
    )
    monkeypatch.setattr(agent, "create_llm", lambda **_: fake)
    out = tmp_path / "report.json"
    code = agent.main(["--repo", "https://github.com/psf/requests", "--output", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"repository": "https://github.com/psf/requests", "violations": []}


def test_cli_invalid_url_exits_2(tmp_path, capsys):
    out = tmp_path / "report.json"
    code = agent.main(["--repo", "not-a-url", "--output", str(out)])
    assert code == 2
    assert not out.exists()
    assert "error:" in capsys.readouterr().err


def test_cli_clone_failure_exits_1(tmp_path, monkeypatch, capsys):
    missing = (tmp_path / "missing").as_uri()
    monkeypatch.setattr(agent, "RepoWorkspace", lambda _url: RepoWorkspace(missing))
    monkeypatch.setattr(agent, "create_llm", lambda **_: FakeLLM([]))
    out = tmp_path / "report.json"
    code = agent.main(["--repo", "https://github.com/psf/requests", "--output", str(out)])
    assert code == 1
    assert not out.exists()
    assert "Could not clone" in capsys.readouterr().err


def test_cli_missing_args_exits_2():
    with pytest.raises(SystemExit) as exc:
        agent.main([])
    assert exc.value.code == 2


def test_cli_missing_api_key_fails_before_cloning(tmp_path, monkeypatch, capsys):
    cloned = []
    monkeypatch.setattr(agent, "RepoWorkspace", lambda url: cloned.append(url))
    code = agent.main(
        ["--repo", "https://github.com/psf/requests", "--output", str(tmp_path / "r.json")]
    )
    assert code == 1 and cloned == []
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err


# -- S11: trace and failure paths ----------------------------------------------------------------
def _patch_repo(monkeypatch, local_repo_url, llm):
    monkeypatch.setattr(agent, "RepoWorkspace", lambda _url: RepoWorkspace(local_repo_url))
    monkeypatch.setattr(agent, "create_llm", lambda **_: llm)


def test_cli_writes_trace_with_all_event_types(tmp_path, local_repo_url, monkeypatch, capsys):
    fake = FakeLLM(
        [
            fake_response(
                "Checking dependencies.", call("read_file", {"path": "requirements.txt"})
            ),
            fake_response(
                call(
                    "record_finding",
                    {
                        "rule_id": "DEP-001",
                        "category": "dependency_management",
                        "severity": "medium",
                        "file": "requirements.txt",
                        "line": 1,
                        "description": "Dependency declared without a version constraint",
                        "evidence": "requirements.txt lists `requests` with no version",
                        "recommendation": "Pin or constrain the version and add a lockfile",
                    },
                )
            ),
            fake_response(
                call("finish_audit", {"summary": "done", "categories_examined": list(CATEGORIES)})
            ),
        ]
    )
    _patch_repo(monkeypatch, local_repo_url, fake)
    out = tmp_path / "report.json"
    assert agent.main(["--repo", "https://github.com/x/y", "--output", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["violations"][0]["rule_id"] == "DEP-001"
    assert report["violations"][0]["line"] == 1
    events = read_trace(tmp_path / "report.trace.jsonl")
    kinds = [e["event"] for e in events]
    assert kinds[:2] == ["start", "loop_start"] and kinds[-1] == "end"
    assert kinds.count("start") == 1
    assert {"llm", "tool"} <= set(kinds)
    end = events[-1]
    assert end["violations_reported"] == 1 and end["repository_unchanged"] is True
    assert end["stop_reason"] == "finished"
    assert capsys.readouterr().out == ""  # stdout stays clean


def test_cli_custom_trace_path(tmp_path, local_repo_url, monkeypatch):
    fake = FakeLLM(
        [
            fake_response(
                call("finish_audit", {"summary": "s", "categories_examined": list(CATEGORIES)})
            )
        ]
    )
    _patch_repo(monkeypatch, local_repo_url, fake)
    trace_path = tmp_path / "traces" / "custom.jsonl"
    args = ["--repo", "https://github.com/x/y", "--output", str(tmp_path / "r.json")]
    assert agent.main([*args, "--trace", str(trace_path)]) == 0
    assert read_trace(trace_path)[0]["event"] == "start"


def test_cli_llm_failure_mid_run(tmp_path, local_repo_url, monkeypatch, capsys):
    from auditor.llm import LLMError

    def boom(_history):
        raise LLMError("Gemini rate limit persisted after retries (429).")

    _patch_repo(monkeypatch, local_repo_url, FakeLLM([fake_response(call("list_files")), boom]))
    out = tmp_path / "report.json"
    assert agent.main(["--repo", "https://github.com/x/y", "--output", str(out)]) == 1
    assert not out.exists()  # no partial report pretending to be complete (D12)
    assert "rate limit persisted" in capsys.readouterr().err
    events = read_trace(tmp_path / "report.trace.jsonl")
    assert events[-1]["event"] == "error" and "rate limit" in events[-1]["message"]


def test_cli_interrupt_cleans_up(tmp_path, local_repo_url, monkeypatch, capsys):
    created = []
    real = RepoWorkspace

    def recording(_url):
        ws = real(local_repo_url)
        created.append(ws)
        return ws

    def interrupt(_history):
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "RepoWorkspace", recording)
    monkeypatch.setattr(agent, "create_llm", lambda **_: FakeLLM([interrupt]))
    out = tmp_path / "report.json"
    assert agent.main(["--repo", "https://github.com/x/y", "--output", str(out)]) == 1
    assert "interrupted" in capsys.readouterr().err
    assert created[0].root is None  # temp clone removed
    assert read_trace(tmp_path / "report.trace.jsonl")[-1]["message"] == "interrupted by user"


def test_cli_output_is_directory(tmp_path, capsys):
    assert agent.main(["--repo", "https://github.com/x/y", "--output", str(tmp_path)]) == 2
    assert "is a directory" in capsys.readouterr().err


def test_cli_invalid_max_steps_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUDITOR_MAX_STEPS", "lots")
    assert (
        agent.main(["--repo", "https://github.com/x/y", "--output", str(tmp_path / "r.json")]) == 1
    )
    assert "AUDITOR_MAX_STEPS must be an integer" in capsys.readouterr().err
