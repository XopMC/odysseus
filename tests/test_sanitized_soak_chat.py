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
