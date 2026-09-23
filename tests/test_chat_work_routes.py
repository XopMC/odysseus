"""Plan/Goal controls are owner-scoped, not tied to one browser Origin."""
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Session as DbSession

from routes import chat_work_routes
from src.chat_work_store import WorkNotFound


class WorkStoreStub:
    def __init__(self):
        self.calls = []

    def goal_action(self, owner, session_id, action, expected_revision):
        self.calls.append((owner, session_id, action, expected_revision))
        return {
            "id": "goal-1", "session_id": session_id, "objective": "Ship it",
            "status": "cancelled", "attempt": 1, "progress": "",
            "checkpoint": {}, "last_error": None, "failure_count": 0,
            "revision": expected_revision + 1,
        }


def test_goal_cancel_accepts_authenticated_owner_from_any_browser(monkeypatch):
    stub = WorkStoreStub()
    monkeypatch.setattr(chat_work_routes, "store", stub)
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: "alice")
    app = FastAPI()
    app.include_router(chat_work_routes.setup_chat_work_routes())

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/work/chat-1/goal/cancel",
            json={"expected_revision": 4},
            headers={"Origin": "https://phone.example"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert stub.calls == [("alice", "chat-1", "cancel", 4)]


def test_why_waiting_is_same_for_two_owner_clients_and_denies_foreign_owner(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DbSession.__table__])
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        db.add(DbSession(id="chat-1", owner="alice", name="Safe test", model="fixture-model",
                         endpoint_url="https://user:secret@model.example:1234/v1?token=private"))
    monkeypatch.setattr("core.database.SessionLocal", factory)
    class WaitStore:
        def wait_metadata(self, owner, session_id):
            assert (owner, session_id) == ("alice", "chat-1")
            return {"status": "waiting_user", "attempt": 2,
                    "lease_held": False, "lease_expires_at": None}

    def verify(request, session_id):
        if request.headers.get("X-Test-Owner") != "alice":
            raise HTTPException(403, "Not your chat")

    monkeypatch.setattr(chat_work_routes, "store", WaitStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", verify)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: request.headers.get("X-Test-Owner"))
    from src import agent_runs, subagent_runtime
    monkeypatch.setattr(agent_runs, "describe_run", lambda session_id: {
        "run_id": "run-1", "status": "done", "started_at": 100,
        "durable_seq": 7, "context_revision": 3, "ledger_hash": "a" * 64,
        "wait_state": {"phase": "user", "phase_since": 140, "model": "fixture-model"},
        "progress_health": {"revision": 1, "stalled": False},
    })
    monkeypatch.setattr(subagent_runtime.runtime, "active_summary", lambda owner, session_id, **kwargs: [])
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())

    with TestClient(app) as client:
        desktop = client.get("/api/chat/work/chat-1/why-waiting", headers={
            "X-Test-Owner": "alice", "X-Device": "desktop",
        })
        mobile = client.get("/api/chat/work/chat-1/why-waiting", headers={
            "X-Test-Owner": "alice", "X-Device": "mobile",
        })
        foreign = client.get("/api/chat/work/chat-1/why-waiting", headers={
            "X-Test-Owner": "bob",
        })

    assert desktop.status_code == mobile.status_code == 200
    for response in (desktop, mobile):
        assert response.json()["phase"] == "user"
        assert response.json()["run_id"] == "run-1"
        assert response.json()["checkpoint"]["durable_seq"] == 7
        assert response.json()["selected_endpoint_label"] == "model.example:1234"
        assert "secret" not in response.text and "private" not in response.text
        assert "Cache-Control" in response.headers
    assert foreign.status_code == 403
    engine.dispose()


def test_unknown_effect_inbox_is_identical_across_clients_and_owner_scoped(monkeypatch):
    from src.chat_effect_inbox import inbox

    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: request.headers.get("X-Test-Owner"))

    def unknown(owner, session_id):
        if owner != "alice":
            raise WorkNotFound("Chat not found")
        assert session_id == "chat-1"
        return [{"id": "effect-1", "status": "unknown", "action_hash": "a" * 64}]

    monkeypatch.setattr(inbox, "unknown", unknown)
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        desktop = client.get("/api/chat/work/chat-1/unknown-effects", headers={"X-Test-Owner": "alice"})
        mobile = client.get("/api/chat/work/chat-1/unknown-effects", headers={"X-Test-Owner": "alice", "X-Device": "mobile"})
        foreign = client.get("/api/chat/work/chat-1/unknown-effects", headers={"X-Test-Owner": "bob"})
    assert desktop.status_code == mobile.status_code == 200
    assert desktop.json() == mobile.json()
    assert foreign.status_code == 404


def test_goal_resume_is_rejected_while_unknown_effect_is_unresolved(monkeypatch):
    from src.chat_effect_inbox import inbox

    class GoalStore:
        def goal_action(self, *args):
            raise AssertionError("Goal must not resume before effect reconciliation")

    monkeypatch.setattr(chat_work_routes, "store", GoalStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [{"id": "effect-1"}])
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        response = client.post("/api/chat/work/chat-1/goal/resume", json={"expected_revision": 5})
    assert response.status_code == 409
    assert "unknown" in str(response.json()).lower()


