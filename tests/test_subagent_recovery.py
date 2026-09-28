"""Recovery must retain work without replaying an uncertain tool action."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ChatSubagentEvent, ChatSubagentRun, ChatToolIntent, Session
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


def test_unanswered_child_question_recovers_after_one_minute(harness, monkeypatch):
    with children.SessionLocal.begin() as db:
        row = db.query(ChatSubagentRun).filter_by(id="child").one()
        row.status = "waiting_user"
        row.error = ""
        row.metrics = {"waiting_user": {"question_id": "q1", "question": "Choose a safe option"}}
        row.policy_snapshot = {"recovery_config": {"model": "worker"}}
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=61)

    resumed = []
    async def fake_resume():
        resumed.append(True)
        return 1

    monkeypatch.setattr(harness, "resume_recovering", fake_resume)
    assert asyncio.run(harness.auto_decide_expired_questions()) == 1
    assert resumed == [True]
    with children.SessionLocal() as db:
        row = db.query(ChatSubagentRun).filter_by(id="child").one()
        assert row.status == "recovering"
        assert row.metrics["question_timeout_count"] == 1
        assert row.metrics["auto_decide_question_id"] == "q1"
        assert row.guidance[-1]["source"] == "question_timeout"
        assert "not approval" in row.guidance[-1]["text"]
        assert db.query(ChatSubagentEvent).filter_by(
            child_id="child", kind="question_timeout",
        ).count() == 1
    assert asyncio.run(harness.auto_decide_expired_questions()) == 0


def test_child_question_timeout_does_not_resume_unknown_effect_or_fresh_question(harness):
    with children.SessionLocal.begin() as db:
        row = db.query(ChatSubagentRun).filter_by(id="child").one()
        row.status = "waiting_user"
        row.metrics = {"waiting_user": {"question_id": "q1"}}
        row.policy_snapshot = {"recovery_config": {"model": "worker"}}
        row.error = "Tool outcome is unknown; inspect it"
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=61)
    assert harness.expired_questions() == []

    with children.SessionLocal.begin() as db:
        row = db.query(ChatSubagentRun).filter_by(id="child").one()
        row.error = ""
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=61)
        db.add(ChatToolIntent(
            id="intent-1", owner="qa", session_id="s", run_id="child",
            tool_call_id="call-1", tool_name="bash", action_hash="sha256", status="unknown",
        ))
    candidate = harness.expired_questions()[0]
    assert harness._claim_expired_question(candidate) is False
    with children.SessionLocal() as db:
        assert db.query(ChatSubagentRun).filter_by(id="child").one().status == "waiting_user"

    with children.SessionLocal.begin() as db:
        row = db.query(ChatSubagentRun).filter_by(id="child").one()
        row.error = ""
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assert harness.expired_questions() == []


def test_timed_out_child_cannot_repeat_ask_user(harness, monkeypatch):
    with children.SessionLocal.begin() as db:
        row = db.query(ChatSubagentRun).filter_by(id="child").one()
        row.metrics = {"question_timeout_count": 1}

    requests = []
    async def stream(*args, **kwargs):
        requests.append(kwargs)
        yield 'data: {"delta":"Verified final result"}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    assert run(harness)["status"] == "completed"
    assert "ask_user" in requests[0]["disabled_tools"]
    assert "write_file" in requests[0]["disabled_tools"]


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
    result = run(harness)
    assert result["status"] == "waiting_user"
    assert result["metrics"]["failure_class"] == "tool_outcome_reconciliation_required"
    assert calls == 1
    events = harness.events("qa", "s", child_id="child")
    assert not any(item["kind"] == "transport_retry" for item in events)
    assert any(item["kind"] == "status"
               and item["payload"].get("reason") == "tool_outcome_reconciliation_required"
               for item in events)


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


def test_repetition_guard_retries_ten_times_before_stopping(harness, monkeypatch):
    calls = 0
    async def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'event: error\ndata: {"error":"repeated reasoning","status":422,"error_category":"degenerate_output"}\n\n'
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert result["status"] == "failed"
    assert calls == 11
    with children.SessionLocal() as db:
        retries = db.query(ChatSubagentEvent).filter_by(child_id="child", kind="output_retry").all()
        assert len(retries) == 10
        assert all(item.payload["reason"] == "degenerate_output" for item in retries)


def test_repetition_guard_continues_from_checkpoint_and_completes(harness, monkeypatch):
    calls = []
    async def stream(*args, **kwargs):
        calls.append((args[2], kwargs))
        if len(calls) == 1:
            yield event("context_checkpoint", messages=[{"role": "user", "content": "retained child task"}])
            yield event("agent_terminal", data={"failed": True, "failure": {
                "status": 422, "kind": "degenerate_output", "message": "Repeated output"}})
        else:
            assert calls[-1][0][1]["content"] == "retained child task"
            assert "repeated itself" in calls[-1][0][-1]["content"]
            yield "data: " + json.dumps({"delta": "Verified final result"}) + chr(10) * 2
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert result["status"] == "completed"
    assert "Verified final result" in result["result"]
    assert len(calls) == 2
    assert calls[1][1]["temperature"] < calls[0][1]["temperature"]
    with children.SessionLocal() as db:
        retry = db.query(ChatSubagentEvent).filter_by(child_id="child", kind="output_retry").one()
        assert retry.payload["attempt"] == 1 and retry.payload["retry_limit"] == 10


def test_repetition_repair_checkpoint_survives_child_process_recovery(harness):
    harness._event("child", "qa", "s", "context_checkpoint", {
        "messages": [{"role": "user", "content": "durable objective"}],
        "compactions": 2,
    })
    harness._event("child", "qa", "s", "output_retry", {
        "attempt": 3, "retry_limit": 10, "reason": "degenerate_output",
        "instruction": "Continue concisely without repeating.",
        "messages": [{"role": "user", "content": "durable objective"}],
    })
    recovered = harness._continuation_checkpoint("qa", "s", "child")
    assert recovered["messages"] == [{"role": "user", "content": "durable objective"}]
    assert recovered["output_retries"] == 3
    assert recovered["output_repair_instruction"] == "Continue concisely without repeating."


def test_progressing_child_continues_past_legacy_task_deadline(harness, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(children, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    async def stream(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            clock[0] = 7200.0
            yield 'data: {"delta":"Verified partial work"}\n\n'
            yield event("context_checkpoint", messages=[{"role": "user", "content": "retained work"}])
            yield event("rounds_exhausted", resource="model_rounds")
        else:
            yield 'data: {"delta":"Verified final result"}\n\n'
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    result = run(harness)
    assert result["status"] == "completed"
    assert len(calls) == 2
    assert calls[1][1]["content"] == "retained work"
    assert "Verified partial work" in result["result"]


def test_raw_transport_timeout_gets_ten_retries_not_task_deadline(harness, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(children, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    async def stream(*args, **kwargs):
        calls.append(1)
        clock[0] += 1800
        if len(calls) <= 10:
            raise asyncio.TimeoutError()
        yield 'data: {"delta":"Recovered final result"}\n\n'
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    assert run(harness)["status"] == "completed"
    assert len(calls) == 11


def test_policy_denial_is_not_retried_as_http_403(harness, monkeypatch):
    calls = []
    async def stream(*args, **kwargs):
        calls.append(1)
        yield event("agent_terminal", data={"failure": {
            "status": 403, "kind": "permission_denied", "message": "Denied by owner policy"}})
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    assert run(harness)["status"] == "failed"
    assert len(calls) == 1


def test_explicit_cancel_preserves_partial_result(harness, monkeypatch):
    async def scenario():
        ready = asyncio.Event()
        async def stream(*args, **kwargs):
            yield 'data: {"delta":"Verified partial result"}\n\n'
            ready.set()
            await asyncio.Event().wait()
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = asyncio.create_task(harness._run_child(
            child_id="child", owner="qa", session_id="s", endpoint_url="http://model",
            model="worker", headers={}, timeout_seconds=5, workspace=None, access_mode="full_access"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = harness.get("qa", "s", "child")
        assert row["status"] == "cancelled"
        assert row["result"] == "Verified partial result"
    asyncio.run(scenario())


def test_answer_resumes_exact_compacted_child_ledger(harness, monkeypatch):
    ledger = [{"role": "user", "content": "retained original task"},
              {"role": "assistant", "content": "", "tool_calls": [{"id": "question-1"}]},
              {"role": "tool", "tool_call_id": "question-1", "content": "Choose A or B"}]
    harness._configs["child"].update(endpoint_url="http://model", model="worker", headers={},
                                     timeout_seconds=60, workspace=None, access_mode="full_access")
    requests = []
    async def stream(*args, **kwargs):
        requests.append((args[2], kwargs))
        if len(requests) == 1:
            yield 'data: {"delta":"Prior verified work"}\n\n'
            yield event("tool_start", tool="ask_user")
            yield event("tool_output", tool="ask_user", output="Choose A or B")
            yield event("context_checkpoint", messages=ledger, compactions=3)
            yield event("ask_user", data={"question": "Choose A or B"})
        else:
            yield 'data: {"delta":"Final verified A"}\n\n'
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    async def scenario():
        await harness._run_child(child_id="child", owner="qa", session_id="s",
            endpoint_url="http://model", model="worker", headers={}, timeout_seconds=60,
            workspace=None, access_mode="full_access")
        assert harness.get("qa", "s", "child")["status"] == "waiting_user"
        assert harness._continuation_checkpoint("other", "s", "child") is None
        reply = await harness.message("qa", "s", "child", "A")
        assert reply["exit_code"] == 0
        await harness._tasks["child"]
        row = harness.get("qa", "s", "child")
        assert row["status"] == "completed"
        assert "Prior verified work" in row["result"] and "Final verified A" in row["result"]
    asyncio.run(scenario())
    assert requests[1][0][1:-1] == ledger
    assert requests[1][0][-1] == {"role": "user", "content": "A"}
    assert requests[1][1]["initial_context_compactions"] == 3
    assert "write_file" in requests[1][1]["disabled_tools"]


def test_child_resume_rejects_unsettled_effect_instead_of_replaying(harness):
    harness._event("child", "qa", "s", "context_checkpoint", {
        "messages": [{"role": "user", "content": "safe retained task"}]})
    harness._event("child", "qa", "s", "tool_start", {"tool": "write_file"})
    with pytest.raises(ValueError, match="no committed checkpoint"):
        harness._continuation_checkpoint("qa", "s", "child")


def test_prior_wait_turn_callback_cannot_evict_resumed_child(harness):
    async def scenario():
        old = asyncio.create_task(asyncio.sleep(0))
        await old
        successor = asyncio.create_task(asyncio.Event().wait())
        harness._tasks["child"] = successor
        await harness._finalize_unexpected("child", "qa", old)
        assert harness._tasks["child"] is successor
        assert "child" in harness._configs
        successor.cancel()
        await asyncio.gather(successor, return_exceptions=True)
    asyncio.run(scenario())
