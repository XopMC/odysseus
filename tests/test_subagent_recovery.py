"""Recovery must retain work without replaying an uncertain tool action."""
import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ChatSubagentRun, Session
from src import subagent_runtime as children


@pytest.fixture
def harness(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(children, "SessionLocal", factory)
    db = factory()
    db.add(Session(id="s", name="QA", endpoint_url="http://model", model="parent", owner="qa"))
    db.commit()
    db.add(ChatSubagentRun(id="child", parent_session_id="s", parent_run_id="parent",
                          owner="qa", ordinal=1, name="worker", objective="safe QA",
                          assigned_context="", model="worker", status="queued"))
    db.commit(); db.close()
    runtime = children.SubagentRuntime()
    runtime._configs["child"] = {"disabled_tools": {"write_file"}}
    monkeypatch.setattr(children, "_retry_delay", lambda attempt: 0, raising=False)
    return runtime


def event(kind, **data):
    return "data: " + json.dumps({"type": kind, **data}) + "\n\n"


def run(runtime):
    asyncio.run(runtime._run_child(child_id="child", owner="qa", session_id="s",
                                  endpoint_url="http://model", model="worker", headers={},
                                  timeout_seconds=60, workspace=None, access_mode="full_access"))
    return runtime.get("qa", "s", "child")


@pytest.mark.parametrize("status", [400, 403, 500, 503])
def test_ten_retries_preserve_checkpoint_after_completed_tool(harness, monkeypatch, status):
    calls = []
    ledger = [{"role": "user", "content": "QA"},
              {"role": "assistant", "content": "", "tool_calls": [
                  {"id": "call-1", "type": "function", "function": {"name": "python", "arguments": "{}"}}]},
              {"role": "tool", "tool_call_id": "call-1", "content": "verified 42"}]

    async def stream(*args, **kwargs):
        calls.append((args[2], kwargs))
        if len(calls) == 1:
            yield 'data: {"delta":"Partial verified work"}\n\n'
            yield event("tool_start", tool="python")
            yield event("tool_output", tool="python", output="verified 42")
            yield event("context_checkpoint", messages=ledger, compactions=1, ledger_hash="qa")
        if len(calls) <= 10:
            yield event("agent_terminal", data={"failed": True, "failure": {
                "status": status, "message": "Model request failed"}})
        else:
            yield 'data: {"delta":"Final verified result"}\n\n'
            yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert result["status"] == "completed"
    assert len(calls) == 11
    assert "Partial verified work" in result["result"]
    assert "Final verified result" in result["result"]
    for messages, options in calls[1:]:
        assert messages[1:] == ledger
        assert options["initial_context_compactions"] == 1
        assert options["access_mode"] == "full_access"
        assert "write_file" in options["disabled_tools"]
    assert len({options["child_attempt_id"] for _, options in calls}) == 11


def test_exhausted_provider_retries_keep_partial_work(harness, monkeypatch):
    calls = 0

    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta":"Partial result remains available"}\n\n'
        yield 'event: error\ndata: {"error":"HTTP 500","status":500}\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert calls == 11
    assert result["status"] == "failed"
    assert "Partial result remains available" in result["result"]
    assert result["metrics"]["provider_retries"] == 10


def test_unknown_effect_terminal_without_failed_flag_is_not_completed(harness, monkeypatch):
    calls = 0

    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'data: {"delta":"Some progress"}\n\n'
        yield event("agent_terminal", data={"failure": {
            "kind": "unknown_side_effect", "message": "Inspect unknown effect"}})

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert calls == 1
    assert result["status"] == "failed"
    assert result["result"] == "Some progress"


def test_uncheckpointed_tool_is_not_replayed(harness, monkeypatch):
    calls = 0

    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield event("tool_start", tool="bash")
        yield 'event: error\ndata: {"error":"Read timeout","status":504}\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    assert run(harness)["status"] == "failed"
    assert calls == 1


def test_round_slice_continues_from_checkpoint_not_partial_prose(harness, monkeypatch):
    calls = 0

    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta":"Still working"}\n\n'
            yield event("context_checkpoint", messages=[{"role": "user", "content": "retained QA"}])
            yield event("rounds_exhausted", resource="model_rounds")
        else:
            assert args[2][1]["content"] == "retained QA"
            yield 'data: {"delta":"Verified completion"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert calls == 2
    assert result["status"] == "completed"


def test_recovery_context_is_bounded_and_owner_scoped(harness):
    harness._event("child", "qa", "s", "context_checkpoint", {
        "ledger_hash": "abc", "messages": [{"role": "tool", "content": "x" * 10000}] * 50,
    })
    context = harness.recovery_context("qa", "s", "child")
    assert context["message_count"] == 50
    assert context["inspection_only"] and context["untrusted"]
    assert sum(len(m["message_json"]) for m in context["tail"]) <= 12000
    assert all(m["truncated"] for m in context["tail"])
    assert harness.recovery_context("other", "s", "child") == {}
    assert harness.recovery_context("qa", "other", "child") == {}


def test_repetition_validation_failure_is_not_ten_provider_retries(harness, monkeypatch):
    calls = 0
    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'event: error\ndata: {"error":"repeated reasoning","status":422,"error_category":"degenerate_output"}\n\n'
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert result["status"] == "failed"
    assert calls == 1  # agent_loop owns the bounded, changed-prompt repair
