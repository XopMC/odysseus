"""Detached Goal dispatch retries are idempotent and bounded."""

import asyncio

import pytest

from src import goal_controller


class _Response:
    def __init__(self, status):
        self.status_code = status

    async def aread(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Client:
    def __init__(self, attempts, status=503, active_after=None, agent_runs=None):
        self.attempts = attempts
        self.status = status
        self.active_after = active_after
        self.agent_runs = agent_runs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        self.attempts.append(1)
        if self.active_after and len(self.attempts) >= self.active_after:
            self.agent_runs.active = True
            raise goal_controller.httpx.ReadTimeout("response lost after server accepted request")
        return _Response(self.status)


def _install_dispatch_fakes(monkeypatch, *, status=503, active_after=None):
    from src import agent_runs, subagent_delivery
    from src.chat_effect_inbox import inbox
    from src.chat_work_store import store
    attempts = []
    failures = []
    agent_runs.active = False
    monkeypatch.setattr(agent_runs, "is_active", lambda _sid: agent_runs.active)
    monkeypatch.setattr(agent_runs, "continuation_for_session", lambda _sid: {})
    monkeypatch.setattr(store, "acquire_goal_lease", lambda *_a, **_k: "lease-token")
    monkeypatch.setattr(store, "get", lambda *_a, **_k: {
        "goal": {"id": "goal-1", "attempt": 4, "status": "active", "objective": "safe fixture", "checkpoint": {}}
    })
    monkeypatch.setattr(store, "record_goal_failure", lambda *a, **k: failures.append((a, k)))
    monkeypatch.setattr(inbox, "unknown", lambda *_a: [])
    monkeypatch.setattr(subagent_delivery, "claim_pending", lambda *_a, **_k: None)

    def client_factory(*_args, **_kwargs):
        return _Client(attempts, status=status, active_after=active_after, agent_runs=agent_runs)

    monkeypatch.setattr(goal_controller.httpx, "AsyncClient", client_factory)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(goal_controller.asyncio, "sleep", no_wait)
    return attempts, failures, agent_runs


def test_dispatch_retries_transient_http_failures_ten_times_before_pausing(monkeypatch):
    attempts, failures, _agent_runs = _install_dispatch_fakes(monkeypatch)
    result = asyncio.run(goal_controller.dispatch_goal_continuation(
        "alice", "chat", reason="terminal_done",
        expected_goal_id="goal-1", expected_attempt=4,
    ))
    assert result is False
    assert len(attempts) == 10
    assert len(failures) == 1
    checkpoint = failures[0][0][3]
    assert checkpoint["failure_code"] == "http_503"
    assert checkpoint["failure_class"] == "provider_http"
    assert checkpoint["retry_exhausted"] is True
    assert failures[0][1]["failure_attempts"] == 10
    assert failures[0][1]["force_wait_user"] is False


def test_ambiguous_dispatch_response_does_not_start_a_duplicate(monkeypatch):
    attempts, failures, agent_runs = _install_dispatch_fakes(monkeypatch, active_after=1)
    result = asyncio.run(goal_controller.dispatch_goal_continuation(
        "alice", "chat", reason="terminal_done",
        expected_goal_id="goal-1", expected_attempt=4,
    ))
    assert result is True
    assert len(attempts) == 1
    assert failures == []
    assert agent_runs.active is True
