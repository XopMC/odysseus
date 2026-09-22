"""Agent control events must reach the HTTP client and detached-run snapshot.

The producer is isolated at stream_agent_loop; the real chat route, SSE filter,
detached manager and session-owner check all run. Reuse the existing foreground
route fixture rather than inventing a second routing setup.
"""

import json
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import database
from core.database import Base, ChatMessage as DbChatMessage, ChatRunState, Session as DbSession
from routes import chat_routes, session_routes
from src import agent_runs
from tests.test_foreground_model_routing import _chat_stream_endpoint


def _events():
    snapshot = {
        "used_tokens": 3200, "prompt_tokens": 3200, "context_length": 4096,
        "source": "estimated", "model": "selected-model", "round": 2,
        "auto_compact_threshold": 70.0, "compactions": 0,
    }
    return [
        {"type": "tool_inventory", "data": {"revision": "fixture-rev", "tools": ["update_plan_step"]}},
        {"type": "context_usage", "data": snapshot},
        {"type": "compacted", "context_length": 4096, "round": 2},
        {"type": "tool_retry_blocked", "tool": "web_fetch", "round": 2},
        {"type": "context_usage", "data": {
            **snapshot, "used_tokens": 1234, "prompt_tokens": 1234,
            "source": "backend", "compactions": 1,
        }},
        {"delta": "Completed without repeating the failed fetch."},
    ]


@pytest.fixture
def stream_client(monkeypatch):
    captured = {}
    events = _events()
    endpoint = _chat_stream_endpoint(
        monkeypatch, "agent", captured,
        agent_chunks=["data: " + json.dumps(event) + "\n\n" for event in events] + ["data: [DONE]\n\n"],
    )
    # Restore the real owner gate after the shared fixture's general-purpose
    # stub. Only its database is replaced, with an actual isolated SQLite DB.
    monkeypatch.setattr(chat_routes, "_verify_session_owner", session_routes._verify_session_owner)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        DbSession.__table__, DbChatMessage.__table__, ChatRunState.__table__,
    ])
    db_factory = sessionmaker(bind=engine)
    captured["db_factory"] = db_factory
    with db_factory() as db:
        db.add(DbSession(id="session-1", name="Runtime events", endpoint_url="https://selected.example/v1", model="selected-model", owner="alice"))
        db.commit()
    monkeypatch.setattr(session_routes, "SessionLocal", db_factory)
    monkeypatch.setattr(database, "SessionLocal", db_factory)
    monkeypatch.setattr(chat_routes, "SessionLocal", db_factory)
    monkeypatch.setattr(agent_runs, "_RUNS", {})

    app = FastAPI()

    @app.middleware("http")
    async def authenticated_user(request, call_next):
        request.state.current_user = request.headers.get("X-Test-User", "alice")
        return await call_next(request)

    app.add_api_route("/api/chat_stream", endpoint, methods=["POST"])
    with TestClient(app) as client:
        yield client, captured, events
    engine.dispose()


