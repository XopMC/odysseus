from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage
import routes.history_routes as history_routes
import routes.session_routes as session_routes


class _FakeQuery:
    def __init__(self, rows=None, first_row=None):
        self._rows = rows or []
        self._first_row = first_row

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._first_row


class _FakeDb:
    def __init__(self):
        self.added = []
        self.deleted = []
        self.session_row = SimpleNamespace(message_count=0, updated_at=None)

    def query(self, model):
        if model is history_routes.DbSession:
            return _FakeQuery(first_row=self.session_row)
        return _FakeQuery(rows=[])

    def add(self, row):
        self.added.append(row)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session
        self.saved = False
        self.replaced_messages = None

    def get_session(self, session_id):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session

    def save_sessions(self):
        self.saved = True

    def replace_messages(self, session_id, messages):
        if session_id != self.session.id:
            return False
        self.replaced_messages = list(messages)
        self.session.history = list(messages)
        self.session.message_count = len(messages)
        return True


class _FakeSession:
    id = "session-1"
    name = "Tool session"
    endpoint_url = "http://example.test/v1"
    model = "test-model"
    headers = {}
    owner = "session-owner"

    def __init__(self, history):
        self.history = history
        self.message_count = len(history)

    def get_context_messages(self):
        return [
            msg.to_dict() if isinstance(msg, ChatMessage) else msg
            for msg in self.history
        ]


def _compact_prompt_for(monkeypatch, history):
    captured = {}

    async def fake_llm_call_async(endpoint_url, model, messages, **kwargs):
        captured["messages"] = messages
        captured["timeout"] = kwargs.get("timeout")
        return "Summary text"

    monkeypatch.setattr(
        session_routes,
        "router",
        APIRouter(prefix="/api", tags=["sessions"]),
    )
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: _FakeDb())

    import src.agent_runs as agent_runs
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core
    import src.model_context as model_context

    monkeypatch.setattr(agent_runs, "is_active", lambda session_id: False)
    def fake_resolve_endpoint(kind, owner=None, **kwargs):
        captured.setdefault("resolve_calls", []).append((kind, owner))
        return kwargs.get("fallback_url"), kwargs.get("fallback_model"), kwargs.get("fallback_headers") or {}

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", fake_resolve_endpoint)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(model_context, "estimate_tokens", lambda messages: 100)
    monkeypatch.setattr(model_context, "get_context_length", lambda endpoint_url, model: 1000)

    session = _FakeSession(history)
    manager = _FakeSessionManager(session)
    app = FastAPI()
    app.include_router(session_routes.setup_session_routes(manager, {}))

    response = TestClient(app).post("/api/session/session-1/compact")

    assert response.status_code == 200
    assert response.json()["status"] == "compacted"
    assert manager.saved is True
    return captured["messages"][1]["content"]


def _registered_compact_response(monkeypatch, history, active_run=False, working_messages=None):
    captured = {}

    async def fake_llm_call_async(endpoint_url, model, messages, **kwargs):
        captured["messages"] = messages
        captured["timeout"] = kwargs.get("timeout")
        return "Summary text"

    monkeypatch.setattr(
        session_routes,
        "router",
        APIRouter(prefix="/api", tags=["sessions"]),
    )
    monkeypatch.setattr(session_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(history_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(history_routes, "SessionLocal", lambda: _FakeDb())

    import src.agent_runs as agent_runs
    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(agent_runs, "is_active", lambda session_id: active_run)
    def fake_resolve_endpoint(kind, owner=None, **kwargs):
        captured.setdefault("resolve_calls", []).append((kind, owner))
        return kwargs.get("fallback_url"), kwargs.get("fallback_model"), kwargs.get("fallback_headers") or {}

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", fake_resolve_endpoint)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)

    session = _FakeSession(history)
    if working_messages is not None:
        session.get_context_messages = lambda: list(working_messages)
    manager = _FakeSessionManager(session)
    app = FastAPI()
    app.include_router(session_routes.setup_session_routes(manager, {}))
    app.include_router(history_routes.setup_history_routes(manager))

    response = TestClient(app).post("/api/session/session-1/compact")
    return response, captured, manager


def test_manual_compact_tolerates_chatmessage_with_none_content(monkeypatch):
    compact_prompt = _compact_prompt_for(
        monkeypatch,
        [
            ChatMessage(role="user", content="start"),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt


def test_manual_compact_tolerates_dict_message_with_none_content(monkeypatch):
    compact_prompt = _compact_prompt_for(
        monkeypatch,
        [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": None},
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt


def test_registered_manual_compact_route_tolerates_none_content(monkeypatch):
    original = [
        ChatMessage(role="user", content="start"),
        ChatMessage(role="assistant", content=None),
        ChatMessage(role="tool", content="tool result"),
        ChatMessage(role="assistant", content="done"),
        ChatMessage(role="user", content="next"),
        ChatMessage(role="assistant", content="final"),
    ]
    response, captured, manager = _registered_compact_response(
        monkeypatch,
        original,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    compact_prompt = captured["messages"][1]["content"]
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt
    assert response.json()["transcript_preserved"] is True
    assert manager.replaced_messages is None
    assert manager.session.history == original
    assert manager.session.context_checkpoint_count == 2
    assert manager.session.context_checkpoint.metadata["hidden"] is True


def test_registered_manual_compact_route_uses_session_owner(monkeypatch):
    response, captured, manager = _registered_compact_response(
        monkeypatch,
        [
            ChatMessage(role="user", content="start"),
            ChatMessage(role="assistant", content="tool call"),
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )

    assert response.status_code == 200
    assert manager.replaced_messages is None
    assert manager.session.message_count == 6
    assert ("utility", "session-owner") in captured["resolve_calls"]
    assert captured["timeout"] == 600
    assert captured["messages"][1]["content"].startswith("USER: start")


def test_manual_compaction_uses_working_checkpoint_not_full_transcript(monkeypatch):
    history = [ChatMessage(role="user", content=f"old secret {i}") for i in range(100)]
    working = [{"role": "system", "content": "[Conversation summary] prior evidence"}]
    working += [{"role": "user", "content": f"recent {i}"} for i in range(10)]
    response, captured, manager = _registered_compact_response(
        monkeypatch, history, working_messages=working)
    assert response.status_code == 200
    prompt = captured["messages"][1]["content"]
    assert "prior evidence" in prompt
    assert "old secret" not in prompt
    assert manager.session.context_checkpoint_count == 92


def test_manual_compaction_does_not_claim_success_without_reduction(monkeypatch):
    from src import model_context
    monkeypatch.setattr(model_context, "estimate_tokens", lambda messages: 2000)
    history = [ChatMessage(role="user", content=f"entry {i}") for i in range(6)]
    response, _captured, manager = _registered_compact_response(monkeypatch, history)
    assert response.status_code == 200
    assert response.json()["status"] == "unchanged"
    assert response.json()["reason"] == "no_reduction"
    assert manager.session.context_checkpoint is None
    assert manager.saved is False


def test_registered_manual_compact_route_rejects_active_agent_run(monkeypatch):
    response, captured, manager = _registered_compact_response(
        monkeypatch,
        [
            ChatMessage(role="user", content="start"),
            ChatMessage(role="assistant", content="tool call"),
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
        active_run=True,
    )

    assert response.status_code == 409
    assert "active run" in response.text
    assert captured == {}
    assert manager.replaced_messages is None
