"""Owner and bounded projection regression for the run inspector."""

from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (Base, Session, ChatRunState, ChatSubagentRun,
                           ChatToolIntent, ChatSubagentEvidence, ChatSubagentEvent)
from routes import chat_work_routes
from src import run_inspector
from src.chat_replay_log import ReplayLog


def test_run_inspector_is_owner_scoped_bounded_and_content_free(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 9, 23, 10, 0, 0)
    with factory.begin() as db:
        db.add(Session(id="chat", owner="alice", name="safe", model="m",
                       endpoint_url="http://fixture.invalid/v1"))
        db.flush()
        db.add_all([
            ChatRunState(run_id="r1", session_id="chat", owner="alice", status="running",
                         started_at=now, last_seq=14, durable_seq=13,
                         continuation={"secret": "PRIVATE_PROMPT"}),
            ChatRunState(run_id="r2", session_id="chat", owner="bob", status="done",
                         started_at=now, continuation={"secret": "FOREIGN_PROMPT"}),
            ChatRunState(run_id="a0", session_id="chat", owner="alice", status="done",
                         started_at=datetime(2026, 9, 22, 10, 0, 0)),
            ChatSubagentRun(id="child", parent_session_id="chat", parent_run_id="r1",
                            owner="alice", objective="PRIVATE_OBJECTIVE",
                            assigned_context="PRIVATE_CONTEXT", model="m2", status="running"),
            ChatToolIntent(id="intent", owner="alice", session_id="chat", run_id="r1",
                           tool_call_id="call", tool_name="read_file", action_hash="hash",
                           status="done", receipt={"secret": "PRIVATE_RECEIPT"}),
        ])
        db.flush()
        db.add(ChatSubagentEvidence(id="e" * 32, owner="alice", parent_session_id="chat",
                                    child_id="child", kind="test", body="PRIVATE_EVIDENCE",
                                    content_hash="hash"))
        db.add(ChatSubagentEvent(child_id="child", parent_session_id="chat", owner="alice",
                                 kind="status", payload={"status": "running"}))
    monkeypatch.setattr(run_inspector, "SessionLocal", factory)
    def verify(request, session_id):
        if request.headers.get("X-Test-Owner") != "alice":
            raise HTTPException(403, "Not your chat")
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", verify)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: request.headers.get("X-Test-Owner"))
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        desktop = client.get("/api/chat/work/chat/run-inspector", headers={"X-Test-Owner": "alice"})
        mobile = client.get("/api/chat/work/chat/run-inspector", headers={"X-Test-Owner": "alice", "X-Device": "mobile"})
        foreign = client.get("/api/chat/work/chat/run-inspector", headers={"X-Test-Owner": "bob"})
        invalid = client.get("/api/chat/work/chat/run-inspector?limit=1000", headers={"X-Test-Owner": "alice"})
        bounded = client.get("/api/chat/work/chat/run-inspector?limit=1", headers={"X-Test-Owner": "alice"})
        older_runs = client.get("/api/chat/work/chat/run-inspector?limit=1&before_run_id=r1",
                                headers={"X-Test-Owner": "alice"})
        artifact = client.get("/api/chat/work/chat/run-inspector/artifacts/" + "e" * 32,
                              headers={"X-Test-Owner": "alice"})
        foreign_artifact = client.get("/api/chat/work/chat/run-inspector/artifacts/" + "e" * 32,
                                      headers={"X-Test-Owner": "bob"})
    assert desktop.status_code == mobile.status_code == 200
    assert desktop.json() == mobile.json()
    assert foreign.status_code == 403 and invalid.status_code == 400
    assert [row["run_id"] for row in desktop.json()["runs"]] == ["r1", "a0"]
    assert [row["run_id"] for row in bounded.json()["runs"]] == ["r1"]
    assert bounded.json()["has_more"] is True
    assert bounded.json()["next_cursor"] == "r1"
    assert [row["run_id"] for row in older_runs.json()["runs"]] == ["a0"]
    assert older_runs.json()["has_more"] is False
    run = desktop.json()["runs"][0]
    assert run["durable_seq"] == 13
    assert run["children"][0]["child_run_id"] == "child"
    assert run["children"][0]["event_cursor"] == 1
    assert run["children"][0]["artifacts"][0]["id"] == "e" * 32
    assert run["children"][0]["artifacts"][0]["status"] == "published"
    assert run["tool_calls"][0]["tool_call_id"] == "call"
    assert not any(secret in desktop.text for secret in (
        "PRIVATE_PROMPT", "FOREIGN_PROMPT", "PRIVATE_OBJECTIVE", "PRIVATE_CONTEXT",
        "PRIVATE_RECEIPT", "PRIVATE_EVIDENCE"))
    assert artifact.status_code == 200 and artifact.json()["body"] == "PRIVATE_EVIDENCE"
    assert foreign_artifact.status_code == 403
    engine.dispose()


