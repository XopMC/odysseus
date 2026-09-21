"""Headless continuations must preserve stops without replaying completed work."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_loop, agent_runs, ai_interaction, bg_monitor, chat_work_store, settings


def _setup(monkeypatch, chunks):
    captured = {"messages": [], "saves": 0}
    session = SimpleNamespace(
        id="test-session", endpoint_url="http://example.test/v1", model="test-model",
        headers=None, context_length=65536, owner="test-owner",
        get_context_messages=lambda: [{"role": "user", "content": "Finish the test task."}],
    )

    class Manager:
        def get_session(self, session_id):
            return session

        def add_message(self, session_id, message):
            captured["messages"].append(message)

        def save_sessions(self):
            captured["saves"] += 1

    async def stream(*args, **kwargs):
        captured["kwargs"] = kwargs
        for chunk in chunks:
            yield chunk if isinstance(chunk, str) else "data: " + json.dumps(chunk) + "\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: Manager())
    monkeypatch.setattr(agent_runs, "is_active", lambda session_id: False)
    monkeypatch.setattr(bg_monitor.bg_jobs, "result_text", lambda rec: "Job output")
    monkeypatch.setattr(chat_work_store.store, "get", lambda owner, session_id: {"goal": None})
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: {
        "agent_max_rounds": 80, "agent_max_tool_calls": 25,
    }.get(key, default))
    return captured, session


def test_active_goal_queues_untrusted_background_context_without_second_agent(monkeypatch):
    captured, session = _setup(monkeypatch, [{"delta": "must not run"}])
    queued = []

    class GoalStore:
        def get(self, owner, session_id):
            return {"goal": {"status": "active"}}

        def add_goal_background_context(self, owner, session_id, message, job_id):
            queued.append((owner, session_id, message, job_id))

    monkeypatch.setattr(chat_work_store, "store", GoalStore())
    handled = asyncio.run(bg_monitor._run_followup({"id": "job-goal", "session_id": session.id}))
    assert handled is True
    assert captured["messages"] == []
    assert captured["saves"] == 0
    assert len(queued) == 1
    owner, session_id, message, job_id = queued[0]
    assert (owner, session_id, job_id) == ("test-owner", "test-session", "job-goal")
    assert message["metadata"]["trusted"] is False
    assert "UNTRUSTED SOURCE DATA" in message["content"]


@pytest.mark.parametrize("event,status,reason", [
    ({"type": "rounds_exhausted", "rounds": 80}, "unfinished", "rounds_exhausted"),
    ({"type": "budget_exceeded", "limit": 25}, "unfinished", "budget_exceeded"),
    ({"type": "loop_breaker_triggered"}, "unfinished", "loop_breaker_triggered"),
    ({"type": "intent_nudge_exhausted"}, "unfinished", "intent_nudge_exhausted"),
    ({"type": "context_compaction_failed", "delta": "checkpoint unavailable"},
     "failed", "context_compaction_failed"),
    ({"type": "agent_terminal", "data": {"failed": True}}, "failed", "agent_terminal"),
    ('event: error\ndata: {"error": "provider unavailable", "status": 503}\n\n',
     "failed", "provider_error"),
])
def test_incomplete_background_followup_is_saved_once_with_explicit_state(
    monkeypatch, event, status, reason,
):
    captured, _ = _setup(monkeypatch, [{"delta": "Partial progress."}, event, "data: [DONE]\n\n"])
    handled = asyncio.run(bg_monitor._run_followup({"id": "job-test", "session_id": "test-session"}))

    # The result has been delivered once, not queued for blind original-job replay.
    assert handled is True
    assert captured["saves"] == 1
    message = captured["messages"][0]
    assert message.metadata["bg_followup"]["status"] == status
    assert message.metadata["bg_followup"]["reason"] == reason
    assert "not completed" in message.content.lower()
    assert "Partial progress." in message.content


def test_background_followup_uses_configured_bounded_agent_limits(monkeypatch):
    captured, _ = _setup(monkeypatch, [{"delta": "Finished."}, "data: [DONE]\n\n"])
    assert asyncio.run(bg_monitor._run_followup({"id": "job-test", "session_id": "test-session"}))
    assert captured["kwargs"]["max_rounds"] == 80
    assert captured["kwargs"]["max_tool_calls"] == 25
    saved = captured["messages"][0]
    assert saved.content == "Finished."
    assert saved.metadata["bg_followup"]["status"] == "completed"


def test_background_followup_keeps_approval_and_does_not_claim_completion(monkeypatch):
    approval = {"kind": "tool_approval", "approval_id": "opaque-test", "question": "Allow?"}
    captured, _ = _setup(monkeypatch, [{
        "type": "tool_output", "tool": "bash", "exit_code": None, "ask_user": approval,
    }, {"type": "ask_user", "data": approval}, "data: [DONE]\n\n"])
    assert asyncio.run(bg_monitor._run_followup({"id": "job-test", "session_id": "test-session"}))
    saved = captured["messages"][0]
    assert saved.metadata["tool_events"][0]["ask_user"] == approval
    assert saved.metadata["bg_followup"]["status"] == "waiting_user"
    assert "approval" in saved.content.lower()


def test_background_followup_without_done_is_not_reported_complete(monkeypatch):
    captured, _ = _setup(monkeypatch, [{"delta": "Partial progress."}])
    assert asyncio.run(bg_monitor._run_followup({"id": "job-test", "session_id": "test-session"}))
    assert captured["messages"][0].metadata["bg_followup"]["reason"] == "stream_incomplete"
