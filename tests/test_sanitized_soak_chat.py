"""The long-chat fixture preserves load shape without copying source content."""

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ChatMessage, Session
from scripts import create_sanitized_soak_chat as soak


def test_sanitized_clone_has_no_source_text_goal_or_workspace(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(soak, "SessionLocal", factory)
    secret = "PRIVATE_SOURCE_DO_NOT_COPY"
    with factory.begin() as db:
        db.add(Session(id="source", owner="alice", name=secret, model="old-model",
                       endpoint_url="http://old", project_id="sensitive-project", message_count=2))
        db.add(ChatMessage(id="m1", session_id="source", role="user", content=secret,
                           meta_data=json.dumps({"goal": secret, "workspace": secret})))
        db.add(ChatMessage(id="m2", session_id="source", role="assistant", content=secret,
                           meta_data=json.dumps({
                               "round_texts": [secret], "round_reasonings": [secret],
                               "tool_events": [{"tool": "bash", "command": secret, "output": secret,
                                                "round": 1, "exit_code": 0}],
                               "timeline_v2": {"run_id": secret, "events": [
                                   {"type": "tool_output", "command": secret, "output": secret, "seq": 1},
                               ]},
                               "rendered_message_count": 2,
                           })))

    result = soak.create_clone(source_session_id="source", owner="alice",
                               model="safe-model", endpoint_url="http://safe")
    with factory() as db:
        clone = db.query(Session).filter_by(id=result["clone_id"]).one()
        messages = db.query(ChatMessage).filter_by(session_id=clone.id).all()
        assert clone.owner == "alice"
        assert clone.project_id is None and clone.context_checkpoint is None
        assert clone.model == "safe-model" and clone.endpoint_url == "http://safe"
        assert len(messages) == result["synthetic_rows"] == 2
        for message in messages:
            assert secret not in message.content
            assert secret not in message.meta_data
        metadata = json.loads(messages[1].meta_data)
        assert len(metadata["round_reasonings"]) == 1
        assert len(metadata["tool_events"]) == 1
        assert metadata["tool_events"][0]["tool"] == "synthetic_check"
    engine.dispose()


def test_existing_seeded_destination_is_populated_but_never_overwritten(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Session.__table__, ChatMessage.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(soak, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(Session(id="source", owner="alice", name="source", model="old", endpoint_url="http://old"))
        db.add(Session(id="safe-empty", owner="alice", name="empty", model="safe", endpoint_url="http://safe"))
        db.add(ChatMessage(id="m1", session_id="source", role="user", content="private objective"))
        db.add(ChatMessage(id="seed", session_id="safe-empty", role="user", content="SAFE STRUCTURAL SOAK FIXTURE SEED"))

    preview = soak.create_clone(source_session_id="source", owner="alice", model="safe-model",
                                endpoint_url="http://safe", dry_run=True)
    assert preview["dry_run"] is True and preview["clone_id"] is None
    with factory() as db:
        assert db.query(Session).count() == 2
        assert db.query(ChatMessage).count() == 2

    result = soak.create_clone(source_session_id="source", owner="alice", model="safe-model",
                               endpoint_url="http://safe", destination_session_id="safe-empty", target_rows=5)
    assert result["clone_id"] == "safe-empty"
    with factory() as db:
        rows = db.query(ChatMessage).filter_by(session_id="safe-empty").all()
        assert len(rows) == 6 and all("private" not in row.content for row in rows)
    try:
        soak.create_clone(source_session_id="source", owner="alice", model="safe-model",
                          endpoint_url="http://safe", destination_session_id="safe-empty")
    except ValueError as exc:
        assert "exactly" in str(exc)
    else:
        raise AssertionError("a populated destination must not be overwritten")
    engine.dispose()
