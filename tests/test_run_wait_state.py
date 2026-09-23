"""Owner-visible waiting diagnostics must follow server events, not a spinner."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import agent_runs


def _event(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def test_wait_phase_tracks_model_tool_and_user_boundaries(monkeypatch):
    run = agent_runs._Run()
    run.session_id = "wait-fixture"
    monkeypatch.setitem(agent_runs._RUNS, run.session_id, run)
    monkeypatch.setattr(agent_runs, "_persist_run_state", lambda *args, **kwargs: None)

    assert agent_runs.describe_run(run.session_id)["wait_state"]["phase"] == "model"
    agent_runs._publish(run, _event({
        "type": "model_actual", "model": "fixture-model",
        "endpoint_id": "endpoint-1", "endpoint_label": "Fixture endpoint",
    }))
    agent_runs._publish(run, _event({"type": "tool_start", "tool": "run_tests", "tool_call_id": "call-1"}))
    state = agent_runs.describe_run(run.session_id)["wait_state"]
    assert (state["phase"], state["tool"], state["tool_call_id"]) == ("tool", "run_tests", "call-1")
    assert (state["model"], state["endpoint_id"]) == ("fixture-model", "endpoint-1")

    agent_runs._publish(run, _event({"type": "tool_progress", "tool": "run_tests", "tool_call_id": "call-1"}))
    assert agent_runs.describe_run(run.session_id)["wait_state"]["phase"] == "tool"
    agent_runs._publish(run, _event({"type": "tool_output", "tool": "run_tests", "tool_call_id": "call-1", "exit_code": 0}))
    assert agent_runs.describe_run(run.session_id)["wait_state"]["phase"] == "model"
    assert agent_runs.describe_run(run.session_id)["health_metrics"]["tool_latency_count"] == 1
    agent_runs._publish(run, _event({"type": "metrics", "data": {"prefill_tps": 42.5}}))
    assert agent_runs.describe_run(run.session_id)["health_metrics"]["prefill_tps_last"] == 42.5
    agent_runs._publish(run, _event({"type": "ask_user", "data": {"kind": "tool_approval", "approval_id": "approval-1"}}))
    assert agent_runs.describe_run(run.session_id)["wait_state"]["phase"] == "approval"


@pytest.mark.asyncio
async def test_detached_run_seeds_safe_route_and_fallback_replaces_it(monkeypatch):
    from src.run_wait_state import RunWaitTracker

    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_persist_run_state", lambda *_a, **_k: None)
    monkeypatch.setattr(agent_runs, "continuation_for_session", lambda *_a: {})
    monkeypatch.delenv("ODYSSEUS_DURABLE_CHAT_REPLAY", raising=False)

    async def source():
        yield "data: [DONE]\n\n"

    run = agent_runs.start(
        "safe-route-fixture", source(), initial_model="requested-model",
        initial_endpoint_label="192.168.50.4:1234",
    )
    assert run.wait.snapshot("running")["endpoint_label"] == "192.168.50.4:1234"
    assert run.wait.snapshot("running")["model"] == "requested-model"
    await run.task

    tracker = RunWaitTracker(100)
    tracker.endpoint_label = "192.168.50.4:1234"
    tracker.observe({"type": "model_actual", "model": "fallback-model",
                     "endpoint_id": "fallback-route", "endpoint_label": "Selected route"}, now=101)
    assert tracker.snapshot("running", now=102)["endpoint_id"] == "fallback-route"
    assert tracker.snapshot("running", now=102)["endpoint_label"] is None
    tracker.observe({"type": "model_actual", "model": "fallback-model",
                     "endpoint_id": "fallback-route", "endpoint_label": "Worker GPU"}, now=103)
    assert tracker.snapshot("running", now=104)["endpoint_label"] == "Worker GPU"

    unsafe = agent_runs.start(
        "unsafe-route-fixture", source(),
        initial_endpoint_label="https://user:secret@model.example:1234/v1?key=private",
    )
    assert unsafe.wait.snapshot("running")["endpoint_label"] is None
    await unsafe.task


def test_selected_endpoint_fallback_is_redacted_and_not_actual_route():
    from src.run_wait_state import compose_wait_panel, selected_endpoint_host

    host = selected_endpoint_host("https://user:secret@model.example:1234/v1/chat?token=private")
    assert host == "model.example:1234"
    assert selected_endpoint_host("file:///etc/passwd") is None
    assert selected_endpoint_host("https://user:secret@model.example:invalid/x") is None
    panel = compose_wait_panel(
        run={"run_id": "run-1", "status": "done", "wait_state": {"phase": "user"}},
        goal={"status": "waiting_user", "wait_reason": "ask_user"},
        selected_endpoint_label=host,
    )
    assert panel["endpoint_label"] is None
    assert panel["selected_endpoint_label"] == "model.example:1234"
    assert "secret" not in json.dumps(panel)


def test_wait_state_does_not_expose_question_or_tool_output(monkeypatch):
    run = agent_runs._Run()
    run.session_id = "wait-redaction-fixture"
    monkeypatch.setitem(agent_runs._RUNS, run.session_id, run)
    monkeypatch.setattr(agent_runs, "_persist_run_state", lambda *args, **kwargs: None)
    agent_runs._publish(run, _event({
        "type": "ask_user", "data": {"kind": "choice", "question": "secret-marker"},
    }))
    agent_runs._publish(run, _event({
        "type": "model_actual", "model": {"private": "secret-marker"},
    }))
    state = agent_runs.describe_run(run.session_id)["wait_state"]
    assert state["phase"] == "user"
    assert "secret-marker" not in json.dumps(state)


def test_wait_panel_prefers_user_approval_over_finished_run_and_redacts_content():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={
            "run_id": "run-1", "status": "done", "started_at": 100,
            "durable_seq": 12, "context_revision": 3,
            "ledger_hash": "a" * 64,
            "wait_state": {"phase": "approval", "phase_since": 140,
                           "model": "model-1", "endpoint_id": "endpoint-1"},
            "progress_health": {"stalled": False},
        },
        goal={"status": "waiting_user", "attempt": 2, "lease_held": False,
              "objective": "secret-marker"},
        children=[{"child_id": "child-1", "parent_run_id": "run-1",
                   "status": "running", "model": "worker-1", "endpoint_id": "worker-endpoint",
                   "objective": "child-secret-marker"}],
        now=200,
    )
    assert panel["phase"] == "approval"
    assert panel["phase_seconds"] == 60
    assert panel["recovery_action"] == "answer"
    assert panel["checkpoint"]["durable_seq"] == 12
    assert panel["current_child"]["child_id"] == "child-1"
    assert panel["current_child"]["model"] == "worker-1"
    assert "secret-marker" not in json.dumps(panel)


def test_interrupted_run_is_reconnect_not_queued_by_active_goal():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "run-1", "status": "interrupted", "started_at": 100,
             "durable_seq": 0, "context_revision": 1},
        goal={"status": "active", "lease_held": False},
        now=200,
    )
    assert panel["phase"] == "reconnect"
    assert panel["recovery_action"] == "reconnect"
    assert panel["checkpoint"]["durable_seq"] == 0


def test_held_goal_lease_without_run_is_queue_not_model():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run=None,
        goal={"status": "active", "attempt": 3, "lease_held": True,
              "lease_expires_at": "2099-01-01T00:00:00Z"},
        now=200,
    )
    assert panel["phase"] == "queue"
    assert panel["recovery_action"] == "wait"
    assert panel["lease"]["held"] is True
    assert panel["model"] is None


def test_paused_goal_duration_starts_at_goal_transition_not_prior_model_call():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "run-1", "status": "stopped", "started_at": 100,
             "wait_state": {"phase": "model", "phase_since": 110}},
        goal={"status": "paused", "status_since": 180, "lease_held": False},
        now=200,
    )
    assert panel["phase"] == "paused"
    assert panel["phase_seconds"] == 20


def test_goal_monologue_stall_is_resumable_not_missing_question():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "run-1", "status": "done", "started_at": 100,
             "wait_state": {"phase": "model", "phase_since": 110}},
        goal={"status": "waiting_user", "wait_reason": "repeated_premature_stop",
              "status_since": 180, "lease_held": False},
        now=200,
    )
    assert panel["phase"] == "user"
    assert panel["phase_seconds"] == 20
    assert panel["recovery_action"] == "resume_goal"
    assert panel["wait_reason"] == "repeated_premature_stop"


def test_repeated_provider_failure_is_not_presented_as_unanswered_question():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "run-2", "status": "done", "started_at": 100,
             "wait_state": {"phase": "model", "phase_since": 110}},
        goal={"status": "waiting_user", "wait_reason": "provider_failure",
              "status_since": 180, "lease_held": False},
        now=200,
    )
    assert panel["recovery_action"] == "resume_goal"
    assert panel["wait_reason"] == "provider_failure"


def test_repeated_compaction_failure_offers_explicit_recovery_not_question_card():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "run-3", "status": "done", "started_at": 100,
             "wait_state": {"phase": "model", "phase_since": 110}},
        goal={"status": "waiting_user", "wait_reason": "context_compaction",
              "failure_code": "summarizer_timeout",
              "status_since": 180, "lease_held": False},
        now=200,
    )
    assert panel["recovery_action"] == "inspect_context"
    assert panel["wait_reason"] == "context_compaction"
    assert panel["failure_code"] == "summarizer_timeout"

    leaked = compose_wait_panel(
        run=None,
        goal={"status": "waiting_user", "wait_reason": "context_compaction",
              "failure_code": "private provider response token"},
        now=200,
    )
    assert leaked["failure_code"] is None
    assert "private provider response" not in json.dumps(leaked)


def test_unknown_effect_wait_requires_inbox_inspection_not_goal_resume():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "run-4", "status": "done", "started_at": 100,
             "wait_state": {"phase": "model", "phase_since": 110}},
        goal={"status": "waiting_user", "wait_reason": "unknown_side_effect",
              "status_since": 180, "lease_held": False},
        now=200,
    )
    assert panel["recovery_action"] == "inspect_effect"
    assert panel["wait_reason"] == "unknown_side_effect"


def test_reconciled_effect_wait_offers_explicit_goal_resume():
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run=None,
        goal={"status": "waiting_user", "wait_reason": "unknown_side_effect",
              "status_since": 180, "lease_held": False},
        unknown_effects=0, now=200,
    )
    assert panel["recovery_action"] == "resume_goal"
    assert panel["unknown_effect_count"] == 0


def test_verified_missing_effect_blocks_goal_until_retry_is_authorized():
    from src.run_wait_state import compose_wait_panel

    waiting = compose_wait_panel(
        run=None,
        goal={"status": "waiting_user", "wait_reason": "unknown_side_effect",
              "status_since": 180, "lease_held": False},
        unknown_effects=0, blocking_effects=1, pending_effects=1, now=200,
    )
    assert waiting["recovery_action"] == "inspect_effect"
    assert waiting["unknown_effect_count"] == 0
    assert waiting["blocking_effect_count"] == 1

    authorized = compose_wait_panel(
        run=None,
        goal={"status": "waiting_user", "wait_reason": "unknown_side_effect",
              "status_since": 180, "lease_held": False},
        unknown_effects=0, blocking_effects=0, pending_effects=1, now=200,
    )
    assert authorized["recovery_action"] == "resume_goal"
    assert authorized["pending_effect_count"] == 1


@pytest.mark.parametrize("resource", ["tool_calls", "model_rounds", "model_tokens", "model_requests", "wall_seconds", "children"])
def test_resource_budget_wait_is_visible_and_requires_explicit_resume(resource):
    from src.run_wait_state import compose_wait_panel

    panel = compose_wait_panel(
        run={"run_id": "a" * 32, "status": "done", "started_at": 100},
        goal={"status": "waiting_user", "wait_reason": "resource_budget",
              "status_since": 180, "lease_held": False,
              "budget": {"resource": resource, "used": 2, "limit": 2,
                         "run_id": "a" * 32}},
        now=200,
    )
    assert panel["recovery_action"] == "resume_goal"
    assert panel["budget"] == {"resource": resource, "used": 2,
                               "limit": 2, "run_id": "a" * 32}


def test_goal_wait_lease_metadata_is_owner_scoped_and_never_returns_token(monkeypatch, tmp_path):
    from core import database
    from src import chat_work_store

    engine = create_engine(f"sqlite:///{tmp_path / 'wait.db'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(chat_work_store, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add(database.Session(
            id="owner-chat", owner="alice", name="Fixture",
            endpoint_url="http://localhost/v1", model="fixture-model",
        ))
        db.flush()
        db.add(database.ChatGoal(
            id="goal-1", session_id="owner-chat", owner="alice",
            objective="secret-marker", status="active", attempt=2,
            progress="", checkpoint={}, failure_count=0, revision=3,
            lease_token="lease-secret-marker",
            lease_expires_at=database.utcnow_naive() + timedelta(minutes=1),
        ))

    metadata = chat_work_store.ChatWorkStore().wait_metadata("alice", "owner-chat")
    assert metadata["lease_held"] is True
    assert metadata["status"] == "active"
    assert metadata["attempt"] == 2
    assert isinstance(metadata["status_since"], float)
    assert "secret-marker" not in json.dumps(metadata)
    with pytest.raises(chat_work_store.WorkNotFound):
        chat_work_store.ChatWorkStore().wait_metadata("bob", "owner-chat")
    engine.dispose()


def test_active_child_wait_summary_is_bounded_owner_scoped_and_redacted(monkeypatch, tmp_path):
    from core import database
    from src import subagent_runtime

    engine = create_engine(f"sqlite:///{tmp_path / 'children.db'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(subagent_runtime, "SessionLocal", sessions)
    with sessions.begin() as db:
        for owner in ("alice", "bob"):
            db.add(database.Session(
                id=f"{owner}-chat", owner=owner, name="Fixture",
                endpoint_url="http://localhost/v1", model="fixture-model",
            ))
        db.flush()
        for owner in ("alice", "bob"):
            db.add(database.ChatSubagentRun(
                id=f"{owner}-child", parent_session_id=f"{owner}-chat",
                parent_run_id="run-1", owner=owner, ordinal=1,
                name="Worker", objective="secret-marker", assigned_context="private-context",
                model="worker-model", endpoint_id="endpoint-1", status="running",
            ))

    runtime = subagent_runtime.SubagentRuntime()
    rows = runtime.active_summary("alice", "alice-chat", parent_run_id="run-1", limit=4)
    assert len(rows) == 1
    assert rows[0]["child_id"] == "alice-child"
    assert "secret-marker" not in json.dumps(rows)
    assert "private-context" not in json.dumps(rows)
    assert runtime.active_summary("bob", "alice-chat", parent_run_id="run-1") == []
    with pytest.raises(ValueError):
        runtime.active_summary("alice", "alice-chat", limit=10000)
    engine.dispose()
