"""Agent control events must reach the HTTP client and detached-run snapshot.

The producer is isolated at stream_agent_loop; the real chat route, SSE filter,
detached manager and session-owner check all run. Reuse the existing foreground
route fixture rather than inventing a second routing setup.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Session as DbSession
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
    Base.metadata.create_all(engine, tables=[DbSession.__table__])
    db_factory = sessionmaker(bind=engine)
    with db_factory() as db:
        db.add(DbSession(id="session-1", name="Runtime events", endpoint_url="https://selected.example/v1", model="selected-model", owner="alice"))
        db.commit()
    monkeypatch.setattr(session_routes, "SessionLocal", db_factory)
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
    control_types = {"context_usage", "compacted", "tool_retry_blocked"}
    controls = [event for event in observed if event.get("type") in control_types]
    assert [{key: value for key, value in event.items() if key != "_replay"} for event in controls] == expected[:-1]
    assert all(event["_replay"]["run_id"] == response.headers["X-Odysseus-Run-Id"] for event in controls)
    assert any(event.get("delta") == expected[-1]["delta"] for event in observed)
    assert "agent" in captured
    run = agent_runs._RUNS["session-1"]
    assert run.context_usage["used_tokens"] == 1234
    assert run.context_usage["source"] == "backend"
    assert run.context_usage["compactions"] == 1


def test_other_owner_cannot_start_or_receive_runtime_context_stream(stream_client):
    client, captured, _events_sent = stream_client
    response = client.post("/api/chat_stream", headers={"X-Test-User": "bob"},
                           data={"session": "session-1", "message": "hello", "mode": "agent"})
    assert response.status_code == 404
    assert "agent" not in captured
    assert agent_runs._RUNS == {}
    assert "context_usage" not in response.text
