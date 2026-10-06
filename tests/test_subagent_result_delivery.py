import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (
    Base, ChatGoal, ChatRunState, ChatSubagentDelivery, ChatSubagentEvent,
    ChatSubagentRun, Session,
)
from core import database
from routes.chat_routes import (
    _child_delivery_checkpoint_frame, _prepare_stream_messages,
    _restore_goal_checkpoint_messages,
)
from src import agent_runs
from src import subagent_delivery as delivery
from src.prompt_security import untrusted_context_message


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


def test_failed_child_delivers_partial_work_and_checkpoint_reference(monkeypatch):
    import json
    factory = _store(monkeypatch)
    db = factory()
    child = db.get(ChatSubagentRun, "a" * 32)
    child.status = "failed"
    child.error = "Provider HTTP 500 after retries"
    child.result = "Implemented parser; 17 checks passed; exporter remains unfinished."
    child.metrics = {"checkpoint_hash": "abc123", "checkpoint_messages": 40,
                     "provider_retries": 10, "private_field": "must not propagate"}
    db.commit(); db.close()
    delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    summary = delivery.claimed_summary("alice", "s", token)
    payload = json.loads(summary.splitlines()[-1])
    assert payload["status"] == "failed"
    assert payload["partial_result"].startswith("Implemented parser")
    assert "HTTP 500" in payload["result_or_error"]
    assert payload["recovery"]["checkpoint_hash"] == "abc123"
    assert payload["recovery"]["provider_retries"] == 10
    assert "manage_subagents" in payload["inspection"]
    assert "private_field" not in summary


def test_failed_child_without_final_text_delivers_bounded_tool_progress(monkeypatch):
    import json
    factory = _store(monkeypatch)
    with factory.begin() as db:
        child = db.get(ChatSubagentRun, "a" * 32)
        child.status = "failed"
        child.error = "Provider failed after retries"
        child.result = ""
        for tool, exit_code, output in [
            ("read_file", 0, "secret source one"),
            ("bash", 0, "private test results"),
            ("python", 1, "private traceback"),
            ("bash", 0, "verification passed"),
            ("read_file", 0, "private final file"),
        ]:
            db.add(ChatSubagentEvent(
                child_id=child.id, parent_session_id="s", owner="alice",
                kind="tool_output", payload={"tool": tool, "exit_code": exit_code,
                                             "output": output, "command": "private command"},
            ))
        db.add(ChatSubagentEvent(
            child_id="b" * 32, parent_session_id="s", owner="alice",
            kind="tool_output", payload={"tool": "sibling_secret", "exit_code": 0},
        ))
        db.add(ChatSubagentEvent(
            child_id=child.id, parent_session_id="s", owner="mallory",
            kind="tool_output", payload={"tool": "foreign_owner_secret", "exit_code": 0},
        ))
    assert delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    summary = delivery.claimed_summary("alice", "s", token)
    payload = json.loads(summary.splitlines()[-1])
    assert payload["partial_result"] == ""
    assert [(item["tool"], item["exit_code"]) for item in payload["recent_tool_progress"]] == [
        ("bash", 0), ("python", 1), ("bash", 0), ("read_file", 0),
    ]
    assert all(item["output_chars"] > 0 for item in payload["recent_tool_progress"])
    assert "sibling_secret" not in summary
    assert "foreign_owner_secret" not in summary
    assert "private command" not in summary
    assert "private test results" not in summary


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


def test_single_user_child_and_run_owner_keys_still_match(monkeypatch):
    factory = _store(monkeypatch)
    with factory.begin() as db:
        db.get(ChatRunState, "c" * 32).owner = agent_runs._SINGLE_USER_OWNER_KEY
        db.get(ChatSubagentRun, "a" * 32).owner = ""
    assert delivery.enqueue_terminal("a" * 32, None)
    token = delivery.claim_pending(None, "s")
    assert token and len(token) == 32
    assert delivery._dispatch_allowed(None, "s") == (True, False)


def test_backfill_skips_delivered_history_but_revisits_pending(monkeypatch):
    factory = _store(monkeypatch)
    assert delivery.backfill_terminal_deliveries() == [("alice", "s")]
    assert delivery.backfill_terminal_deliveries() == [("alice", "s")]
    token = delivery.claim_pending("alice", "s")
    assert delivery.backfill_terminal_deliveries() == [("alice", "s")]
    delivery.mark_delivered("alice", "s", token, "r" * 32)
    assert delivery.backfill_terminal_deliveries() == []
    with factory() as db:
        assert db.query(ChatSubagentDelivery).count() == 2


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


