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
