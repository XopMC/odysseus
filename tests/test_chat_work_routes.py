"""Plan/Goal controls are owner-scoped, not tied to one browser Origin."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import chat_work_routes


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


def test_goal_resume_dispatches_server_controller_before_return(monkeypatch):
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
