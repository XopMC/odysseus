"""A live transport is not proof that an agent accomplished useful work."""

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src import agent_runs


def _event(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def test_run_snapshot_separates_activity_from_useful_progress(monkeypatch):
    run = agent_runs._Run()
    run.session_id = "progress-fixture"
    monkeypatch.setitem(agent_runs._RUNS, run.session_id, run)
    monkeypatch.setattr(agent_runs, "_persist_run_state", lambda *args, **kwargs: None)

    agent_runs._publish(run, ": heartbeat 1\n\n")
    agent_runs._publish(run, _event({"type": "agent_step", "round": 2}))
    agent_runs._publish(run, _event({"delta": "reasoning stream", "thinking": True}))
    agent_runs._publish(run, _event({"type": "tool_progress", "tool": "bash", "tail": "still running"}))
    for round_number in (2, 3):
        agent_runs._publish(run, _event({"type": "goal_update", "data": {
            "id": "goal-1", "status": "active", "revision": round_number,
            "progress": "Goal is still active; continuing from checkpoint.",
            "checkpoint": {"round": round_number, "response_excerpt": "same plan"},
        }}))
    health = agent_runs.describe_run(run.session_id)["progress_health"]
    assert health["revision"] == 0
    assert health["last_progress_at"] is None
    assert health["last_activity_at"] is not None

    plan = {"type": "plan_update", "data": {
        "id": "plan-1", "revision": 2, "status": "executing",
        "current_step_id": "step-1",
        "steps": [{"step_id": "step-1", "status": "in_progress"}],
    }}
    agent_runs._publish(run, _event(plan))
    agent_runs._publish(run, _event(plan))
    health = agent_runs.describe_run(run.session_id)["progress_health"]
    assert health["revision"] == 1
    assert health["last_progress_kind"] == "step"

    agent_runs._publish(run, _event({
        "type": "tool_output", "tool": "apply_patch", "exit_code": 1,
        "diff": {"text": "+bad", "added": 1, "removed": 0},
    }))
    assert agent_runs.describe_run(run.session_id)["progress_health"]["revision"] == 1

    edit = {
        "type": "tool_output", "tool": "apply_patch", "exit_code": 0,
        "diff": {"text": "+good", "added": 1, "removed": 0},
    }
    agent_runs._publish(run, _event(edit))
    agent_runs._publish(run, _event(edit))
    health = agent_runs.describe_run(run.session_id)["progress_health"]
    assert health["revision"] == 2
    assert health["last_progress_kind"] == "diff"
    assert agent_runs.event_page(run.session_id, limit=1)["progress_health"]["revision"] == 2


def test_useful_progress_requires_evidence_not_merely_tool_success():
    from src.run_progress import ProgressTracker, progress_marker

    plan = {
        "type": "plan_update", "data": {
            "id": "plan-1", "revision": 2, "current_step_id": "step-1",
            "steps": [{"step_id": "step-1", "status": "in_progress"}],
        },
    }
    title_only = {
        "type": "plan_update", "data": {
            **plan["data"], "revision": 3, "title": "Renamed plan",
        },
    }
    assert progress_marker(plan) == progress_marker(title_only)

    goal = {
        "type": "goal_update", "data": {
            "id": "goal-1", "status": "active", "revision": 4,
            "progress": "Implemented and tested a step",
            "checkpoint": {"verification": "3 passed"},
        },
    }
    assert progress_marker(goal)[0] == "evidence"
    assert progress_marker(goal) == progress_marker({
        **goal, "data": {**goal["data"], "revision": 5},
    })
    assert progress_marker({
        **goal, "data": {**goal["data"], "status": "waiting_user"},
    }) is None

    assert progress_marker({"type": "tool_output", "tool": "bash", "exit_code": 0, "output": "hello"}) is None
    assert progress_marker({"type": "context_usage", "data": {"used_tokens": 100}}) is None
    assert progress_marker({"type": "tool_output", "tool": "run_tests", "exit_code": 0, "output": "3 passed"})[0] == "verification"
    assert progress_marker({"type": "tool_output", "tool": "run_tests", "exit_code": 1, "output": "1 failed"})[0] == "verification"
    assert progress_marker({"type": "generated_image", "image_id": "artifact-1"})[0] == "artifact"
    assert progress_marker({
        "type": "tool_output", "tool": "apply_patch", "exit_code": 0,
        "diff": {"text": "+maybe", "added": "invalid"},
    }) is None

    tracker = ProgressTracker(started_at=0)
    tracker.heartbeat(now=599)
    tracker.observe({"delta": "more tokens"}, now=599)
    health = tracker.snapshot("running", now=601)
    assert health["last_heartbeat_at"] == 599
    assert health["revision"] == 0
    assert health["stalled"] is True
    assert tracker.snapshot("done", now=601)["stalled"] is False

    tracker.observe({
        "type": "tool_output", "tool": "run_tests", "exit_code": 0,
        "output": "3 passed",
    }, now=602)
    assert tracker.snapshot("running", now=603)["stalled"] is False


def test_goal_round_and_repeated_claims_do_not_reset_useful_progress_clock():
    from src.run_progress import ProgressTracker, progress_marker

    tracker = ProgressTracker(started_at=0)
    for round_number in range(1, 9):
        claim = {"type": "goal_update", "data": {
            "id": "goal-1", "status": "active", "revision": round_number,
            "progress": "Goal is still active; continuing from checkpoint.",
            "checkpoint": {"round": round_number, "response_excerpt": "same plan again"},
        }}
        assert progress_marker(claim) is None
        assert tracker.observe(claim, now=round_number * 100) is False
    assert tracker.snapshot("running", now=800)["stalled"] is True

    verified = {"type": "goal_update", "data": {
        "id": "goal-1", "status": "active", "revision": 9,
        "progress": "Tests finished", "checkpoint": {"round": 9, "verification": "3 passed"},
    }}
    same_evidence = {"type": "goal_update", "data": {
        **verified["data"], "revision": 10, "progress": "Restated completion",
        "checkpoint": {"round": 10, "verification": "3 passed"},
    }}
    assert tracker.observe(verified, now=801) is True
    assert tracker.observe(same_evidence, now=900) is False
    assert tracker.snapshot("running", now=1402)["stalled"] is True


def test_durable_terminal_run_cannot_remain_marked_stalled():
    row = SimpleNamespace(
        status="interrupted",
        continuation={"progress_health": {"revision": 2, "stalled": True}},
    )
    assert agent_runs._durable_progress_health(row) == {
        "revision": 2, "stalled": False,
    }


def test_real_sse_heartbeat_does_not_advance_useful_progress(monkeypatch):
    run = agent_runs._Run()
    run.session_id = "heartbeat-fixture"

    async def immediate_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(agent_runs.asyncio, "wait_for", immediate_timeout)

    async def observe():
        stream = agent_runs.subscribe(run.session_id, run)
        try:
            return await stream.__anext__()
        finally:
            await stream.aclose()

    frame = asyncio.run(observe())
    assert frame.startswith(": heartbeat 1")
    assert run.progress.last_heartbeat_at is not None
    assert run.progress.revision == 0
    assert run.progress.last_progress_at is None


def test_progress_revision_survives_durable_run_checkpoint(monkeypatch, tmp_path):
    from core import database

    engine = create_engine(f"sqlite:///{tmp_path / 'progress.db'}")
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    with database.SessionLocal.begin() as db:
        db.add(database.Session(
            id="durable-progress-fixture", name="Progress fixture",
            endpoint_url="http://localhost/v1", model="fixture-model",
        ))

    run = agent_runs._Run()
    run.session_id = "durable-progress-fixture"
    run.progress.observe({
        "type": "tool_output", "tool": "run_tests", "exit_code": 0,
        "output": "3 passed",
    })
    agent_runs._persist_run_state(run, durable=True)

    with database.SessionLocal() as db:
        row = db.query(database.ChatRunState).filter_by(run_id=run.run_id).one()
        health = row.continuation["progress_health"]
        wait_state = row.continuation["wait_state"]
    assert health["revision"] == 1
    assert health["last_progress_kind"] == "verification"
    assert wait_state["phase"] == "model"
    assert wait_state["checkpoint"]["durable_seq"] == -1
    assert agent_runs.describe_run(run.session_id)["progress_health"]["revision"] == 1
    engine.dispose()