def test_runtime_control_events_survive_route_filter_and_reach_detached_snapshot(stream_client):
    client, captured, expected = stream_client
    response = client.post("/api/chat_stream", data={"session": "session-1", "message": "hello", "mode": "agent"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Odysseus-Run-Id"]
    observed = [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"]
    control_types = {"context_usage", "compacted", "tool_retry_blocked", "tool_inventory"}
    controls = [event for event in observed if event.get("type") in control_types]
    assert [{key: value for key, value in event.items() if key != "_replay"} for event in controls] == expected[:-1]
    assert all(event["_replay"]["run_id"] == response.headers["X-Odysseus-Run-Id"] for event in controls)
    assert any(event.get("delta") == expected[-1]["delta"] for event in observed)
    assert "agent" in captured
    run = agent_runs._RUNS["session-1"]
    assert run.context_usage["used_tokens"] == 1234
    assert run.context_usage["source"] == "backend"
    assert run.context_usage["compactions"] == 1
    with captured["db_factory"]() as db:
        durable = db.query(ChatRunState).filter_by(run_id=run.run_id).one()
        assert durable.status == "done"
        assert durable.durable_seq == run.durable_seq
        assert durable.context_snapshot["used_tokens"] == 1234


def test_other_owner_cannot_start_or_receive_runtime_context_stream(stream_client):
    client, captured, _events_sent = stream_client
    response = client.post("/api/chat_stream", headers={"X-Test-User": "bob"},
                           data={"session": "session-1", "message": "hello", "mode": "agent"})
    assert response.status_code == 404
    assert "agent" not in captured
    assert agent_runs._RUNS == {}
    assert "context_usage" not in response.text


@pytest.mark.parametrize("event_type, resource", [
    ("budget_exceeded", "tool_calls"),
    ("budget_exceeded", "model_tokens"),
    ("budget_exceeded", "model_requests"),
    ("budget_exceeded", "wall_seconds"),
    ("budget_exceeded", "children"),
    ("rounds_exhausted", "model_rounds"),
])
def test_goal_tool_budget_event_parks_goal_before_detached_run_ends(
        stream_client, monkeypatch, event_type, resource):
    client, captured, _events_sent = stream_client
    calls = []

    class BudgetWorkStore:
        goal = {"id": "safe-goal", "status": "active", "revision": 1, "attempt": 3,
                "objective": "Harmless bounded fixture", "checkpoint": {}}

        def get(self, owner, session):
            assert (owner, session) == ("alice", "session-1")
            return {"plan": None, "goal": dict(self.goal), "cursor": 0}

        def wait_on_goal_budget(self, owner, session, *, resource, used, limit, run_id,
                                expected_goal_id, expected_attempt, usage_source=None):
            calls.append((owner, session, resource, used, limit, run_id,
                          expected_goal_id, expected_attempt, usage_source))
            self.goal = {**self.goal, "status": "waiting_user", "revision": 2,
                         "checkpoint": {"_wait_reason": "resource_budget"}}
            return dict(self.goal)

    work = BudgetWorkStore()
    monkeypatch.setattr(chat_routes, "chat_work_store", work)

    async def budget_stream(*_args, **_kwargs):
        run_id = agent_runs.get_run_id("session-1")
        yield "data: " + json.dumps({"type": event_type, "resource": resource,
                                    "used": 1, "limit": 1, "run_id": run_id,
                                    "usage_source": "estimated" if resource == "model_tokens" else None}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", budget_stream)
    response = client.post("/api/chat_stream", data={"session": "session-1", "message": "safe", "mode": "agent"})
    assert response.status_code == 200
    run_id = response.headers["X-Odysseus-Run-Id"]
    assert calls == [("alice", "session-1", resource, 1, 1, run_id,
                      "safe-goal", 3, "estimated" if resource == "model_tokens" else None)]
    assert work.goal["status"] == "waiting_user"
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert any(event.get("type") == "goal_update" and event.get("data", {}).get("status") == "waiting_user"
               for event in events)
    with captured["db_factory"]() as db:
        durable = db.query(ChatRunState).filter_by(run_id=run_id).one()
        assert durable.status == "done"
        assert durable.durable_seq >= 1


@pytest.mark.parametrize("goal_limit, expected", [(4, 4), (None, 200)])
def test_active_goal_honors_its_own_model_round_budget(stream_client, monkeypatch,
                                                       goal_limit, expected):
    client, _captured, _events_sent = stream_client
    from src import settings

    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: (
        20 if key == "agent_max_rounds" else
        1000 if key == "goal_max_total_tokens" else
        2 if key == "goal_max_model_requests" else
        30 if key == "goal_max_wall_seconds" else
        goal_limit if key == "goal_max_rounds" and goal_limit is not None else default
    ))

    class ActiveGoalStore:
        def get(self, owner, session):
            return {"plan": None, "goal": {
                "id": "safe-goal", "status": "active", "revision": 1,
                "attempt": 1, "objective": "Harmless bounded fixture", "checkpoint": {},
            }, "cursor": 0}

    monkeypatch.setattr(chat_routes, "chat_work_store", ActiveGoalStore())
    observed = {}

    async def bounded_stream(*_args, **kwargs):
        observed["max_rounds"] = kwargs["max_rounds"]
        observed["max_total_tokens"] = kwargs["max_total_tokens"]
        observed["max_model_requests"] = kwargs["max_model_requests"]
        observed["max_wall_seconds"] = kwargs["max_wall_seconds"]
        yield 'data: {"delta":"done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", bounded_stream)
    response = client.post("/api/chat_stream", data={
        "session": "session-1", "message": "safe", "mode": "agent",
    })
    assert response.status_code == 200
    assert observed["max_rounds"] == expected
    assert observed["max_total_tokens"] == 1000
    assert observed["max_model_requests"] == 2
    assert observed["max_wall_seconds"] == 30


def test_ordinary_agent_retains_message_round_budget(stream_client, monkeypatch):
    client, _captured, _events_sent = stream_client
    from src import settings

    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: (
        4 if key == "agent_max_rounds" else
        200 if key == "goal_max_rounds" else default
    ))
    observed = {}

    async def bounded_stream(*_args, **kwargs):
        observed["max_rounds"] = kwargs["max_rounds"]
        observed["max_total_tokens"] = kwargs["max_total_tokens"]
        observed["max_model_requests"] = kwargs["max_model_requests"]
        observed["max_wall_seconds"] = kwargs["max_wall_seconds"]
        yield 'data: {"delta":"done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", bounded_stream)
    response = client.post("/api/chat_stream", data={
        "session": "session-1", "message": "safe", "mode": "agent",
    })
    assert response.status_code == 200
    assert observed["max_rounds"] == 4
    assert observed["max_total_tokens"] == 0
    assert observed["max_model_requests"] == 0
    assert observed["max_wall_seconds"] == 0


@pytest.mark.parametrize("detail, expected_code", [
    ("summarizer_timeout", "summarizer_timeout"),
    ("private provider response token", "context_policy_error"),
])
def test_checkpoint_failure_parks_goal_on_first_failed_attempt(stream_client, monkeypatch,
                                                                detail, expected_code):
    client, _captured, _events_sent = stream_client
    calls = []

    class CheckpointWorkStore:
        goal = {"id": "safe-goal", "status": "active", "revision": 1, "attempt": 1,
                "objective": "Harmless bounded fixture", "checkpoint": {}}

        def get(self, owner, session):
            assert (owner, session) == ("alice", "session-1")
            return {"plan": None, "goal": dict(self.goal), "cursor": 0}

        def record_goal_failure(self, owner, session, error, checkpoint, *, force_wait_user=False):
            calls.append((checkpoint.get("reason"), checkpoint.get("failure_code"),
                          force_wait_user))
            self.goal = {**self.goal, "status": "waiting_user" if force_wait_user else "active",
                         "revision": 2}
            return dict(self.goal)

    work = CheckpointWorkStore()
    monkeypatch.setattr(chat_routes, "chat_work_store", work)

    async def failed_stream(*_args, **_kwargs):
        yield "data: " + json.dumps({"type": "context_compaction_failed",
                                     "reason": "failed", "detail": detail}) + "\n\n"
        yield "data: " + json.dumps({"type": "agent_terminal", "data": {
            "failed": True, "failure": {"kind": "context_compaction", "status": None,
                                        "message": "Context checkpoint failed"},
            "round_texts": [], "round_reasonings": [], "tool_events": [],
        }}) + "\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", failed_stream)
    response = client.post("/api/chat_stream", data={"session": "session-1", "message": "safe", "mode": "agent"})
    assert response.status_code == 200
    assert calls == [("context_compaction", expected_code, True)]
    assert work.goal["status"] == "waiting_user"


def test_terminal_goal_retry_uses_shared_fenced_dispatcher(stream_client, monkeypatch):
    from src import goal_controller

    client, _captured, _events_sent = stream_client
    calls = []

    class GoalWorkStore:
        goal = {"id": "safe-goal", "status": "active", "revision": 1,
                "attempt": 1, "objective": "Harmless fixture", "checkpoint": {},
                "failure_count": 0}

        def get(self, owner, session):
            assert (owner, session) == ("alice", "session-1")
            return {"plan": None, "goal": dict(self.goal), "cursor": 0}

        def acquire_goal_lease(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(chat_routes, "chat_work_store", GoalWorkStore())

    async def dispatch(owner, session, *, reason, expected_goal_id=None,
                       expected_attempt=None):
        calls.append((owner, session, reason, expected_goal_id, expected_attempt))
        return True

    monkeypatch.setattr(goal_controller, "dispatch_goal_continuation", dispatch)
    response = client.post("/api/chat_stream", data={"session": "session-1", "message": "safe", "mode": "agent"})
    assert response.status_code == 200
    run = agent_runs._RUNS["session-1"]
    asyncio.run(run.on_terminal("done"))
    assert ("alice", "session-1", "terminal_done", "safe-goal", 1) in calls


@pytest.mark.parametrize("terminal_status", ["done", "error"])
def test_stale_terminal_callback_cannot_mutate_revised_goal(
        stream_client, monkeypatch, terminal_status):
    from src import goal_controller

    client, _captured, _events_sent = stream_client
    dispatches = []
    failures = []

    class GoalWorkStore:
        goal = {"id": "safe-goal", "status": "active", "revision": 1,
                "attempt": 1, "objective": "Harmless fixture", "checkpoint": {},
                "failure_count": 0}

        def get(self, owner, session):
            return {"plan": None, "goal": dict(self.goal), "cursor": 0}

        def record_goal_failure(self, *_args, **_kwargs):
            failures.append(True)
            return dict(self.goal)

    work = GoalWorkStore()
    monkeypatch.setattr(chat_routes, "chat_work_store", work)
    original_start = agent_runs.start
    callbacks = []

    def detached_without_auto_callback(*args, on_terminal=None, **kwargs):
        callbacks.append(on_terminal)
        return original_start(*args, on_terminal=None, **kwargs)

    monkeypatch.setattr(agent_runs, "start", detached_without_auto_callback)

    async def dispatch(owner, session, *, reason, expected_goal_id=None,
                       expected_attempt=None):
        dispatches.append((owner, session, reason))
        return True

    monkeypatch.setattr(goal_controller, "dispatch_goal_continuation", dispatch)
    response = client.post("/api/chat_stream", data={"session": "session-1", "message": "safe", "mode": "agent"})
    assert response.status_code == 200
    assert len(callbacks) == 1
    work.goal = {**work.goal, "attempt": 2, "revision": 2,
                 "objective": "Revised harmless fixture"}
    asyncio.run(callbacks[0](terminal_status))
    assert failures == []
    assert dispatches == []
