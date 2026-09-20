from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatContextCompaction, ChatRunState, Session
from src import context_compaction_ledger as ledger


def test_compaction_marker_is_durable_and_settled(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = sessionmaker(bind=engine)
    monkeypatch.setattr(ledger, "SessionLocal", store)
    db = store()
    db.add(Session(id="s", name="n", endpoint_url="u", model="m", owner="alice"))
    db.commit()
    db.add(ChatRunState(run_id="r", session_id="s", owner="alice", status="running"))
    db.commit(); db.close()
    result = ledger.record("alice", "s", 1, ledger_hash="h", before_tokens=100,
                           after_tokens=50, economics={"reason": "economic"})
    assert result["run_id"] == "r" and result["status"] == "pending_settlement"
    assert ledger.settle("alice", "s", 1) is True
    db = store(); row = db.query(ChatContextCompaction).one()
    assert row.status == "settled" and row.rebuild_marker["kind"] == "rebuild_plan_after_compaction"
    assert row.rebuild_marker["mandatory"] is True
    assert "create_plan" in row.rebuild_marker["required_tools"]
    db.close()
