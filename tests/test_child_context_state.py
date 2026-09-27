"""A child's compaction generation and economic state never belong to its parent."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Base, ChatContextCompaction, ChatContextEfficiencyState, ChatRunState,
    ChatSubagentEvent, ChatSubagentRun, Session,
)
from src import context_compaction_ledger as ledger, context_efficiency_state as efficiency
from src import subagent_runtime as children


@contextmanager
def lease(child_id, worker=None, owner="alice"):
    token = children._execution_lease.set((child_id, owner, worker or "lease-" + child_id))
    try:
        yield
    finally:
        children._execution_lease.reset(token)


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'context.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(ledger, "SessionLocal", factory)
    monkeypatch.setattr(efficiency, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(Session(id="parent", owner="alice", name="QA", endpoint_url="http://fixture", model="m"))
        db.flush()
        db.add(ChatRunState(run_id="parent-run", session_id="parent", owner="alice", status="running"))
        for ident in ("a", "b"):
            db.add(ChatSubagentRun(id=ident, owner="alice", parent_session_id="parent",
                                  parent_run_id="parent-run", ordinal=1, name="QA", objective="QA",
                                  assigned_context="", model="m", status="running", worker_id="lease-" + ident,
                                  heartbeat_at=children._utcnow(), metrics={"output_chars": 321}))
    yield factory
    engine.dispose()


def record(child_id=None, generation=1):
    return ledger.record("alice", "parent", generation, ledger_hash="verified-hash",
                         before_tokens=1000, after_tokens=300, economics={"reason": "economic"},
                         **({"child_id": child_id} if child_id is not None else {}))


def test_child_compaction_does_not_inherit_parent_generation_or_settle_parent(store):
    parent = record(generation=20)
    with lease("a"):
        child = record("a")
    with lease("b"):
        sibling = record("b")
    assert child["run_id"] == "a" and sibling["run_id"] == "b"
    assert child["rebuild_marker"]["generation"] == sibling["rebuild_marker"]["generation"] == 1
    assert "private" in child["rebuild_marker"]["instruction"].lower()
    assert "parent task" not in child["rebuild_marker"]["instruction"].lower()
    assert ledger.pending("alice", "parent")["id"] == parent["id"]
    with lease("a"):
        assert ledger.settle("alice", "parent", 1, child_id="a")
    assert ledger.pending("alice", "parent", child_id="a") is None
    assert ledger.pending("alice", "parent", child_id="b")["id"] == sibling["id"]
    assert ledger.pending("alice", "parent")["generation"] == 20
    with store() as db:
        assert db.query(ChatContextCompaction).count() == 1
        assert db.get(ChatRunState, "parent-run").continuation["compaction_settlement"]["id"] == parent["id"]
        assert db.get(ChatSubagentRun, "a").metrics["output_chars"] == 321
        kinds = {event.kind for event in db.query(ChatSubagentEvent).filter_by(child_id="a")}
        assert kinds >= {"context_compaction_recorded", "context_compaction_settled"}


def test_child_generation_keeps_its_own_highwater_across_restart(store):
    with lease("a"):
        first = record("a", generation=21)
    with store.begin() as db:
        db.get(ChatSubagentRun, "a").worker_id = "replacement"
    assert ledger.pending("alice", "parent", child_id="a")["id"] == first["id"]
    with lease("a", "replacement"):
        next_record = record("a", generation=1)
        assert next_record["rebuild_marker"]["generation"] == 22
        assert not ledger.settle("alice", "parent", 21, child_id="a")
        assert ledger.settle("alice", "parent", 22, child_id="a")
        assert not ledger.settle("alice", "parent", 22, child_id="a")
    assert ledger.pending("alice", "parent", child_id="a") is None
    assert ledger.pending("alice", "parent") is None


def test_child_efficiency_requests_boundaries_compaction_and_restart_are_isolated(store):
    parent = efficiency.record_provider_request("alice", "parent", 12.5, 190000)
    with lease("a"):
        efficiency.record_provider_request("alice", "parent", 12.5, 100, child_id="a")
        efficiency.record_provider_request("alice", "parent", 12.5, 140, child_id="a")
        boundary = efficiency.record_boundary("alice", "parent", 12.5,
                                               [{"id": "private", "status": "pending"}],
                                               {"step_id": "done"}, child_id="a")
        assert boundary["request_count"] == 2
        assert boundary["positive_context_delta_total"] == 40
        assert boundary["completed_boundary_request_counts"] == [2]
        assert efficiency.record_compaction("alice", "parent", 12.5, 200, 50, child_id="a")["native_compaction_count"] == 1
    with lease("b"):
        assert efficiency.record_provider_request("alice", "parent", 12.5, 10, child_id="b")["request_count"] == 1
    with store.begin() as db:
        db.get(ChatSubagentRun, "a").worker_id = "replacement"
    restored = efficiency.restore("alice", "parent", 99, child_id="a")
    assert restored["cache_write_read_ratio"] == 12.5
    assert restored["cache_debt_tokens"] == 200
    assert restored["request_count"] == 2 and restored["native_compaction_count"] == 1
    with lease("a", "replacement"):
        corrected = efficiency.record_correction("alice", "parent", 99, child_id="a")
    assert corrected["epoch"] == 2 and corrected["cache_debt_tokens"] == 0
    assert efficiency.restore("alice", "parent", 99) == parent
    with store() as db:
        assert db.query(ChatContextEfficiencyState).count() == 1
        assert db.get(ChatSubagentRun, "a").metrics["output_chars"] == 321


@pytest.mark.parametrize("target_owner,target_session,target_child", [
    ("other", "parent", "a"), ("alice", "other", "a"), ("alice", "parent", "missing"),
])
def test_child_reads_reject_wrong_scope_without_parent_fallback(store, target_owner, target_session, target_child):
    for read in (
        lambda: ledger.pending(target_owner, target_session, child_id=target_child),
        lambda: efficiency.restore(target_owner, target_session, 12.5, child_id=target_child),
    ):
        with pytest.raises((ValueError, children.ChildLeaseLost)):
            read()
    with store() as db:
        assert db.query(ChatContextCompaction).count() == 0
        assert db.query(ChatContextEfficiencyState).count() == 0


@pytest.mark.parametrize("fence", ["foreign_lease", "expired", "cancelled", "sibling"])
def test_child_state_writes_obey_execution_lease(store, fence):
    with store.begin() as db:
        row = db.get(ChatSubagentRun, "a")
        if fence == "foreign_lease":
            row.worker_id = "successor"
        elif fence == "expired":
            row.heartbeat_at = children._utcnow() - timedelta(seconds=100)
        elif fence == "cancelled":
            row.cancel_requested = True
    with lease("b" if fence == "sibling" else "a"):
        for write in (lambda: record("a"),
                      lambda: efficiency.record_provider_request("alice", "parent", 12.5, 5, child_id="a"),
                      lambda: ledger.settle("alice", "parent", 1, child_id="a")):
            with pytest.raises(children.ChildLeaseLost):
                write()
    with store() as db:
        assert db.query(ChatSubagentEvent).count() == 0
        assert db.get(ChatSubagentRun, "a").metrics == {"output_chars": 321}


def test_uninitialized_read_only_restore_does_not_create_any_parent_or_child_state(store):
    restored = efficiency.restore("alice", "parent", 12.5, child_id="a")
    assert restored["request_count"] == 0 and restored["revision"] == 0
    with store() as db:
        assert db.query(ChatContextEfficiencyState).count() == 0
        assert db.query(ChatSubagentEvent).count() == 0
        assert db.get(ChatSubagentRun, "a").metrics == {"output_chars": 321}


def test_child_mutations_need_lease_and_parallel_updates_preserve_counts(store):
    with pytest.raises(children.ChildLeaseLost):
        record("a")
    with pytest.raises(children.ChildLeaseLost):
        efficiency.record_provider_request("alice", "parent", 12.5, 5, child_id="a")

    def request(tokens):
        with lease("a"):
            efficiency.record_provider_request("alice", "parent", 12.5, tokens, child_id="a")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(request, range(100, 108)))
    restored = efficiency.restore("alice", "parent", 12.5, child_id="a")
    assert restored["request_count"] == 8
    assert restored["revision"] == 8
    with store() as db:
        assert db.get(ChatSubagentRun, "a").metrics["output_chars"] == 321
        assert db.get(ChatSubagentRun, "b").metrics == {"output_chars": 321}


def test_oversized_child_state_fails_atomically_without_erasing_checkpoint(store):
    with lease("a"):
        saved = record("a")
        efficiency.record_provider_request("alice", "parent", 12.5, 100, child_id="a")
        before = efficiency.restore("alice", "parent", 12.5, child_id="a")
        with pytest.raises(ValueError, match="durable limit"):
            ledger.record("alice", "parent", 2, child_id="a", ledger_hash="hash",
                          before_tokens=100, after_tokens=10, economics={"oversized": "x" * 70000})
        with pytest.raises(ValueError, match="durable limit"):
            efficiency.record_boundary("alice", "parent", 12.5, [{"text": "x" * 70000}],
                                       None, child_id="a")
    assert ledger.pending("alice", "parent", child_id="a")["id"] == saved["id"]
    assert efficiency.restore("alice", "parent", 12.5, child_id="a") == before
    with store() as db:
        assert db.query(ChatSubagentEvent).filter_by(child_id="a").count() == 2
