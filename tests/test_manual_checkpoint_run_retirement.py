from types import SimpleNamespace

from src import agent_runs


def test_manual_checkpoint_retires_only_stale_run_ledger(monkeypatch):
    from core import database

    row = SimpleNamespace(
        run_id="run-old", continuation={
            "working_checkpoint": {"messages": [{"role": "user", "content": "old"}]},
            "route_revision": "route-1", "tool_inventory_revision": "tools-1",
        },
    )
    run = SimpleNamespace(
        run_id="run-old", status="done", continuation=dict(row.continuation),
    )
    monkeypatch.setitem(agent_runs._RUNS, "chat-manual", run)

    class FakeQuery:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def first(self):
            return row

    class FakeDb:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def query(self, _model):
            return FakeQuery()

        def commit(self):
            self.committed = True

    db = FakeDb()
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    assert agent_runs.invalidate_working_checkpoint_after_manual_compaction("chat-manual")
    assert db.committed
    assert "working_checkpoint" not in row.continuation
    assert "working_checkpoint" not in run.continuation
    assert row.continuation["route_revision"] == "route-1"
    assert row.continuation["tool_inventory_revision"] == "tools-1"


def test_manual_checkpoint_does_not_retire_running_ledger(monkeypatch):
    run = SimpleNamespace(run_id="run-active", status="running", continuation={
        "working_checkpoint": {"messages": [{"role": "user", "content": "live"}]},
    })
    monkeypatch.setitem(agent_runs._RUNS, "chat-active", run)
    assert not agent_runs.invalidate_working_checkpoint_after_manual_compaction("chat-active")
    assert "working_checkpoint" in run.continuation