def test_owner_can_choose_no_retry_with_cas_but_foreign_owner_cannot(monkeypatch):
    from src.chat_effect_inbox import inbox
    calls = []
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: request.headers.get("X-Test-Owner"))

    def no_retry(owner, session, intent, *, expected_revision):
        if owner != "alice":
            raise WorkNotFound("Tool intent not found")
        calls.append((owner, session, intent, expected_revision))
        return {"id": intent, "status": "no_retry", "revision": expected_revision + 1}

    monkeypatch.setattr(inbox, "no_retry", no_retry)
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        good = client.post("/api/chat/work/chat-1/unknown-effects/effect-1/no-retry",
                           json={"expected_revision": 2}, headers={"X-Test-Owner": "alice"})
        foreign = client.post("/api/chat/work/chat-1/unknown-effects/effect-1/no-retry",
                              json={"expected_revision": 2}, headers={"X-Test-Owner": "bob"})
        malformed = client.post("/api/chat/work/chat-1/unknown-effects/effect-1/no-retry",
                                json={"expected_revision": 2, "replay": True},
                                headers={"X-Test-Owner": "alice"})
    assert good.status_code == 200 and good.json()["status"] == "no_retry"
    assert foreign.status_code == 404 and malformed.status_code == 400
    assert calls == [("alice", "chat-1", "effect-1", 2)]


def test_goal_cancel_still_requires_an_authenticated_owner(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: None)
    app = FastAPI()
    app.include_router(chat_work_routes.setup_chat_work_routes())

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/work/chat-1/goal/cancel",
            json={"expected_revision": 4},
            headers={"Origin": "https://phone.example"},
        )

    assert response.status_code == 401


def test_goal_snapshot_supports_explicit_single_user_mode(monkeypatch):
    class SnapshotStore:
        def get(self, owner, session_id):
            assert owner is None
            return {"plan": None, "goal": None, "cursor": 0}

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(chat_work_routes, "store", SnapshotStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: None)
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        response = client.get("/api/chat/work/chat-1")
    assert response.status_code == 200
    assert response.json() == {"plan": None, "goal": None, "cursor": 0}


def test_events_stream_rejects_missing_work_state_before_starting_sse(monkeypatch):
    class MissingStore:
        def get(self, owner, session_id):
            raise WorkNotFound("Chat not found")

        def events(self, owner, session_id, after, limit):
            raise AssertionError("stream generator must not start")

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(chat_work_routes, "store", MissingStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: None)
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/chat/work/missing/events/stream?after=0")
    assert response.status_code == 404
    assert response.json()["detail"] == "Chat not found"


def test_goal_resume_dispatches_server_controller_before_return(monkeypatch):
    from src.chat_effect_inbox import inbox
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])
    class ResumeStore(WorkStoreStub):
        def goal_action(self, owner, session_id, action, expected_revision):
            self.calls.append((owner, session_id, action, expected_revision))
            return {"id": "goal-1", "session_id": session_id, "objective": "Ship it",
                    "status": "active", "attempt": 1, "progress": "", "checkpoint": {},
                    "last_error": None, "failure_count": 0, "revision": expected_revision + 1}
    calls = []
    async def dispatch(owner, session_id, *, reason):
        calls.append((owner, session_id, reason)); return True
    import src.goal_controller as controller
    monkeypatch.setattr(controller, "dispatch_goal_continuation", dispatch)
    monkeypatch.setattr(chat_work_routes, "store", ResumeStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: "alice")
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        response = client.post("/api/chat/work/chat-1/goal/resume", json={"expected_revision": 4})
    assert response.status_code == 200
    assert calls == [("alice", "chat-1", "goal_resumed")]


def test_goal_resume_does_not_report_success_when_controller_did_not_start(monkeypatch):
    from src.chat_effect_inbox import inbox
    import src.goal_controller as controller

    class ResumeStore:
        def goal_action(self, owner, session_id, action, expected_revision):
            return {"id": "goal-1", "session_id": session_id, "status": "active",
                    "revision": expected_revision + 1}

    async def failed_dispatch(owner, session_id, *, reason):
        return False

    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])
    monkeypatch.setattr(controller, "dispatch_goal_continuation", failed_dispatch)
    monkeypatch.setattr(chat_work_routes, "store", ResumeStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: "alice")
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        response = client.post("/api/chat/work/chat-1/goal/resume", json={"expected_revision": 4})
    assert response.status_code == 503
    assert "did not start" in response.json()["detail"]


def test_goal_guidance_route_keeps_goal_active(monkeypatch):
    class GuidanceStore:
        def add_goal_guidance(self, owner, session_id, message):
            assert (owner, session_id, message) == ("alice", "chat-1", "check mobile too")
            return {"goal": {"status": "active"}, "guidance": {"id": "g1", "text": message}}

    monkeypatch.setattr(chat_work_routes, "store", GuidanceStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: "alice")
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    with TestClient(app) as client:
        response = client.post("/api/chat/work/chat-1/goal-guidance", json={"message": "check mobile too"})
    assert response.status_code == 200
    assert response.json()["goal"]["status"] == "active"
