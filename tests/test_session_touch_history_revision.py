"""Reading an idle chat must not invalidate its history revision."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Session as DbSession
from core.session_manager import SessionManager
import core.session_manager as session_manager


def test_touch_updates_access_without_changing_history_revision(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DbSession.__table__])
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(session_manager, "SessionLocal", factory)
    revision = datetime(2026, 9, 28, 12, 0, 0)
    with factory.begin() as db:
        db.add(DbSession(
            id="idle-chat", name="QA", endpoint_url="http://fixture.invalid", model="fixture",
            updated_at=revision, last_accessed=revision - timedelta(days=1),
        ))

    manager = SessionManager.__new__(SessionManager)
    manager._touch_session("idle-chat")
    with factory() as db:
        row = db.get(DbSession, "idle-chat")
        first_access = row.last_accessed
        assert first_access > revision
        assert row.updated_at == revision

    # Polling the same idle chat every three seconds must not create another
    # write or a new revision that forces every browser to fetch history.
    manager._touch_session("idle-chat")
    with factory() as db:
        row = db.get(DbSession, "idle-chat")
        assert row.last_accessed == first_access
        assert row.updated_at == revision