def test_new_child_delivery_requires_model_visible_checkpoint_before_ack(monkeypatch):
    factory = _store(monkeypatch)
    assert delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    run_id = "r" * 32
    with factory.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id="s", owner="alice", status="running",
            continuation={"subagent_delivery_token": token,
                          "subagent_delivery_requires_checkpoint": True},
        ))
        db.get(ChatSubagentDelivery, "a" * 32).claimed_at = datetime.utcnow() - timedelta(minutes=3)
    assert delivery.mark_delivered("alice", "s", token, run_id, require_checkpoint=True) == 0
    assert delivery.claim_pending("alice", "s") is None
    with factory() as db:
        assert db.get(ChatSubagentDelivery, "a" * 32).status == "claimed"
    with factory.begin() as db:
        row = db.get(ChatRunState, run_id)
        row.continuation = {**row.continuation,
                            "working_checkpoint": {"checkpoint_run_id": run_id,
                                                   "messages": [{"role": "user", "content": "other checkpoint"}]}}
    assert delivery.mark_delivered("alice", "s", token, run_id, require_checkpoint=True) == 0
    with factory.begin() as db:
        row = db.get(ChatRunState, run_id)
        row.continuation = {**row.continuation,
                            "working_checkpoint": {"checkpoint_run_id": run_id,
                                                   "messages": [{"role": "user", "content": "child result",
                                                                 "metadata": {"source": "child-agent results",
                                                                              "trusted": False}}]}}
    assert delivery.mark_delivered("alice", "s", token, run_id, require_checkpoint=True) == 0
    with factory.begin() as db:
        row = db.get(ChatRunState, run_id)
        row.continuation = {**row.continuation,
                            "subagent_delivery_checkpointed_token": token}
    assert delivery.mark_checkpointed("alice", "s", token, run_id) == 1
    with factory() as db:
        assert db.get(ChatSubagentDelivery, "a" * 32).status == "in_run"
    assert delivery.mark_delivered("alice", "s", token, run_id, require_checkpoint=True) == 0
    with factory.begin() as db:
        db.get(ChatRunState, run_id).status = "done"
    assert delivery.mark_delivered("alice", "s", token, run_id, require_checkpoint=True) == 1
    with factory() as db:
        assert db.get(ChatSubagentDelivery, "a" * 32).status == "delivered"


def test_uncheckpointed_child_delivery_requeues_after_interrupted_run(monkeypatch):
    factory = _store(monkeypatch)
    # Match production SessionLocal: without an explicit flush, the recovered
    # pending row is invisible until the next monitor pass.
    factory.configure(autoflush=False)
    assert delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    run_id = "r" * 32
    with factory.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id="s", owner="alice", status="interrupted",
            continuation={"subagent_delivery_token": token,
                          "subagent_delivery_requires_checkpoint": True,
                          "subagent_delivery_checkpointed_token": token},
        ))
        claim = db.get(ChatSubagentDelivery, "a" * 32)
        claim.status = "in_run"
        claim.delivered_run_id = run_id
        # A restarted process has already marked this run interrupted; a
        # checkpointed result should not wait for the two-minute claim lease.
        claim.claimed_at = datetime.utcnow()
    replacement = delivery.claim_pending("alice", "s")
    assert replacement and replacement != token
    with factory() as db:
        row = db.get(ChatSubagentDelivery, "a" * 32)
        assert row.status == "claimed"
        assert row.delivered_run_id is None


def test_checkpointed_child_does_not_block_another_parallel_result(monkeypatch):
    factory = _store(monkeypatch)
    assert delivery.enqueue_terminal("a" * 32, "alice")
    first = delivery.claim_pending("alice", "s")
    run_id = "r" * 32
    with factory.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id="s", owner="alice", status="running",
            continuation={"subagent_delivery_token": first,
                          "subagent_delivery_requires_checkpoint": True,
                          "subagent_delivery_checkpointed_token": first},
        ))
    assert delivery.mark_checkpointed("alice", "s", first, run_id) == 1
    assert delivery.enqueue_terminal("b" * 32, "alice")
    second = delivery.claim_pending("alice", "s")
    assert second and second != first
    with factory() as db:
        assert db.get(ChatSubagentDelivery, "a" * 32).status == "in_run"
        assert db.get(ChatSubagentDelivery, "b" * 32).status == "claimed"
    with factory.begin() as db:
        run = db.get(ChatRunState, run_id)
        run.continuation = {**run.continuation,
                            "subagent_delivery_checkpointed_token": second}
    assert delivery.mark_checkpointed("mallory", "s", second, run_id) == 0
    assert delivery.mark_checkpointed("alice", "s", second, run_id) == 1
    with factory.begin() as db:
        db.get(ChatRunState, run_id).status = "done"
    assert delivery.finalize_run_deliveries("alice", "s", run_id, "done") == 2
    with factory() as db:
        assert {row.status for row in db.query(ChatSubagentDelivery).all()} == {"delivered"}


def test_failed_parent_requeues_provisional_child_result(monkeypatch):
    factory = _store(monkeypatch)
    assert delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    run_id = "r" * 32
    with factory.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id="s", owner="alice", status="running",
            continuation={"subagent_delivery_checkpointed_token": token,
                          "subagent_delivery_two_phase": True},
        ))
    assert delivery.mark_checkpointed("alice", "s", token, run_id) == 1
    with factory.begin() as db:
        db.get(ChatRunState, run_id).status = "error"
    assert delivery.finalize_run_deliveries("alice", "s", run_id, "error") == 1
    with factory() as db:
        row = db.get(ChatSubagentDelivery, "a" * 32)
        assert row.status == "pending"
        assert row.claim_token is None
        assert row.delivered_run_id is None


