"""Critical read-then-write checkpoints must wait before taking a SQLite snapshot."""

import threading
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import reserve_sqlite_writer
from src import agent_runs


def test_competing_writer_waits_before_read_snapshot(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'writers.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE records (id INTEGER PRIMARY KEY)"))
    local = sessionmaker(bind=engine)
    attempted = threading.Event()
    reserved = threading.Event()
    errors = []

    def second_writer():
        try:
            with local.begin() as db:
                attempted.set()
                reserve_sqlite_writer(db)
                reserved.set()
                db.execute(text("SELECT COUNT(*) FROM records")).scalar()
                db.execute(text("INSERT INTO records (id) VALUES (2)"))
        except Exception as exc:
            errors.append(exc)

    with local.begin() as first:
        reserve_sqlite_writer(first)
        first.execute(text("INSERT INTO records (id) VALUES (1)"))
        worker = threading.Thread(target=second_writer)
        worker.start()
        assert attempted.wait(1)
        assert not reserved.wait(0.05)
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert not errors
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM records ORDER BY id")).scalars().all() == [1, 2]


def test_writer_reservation_is_noop_for_non_sqlite():
    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, *_args):
            raise AssertionError("must not issue SQLite SQL on PostgreSQL")

    reserve_sqlite_writer(FakeDb())


def test_concurrent_effect_intent_has_one_durable_identity(tmp_path, monkeypatch):
    from core.database import Session, ChatToolIntent
    from src import chat_effect_inbox

    engine = create_engine(f"sqlite:///{tmp_path / 'intents.db'}")
    for table in (Session.__table__, ChatToolIntent.__table__):
        table.create(engine)
    local = sessionmaker(bind=engine)
    with local.begin() as db:
        db.add(Session(id="safe", name="safe", endpoint_url="http://example.invalid/v1",
                       model="fixture", owner="alice"))
    monkeypatch.setattr(chat_effect_inbox, "SessionLocal", local)
    gate = threading.Barrier(2)
    results = []
    errors = []

    def record():
        try:
            gate.wait(timeout=2)
            results.append(chat_effect_inbox.inbox.record_intent(
                "alice", "safe", "a" * 32, "call-1", "bash", '{"command":"echo safe"}',
            ))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=record) for _ in range(2)]
    for worker in threads:
        worker.start()
    for worker in threads:
        worker.join(timeout=3)
    assert all(not worker.is_alive() for worker in threads)
    assert not errors
    assert len(results) == 2
    assert {row["id"] for row in results} == {results[0]["id"]}
    assert sorted(row["created"] for row in results) == [False, True]
    with local() as db:
        assert db.query(ChatToolIntent).count() == 1


def test_failed_checkpoint_does_not_claim_new_durable_cursor(monkeypatch):
    import core.database as database

    class BrokenFactory:
        def begin(self):
            raise RuntimeError("synthetic unavailable database")

    monkeypatch.setattr(database, "SessionLocal", BrokenFactory())
    run = agent_runs._Run()
    run.session_id = "safe-fixture"
    run.buffer.extend(["frame-0", "frame-1"])
    assert run.durable_seq == -1
    agent_runs._persist_run_state(run, status="stopped", durable=True)
    assert run.durable_seq == -1
    assert run.terminal_at is None
