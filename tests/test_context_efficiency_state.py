from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Session
from src import context_efficiency_state as state


def _store(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    store = sessionmaker(bind=engine)
    monkeypatch.setattr(state, "SessionLocal", store)
    db = store(); db.add(Session(id="s", name="n", endpoint_url="u", model="m", owner="a")); db.commit(); db.close()
    return store


def test_ratio_is_fixed_and_state_survives_boundaries_and_compaction(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    first = state.restore("a", "s", 12.5)
    assert first["cache_write_read_ratio"] == 12.5
    assert state.restore("a", "s", 99)["cache_write_read_ratio"] == 12.5
    state.record_provider_request("a", "s", 12.5, 100)
    state.record_provider_request("a", "s", 12.5, 140)
    boundary = state.record_boundary("a", "s", 12.5, [{"id": "p2", "status": "pending"}], {"step_id": "p1"})
    assert boundary["completed_boundary_request_counts"] == [2]
    assert boundary["positive_context_delta_total"] == 40
    compacted = state.record_compaction("a", "s", 12.5, 1150, 900)
    assert compacted["native_compaction_count"] == 1 and compacted["plan"] == []
    assert compacted["cache_debt_tokens"] == 1150
    corrected = state.record_correction("a", "s", 12.5)
    assert corrected["epoch"] == compacted["epoch"] + 1
    assert corrected["completed_boundary_request_counts"] == []
    assert corrected["cache_debt_tokens"] == 0
    assert corrected["request_count"] == compacted["request_count"]


def test_request_updates_use_cas_without_lost_counts(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    state.restore("a", "s", 12.5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: state.record_provider_request("a", "s", 12.5, value), range(100, 108)))
    assert state.restore("a", "s", 12.5)["request_count"] == 8
