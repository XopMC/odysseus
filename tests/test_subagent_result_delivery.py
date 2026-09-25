import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (
    Base, ChatGoal, ChatRunState, ChatSubagentDelivery, ChatSubagentRun, Session,
)
from src import subagent_delivery as delivery


def _store(monkeypatch, *, goal_status=None):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(delivery, "SessionLocal", factory)
    db = factory()
    db.add(Session(id="s", name="QA", endpoint_url="http://model", model="m", owner="alice"))
    db.commit()
    db.add(ChatRunState(
        run_id="c" * 32, session_id="s", owner="alice", status="done",
        continuation={"goal": bool(goal_status), "allow_bash": False},
    ))
    if goal_status:
        db.add(ChatGoal(
            id="g", session_id="s", owner="alice", objective="QA", status=goal_status,
        ))
    for ordinal, child_id in enumerate(("a" * 32, "b" * 32), 1):
        db.add(ChatSubagentRun(
            id=child_id, parent_session_id="s", parent_run_id="c" * 32,
            owner="alice", ordinal=ordinal, name=f"Child {ordinal}",
            objective="safe QA", assigned_context="", model="worker",
            status="completed", result=f"verified result {ordinal}",
            policy_snapshot={"auto_delivery": True},
        ))
    db.commit(); db.close()
    return factory


def test_parallel_children_coalesce_into_one_claim_and_one_parent_run(monkeypatch):
    factory = _store(monkeypatch)
    assert delivery.enqueue_terminal("a" * 32, "alice") is True
    assert delivery.enqueue_terminal("a" * 32, "alice") is False
    assert delivery.enqueue_terminal("b" * 32, "alice") is True
    token = delivery.claim_pending("alice", "s", include_goal=False)
    assert len(token) == 32
    assert delivery.claim_pending("alice", "s", include_goal=False) is None
    summary = delivery.claimed_summary("alice", "s", token)
    assert "verified result 1" in summary and "verified result 2" in summary
    assert delivery.mark_delivered("alice", "s", token, "r" * 32) == 2
    assert delivery.claimed_summary("alice", "s", token) is None
    db = factory()
    assert {row.status for row in db.query(ChatSubagentDelivery).all()} == {"delivered"}
    assert {row.delivered_run_id for row in db.query(ChatSubagentDelivery).all()} == {"r" * 32}
    db.close()


def test_cross_owner_and_unattached_historical_child_cannot_dispatch(monkeypatch):
    factory = _store(monkeypatch)
    assert delivery.enqueue_terminal("a" * 32, "mallory") is False
    assert delivery.claim_pending("mallory", "s") is None
    db = factory()
    db.query(ChatSubagentRun).filter(ChatSubagentRun.id == "b" * 32).update(
        {ChatSubagentRun.policy_snapshot: {}}
    )
    db.commit(); db.close()
    assert delivery.enqueue_terminal("b" * 32, "alice") is False
    assert delivery.backfill_terminal_deliveries() == [("alice", "s")]
    db = factory()
    assert db.query(ChatSubagentDelivery).count() == 1
    db.close()


def test_expired_claim_requeues_only_if_no_durable_run_used_token(monkeypatch):
    factory = _store(monkeypatch)
    delivery.enqueue_terminal("a" * 32, "alice")
    first = delivery.claim_pending("alice", "s")
    db = factory()
    row = db.query(ChatSubagentDelivery).one()
    row.claimed_at = datetime.utcnow() - timedelta(minutes=3)
    db.commit(); db.close()
    second = delivery.claim_pending("alice", "s")
    assert second and second != first
    db = factory()
    db.add(ChatRunState(
        run_id="r" * 32, session_id="s", owner="alice", status="done",
        continuation={"subagent_delivery_token": second},
    ))
    row = db.query(ChatSubagentDelivery).one()
    row.claimed_at = datetime.utcnow() - timedelta(minutes=3)
    db.commit(); db.close()
    assert delivery.claim_pending("alice", "s") is None
    db = factory()
    assert db.query(ChatSubagentDelivery).one().status == "delivered"
    db.close()


def test_completed_goal_child_is_not_auto_dispatched_as_ordinary_agent(monkeypatch):
    _store(monkeypatch, goal_status="completed")
    delivery.enqueue_terminal("a" * 32, "alice")
    assert delivery._dispatch_allowed("alice", "s") == (False, False)
    assert delivery.claim_pending("alice", "s", include_goal=False) is None


def test_idle_parent_dispatch_uses_internal_unprivileged_continuation(monkeypatch):
    _store(monkeypatch)
    delivery.enqueue_terminal("a" * 32, "alice")
    monkeypatch.setattr("src.agent_runs.is_active", lambda _sid: False)
    monkeypatch.setattr("src.agent_runs.continuation_for_session", lambda _sid: {
        "allow_bash": False, "allow_web_search": False,
    })
    sent = []

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url, *, headers, data):
            sent.append((method, url, headers, data))
            return Response()

    monkeypatch.setattr(delivery.httpx, "AsyncClient", Client)
    assert asyncio.run(delivery.dispatch_if_idle("alice", "s")) is True
    assert len(sent) == 1
    assert sent[0][3]["subagent_continuation"] == "true"
    assert sent[0][3]["allow_bash"] == "false"
    assert sent[0][3]["allow_web_search"] == "false"
    assert len(sent[0][3]["subagent_delivery_token"]) == 32


def test_chat_route_fences_internal_result_and_does_not_save_fake_user_turn():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text()
    assert "Internal child continuation required" in source
    assert "claimed_summary" in source
    assert "and not subagent_continuation" in source
    assert "mark_delivered" in source
    assert "dispatch_if_idle" in source