def test_run_inspector_event_page_has_exact_cursor_without_frame_content(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    run_id = "a" * 32
    with factory.begin() as db:
        db.add(Session(id="chat", owner="alice", name="safe", model="m",
                       endpoint_url="http://fixture.invalid/v1"))
        db.flush()
        db.add(ChatRunState(run_id=run_id, session_id="chat", owner="alice",
                            status="done", last_seq=2, durable_seq=2))
    log = ReplayLog(tmp_path, run_id, "chat", create=True)
    log.append('data: {"type":"agent_step","round":1,"_replay":{"seq":0,"created_at":5,"segment_id":"seg-1"}}\n\n')
    log.append('data: {"type":"tool_start","tool":"read_file","command":"PRIVATE_COMMAND","_replay":{"seq":1,"created_at":6,"segment_id":"seg-2","tool_call_id":"call-1"}}\n\n')
    log.append('data: {"type":"tool_output","output":"PRIVATE_OUTPUT","_replay":{"seq":2,"created_at":7,"segment_id":"seg-2","tool_call_id":"call-1"}}\n\n')
    log.checkpoint("done")
    monkeypatch.setattr(run_inspector, "SessionLocal", factory)
    monkeypatch.setattr("src.agent_runs.replay_root", lambda: tmp_path)
    def verify(request, session_id):
        if request.headers.get("X-Test-Owner") != "alice":
            raise HTTPException(403, "Not your chat")
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", verify)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: request.headers.get("X-Test-Owner"))
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    url = f"/api/chat/work/chat/run-inspector/{run_id}/events"
    with TestClient(app) as client:
        recent = client.get(url + "?limit=2", headers={"X-Test-Owner": "alice"})
        older = client.get(url + "?before_seq=1&limit=2", headers={"X-Test-Owner": "alice"})
        foreign = client.get(url, headers={"X-Test-Owner": "bob"})
        invalid = client.get(url + "?before_seq=-1", headers={"X-Test-Owner": "alice"})
    assert recent.status_code == older.status_code == 200
    assert [row["seq"] for row in recent.json()["events"]] == [1, 2]
    assert recent.json()["previous_cursor"] == 1
    assert recent.json()["has_more_before"] is True
    assert recent.json()["events"][0]["tool_call_id"] == "call-1"
    assert recent.json()["events"][0]["artifact_seq"] is None
    assert recent.json()["events"][1]["artifact_seq"] == 2
    assert [row["seq"] for row in older.json()["events"]] == [0]
    assert foreign.status_code == 403 and invalid.status_code == 400
    assert "PRIVATE_COMMAND" not in recent.text and "PRIVATE_OUTPUT" not in recent.text
    engine.dispose()


def test_run_inspector_auth_disabled_uses_existing_durable_owner_keys(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        db.add(Session(id="chat", owner="__odysseus_local__", name="safe", model="m",
                       endpoint_url="http://fixture.invalid/v1"))
        db.flush()
        db.add(ChatRunState(run_id="b" * 32, session_id="chat",
                            owner="__odysseus_single_user__", status="done"))
        db.add(ChatSubagentRun(id="child", parent_session_id="chat", parent_run_id="c" * 32,
                               owner="", objective="PRIVATE", model="m", status="done"))
    monkeypatch.setattr(run_inspector, "SessionLocal", factory)
    result = run_inspector.snapshot(None, "chat")
    assert len(result["runs"]) == 1
    assert result["runs"][0]["children"] == []
    assert result["unlinked_children"][0]["child_run_id"] == "child"
    assert result["unlinked_children"][0]["parent_run_id"] == "c" * 32
    assert "PRIVATE" not in str(result)
    engine.dispose()