def test_parent_run_checkpoints_child_evidence_before_delivery_ack(monkeypatch):
    factory = _store(monkeypatch)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_schedule_evict", lambda *_args: None)
    monkeypatch.delenv("ODYSSEUS_DURABLE_CHAT_REPLAY", raising=False)
    assert delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    acknowledged = []

    async def stream():
        messages = _prepare_stream_messages([], "Finished child result: verified 42",
                                            child_delivery=True)
        yield _child_delivery_checkpoint_frame(messages, token)
        acknowledged.append(delivery.mark_checkpointed(
            "alice", "s", token, agent_runs.get_run_id("s"),
        ))
        assert delivery.mark_delivered(
            "alice", "s", token, agent_runs.get_run_id("s"), require_checkpoint=True,
        ) == 0
        yield "data: [DONE]\n\n"

    async def scenario():
        run = agent_runs.start(
            "s", stream(), owner="alice",
            continuation={"subagent_delivery_token": token,
                          "subagent_delivery_requires_checkpoint": True},
        )
        await run.task
        return run

    run = asyncio.run(scenario())
    assert run.status == "done"
    assert acknowledged == [1]
    assert all(token not in frame for frame in run.buffer)
    with factory() as db:
        saved = db.get(ChatRunState, run.run_id)
        checkpoint = saved.continuation["working_checkpoint"]
        assert checkpoint["checkpoint_run_id"] == run.run_id
        assert saved.continuation["subagent_delivery_checkpointed_token"] == token
        assert checkpoint["messages"][-1]["metadata"] == {
            "trusted": False, "source": "child-agent results", "tool_gate_untrusted": True,
        }
        assert "verified 42" in checkpoint["messages"][-1]["content"]
        assert db.get(ChatSubagentDelivery, "a" * 32).status == "delivered"


def test_restored_goal_seals_child_result_separately_from_control_message():
    control = "Continue the current Goal from its durable checkpoint"
    child_result = "Finished child result: partial verified work"
    preface = [{"role": "system", "content": "current policy"}]
    ctx = SimpleNamespace(
        preface=preface,
        messages=preface + [{"role": "user", "content": control}],
        route_messages=preface + [{"role": "user", "content": control}],
    )
    ledger = [{"role": "user", "content": "original objective"}] + [
        {"role": "assistant", "content": "prior verified progress " * 180}
        for _ in range(62)
    ]
    _restore_goal_checkpoint_messages(ctx, ledger)
    messages = _prepare_stream_messages(
        ctx.route_messages, control, child_delivery_content=child_result,
    )
    assert messages[-2]["content"] == control
    assert messages[-2].get("metadata") is None
    assert messages[-1]["metadata"]["source"] == "child-agent results"
    assert messages[-1]["metadata"]["trusted"] is False
    assert sum(child_result in str(item.get("content") or "") for item in messages) == 1
    frame = _child_delivery_checkpoint_frame(messages, "a" * 32)
    payload = json.loads(frame.removeprefix("data: "))
    assert payload["subagent_delivery_token"] == "a" * 32
    assert payload["messages"][-1]["metadata"]["source"] == "child-agent results"


def test_round_boundary_child_result_is_recoverable_until_parent_finishes(monkeypatch):
    factory = _store(monkeypatch)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_schedule_evict", lambda *_args: None)
    monkeypatch.delenv("ODYSSEUS_DURABLE_CHAT_REPLAY", raising=False)
    assert delivery.enqueue_terminal("a" * 32, "alice")
    token = delivery.claim_pending("alice", "s")
    accepted = []

    async def stream():
        messages = [untrusted_context_message("completed subagent results", "verified child work")]
        yield "data: " + json.dumps({
            "type": "context_checkpoint", "messages": messages,
            "subagent_delivery_token": token,
        }) + "\n\n"
        accepted.append(delivery.mark_checkpointed(
            "alice", "s", token, agent_runs.get_run_id("s"),
        ))
        yield "data: [DONE]\n\n"

    async def scenario():
        run = agent_runs.start("s", stream(), owner="alice")
        await run.task
        return run

    run = asyncio.run(scenario())
    assert run.status == "done"
    assert accepted == [1]
    with factory() as db:
        assert db.get(ChatSubagentDelivery, "a" * 32).status == "delivered"


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
        "workspace": "/tmp/owned-qa",
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
    assert sent[0][3]["workspace"] == "/tmp/owned-qa"
    assert len(sent[0][3]["subagent_delivery_token"]) == 32


def test_chat_route_fences_internal_result_and_does_not_save_fake_user_turn():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text()
    assert "Internal child continuation required" in source
    assert "claimed_summary" in source
    assert "and not subagent_continuation" in source
    assert "mark_checkpointed" in source
    assert "finalize_run_deliveries" in (Path(__file__).resolve().parents[1] / "src/agent_runs.py").read_text()
    assert "dispatch_if_idle" in source
