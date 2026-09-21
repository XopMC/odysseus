from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatContextCompaction, ChatRunState, Session
from src import context_compaction_ledger as ledger
from pathlib import Path


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
    older = ledger.record("alice", "s", 1, ledger_hash="h1", before_tokens=150,
                          after_tokens=100, economics={"reason": "economic"})
    result = ledger.record("alice", "s", 2, ledger_hash="h2", before_tokens=100,
                           after_tokens=50, economics={"reason": "economic"})
    assert result["run_id"] == "r" and result["status"] == "pending_settlement"
    restored = ledger.pending("alice", "s")
    assert restored["id"] == result["id"]
    assert restored["generation"] == 2
    assert restored["rebuild_marker"]["mandatory"] is True
    assert ledger.settle("alice", "s", 2) is True
    assert ledger.pending("alice", "s") is None
    db = store(); rows = db.query(ChatContextCompaction).order_by(ChatContextCompaction.generation).all()
    assert [row.status for row in rows] == ["settled", "settled"]
    row = rows[-1]
    assert row.rebuild_marker["kind"] == "rebuild_plan_after_compaction"
    assert row.rebuild_marker["mandatory"] is True
    assert "create_plan" in row.rebuild_marker["required_tools"]
    db.close()


def test_record_never_reuses_generation_after_recovered_checkpoint(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = sessionmaker(bind=engine)
    monkeypatch.setattr(ledger, "SessionLocal", store)
    db = store()
    db.add(Session(id="s", name="n", endpoint_url="u", model="m", owner="alice"))
    db.commit()
    db.add(ChatRunState(run_id="r", session_id="s", owner="alice", status="running"))
    db.commit(); db.close()
    first = ledger.record("alice", "s", 92, ledger_hash="h1", before_tokens=150,
                          after_tokens=100, economics={})
    recovered = ledger.record("alice", "s", 1, ledger_hash="h2", before_tokens=140,
                              after_tokens=90, economics={})
    assert first["rebuild_marker"]["generation"] == 92
    assert recovered["rebuild_marker"]["generation"] == 93
    assert ledger.settle("alice", "s", 93) is True


def test_agent_settles_the_pending_marker_generation_not_local_counter():
    source = (Path(__file__).resolve().parents[1] / "src/agent_loop.py").read_text()
    assert '_pending_compaction_settlement.get("generation")' in source
    assert '_settle_compaction(owner, session_id, _settlement_generation)' in source


def test_agent_server_recovery_plan_settles_before_continuing():
    source = (Path(__file__).resolve().parents[1] / "src/agent_loop.py").read_text(
        encoding="utf-8"
    )
    assert "if _compaction_plan_nudges >= 1:" in source
    branch = source.split("if _compaction_plan_nudges >= 1:", 1)[1].split(
        '"type": "context_compaction_failed"', 1,
    )[0]
    assert 'replace_terminal=True' in branch
    assert '_work_store.plan_action(' in branch
    assert 'if not _settle_compaction(' in branch
    assert branch.index('if not _settle_compaction(') < branch.index(
        '"reason": "server_recovery_plan"'
    )
