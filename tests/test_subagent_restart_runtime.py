"""Restart preserves the execution ledger, not old credentials or effect guesses."""
import asyncio
import copy
import json
from datetime import timedelta

from core.database import ChatSubagentRun, ChatSubagentEvent
from src import subagent_runtime as children
from tests.test_subagent_worker_fencing import workers, frame


def seed(factory, *, status="running", checkpoint=True):
    ledger = [{"role": "user", "content": "Original QA"},
              {"role": "assistant", "content": "Already verified fact"}]
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "child")
        row.status, row.worker_id = status, "dead-process"
        row.policy_snapshot = {"recovery_config": {"version": 1}}
        row.result = "Retained result"
        row.heartbeat_at = children._utcnow() - timedelta(seconds=91)
        row.guidance = [{"id": "old", "text": "already consumed"},
                        {"id": "new1", "text": "first pending"},
                        {"id": "new2", "text": "second pending"}]
        if checkpoint:
            db.add(ChatSubagentEvent(child_id="child", owner="alice", parent_session_id="qa",
                kind="context_checkpoint", payload={"messages": ledger, "guidance_ids": ["old"],
                    "compactions": 3, "consecutive_provider_failures": 0, "provider_retries": 2}))
    return ledger


def configure(monkeypatch, **overrides):
    config = dict(endpoint_url="http://fixture", model="fixture", headers={"Authorization": "fresh"},
                  workspace=None, access_mode="ask_important", timeout_seconds=600,
                  disabled_tools={"write_file"}, max_active_for_model=1)
    config.update(overrides)
    monkeypatch.setattr("src.subagent_recovery_config.prepare_config", lambda **kw: copy.deepcopy(config))
    monkeypatch.setattr("src.subagent_recovery_config.validate_recovery_seal", lambda *a, **kw: True)
    return config


def test_restart_reclaims_same_child_ledger_and_all_unconsumed_guidance(workers, monkeypatch):
    first, restarted, factory = workers
    ledger = seed(factory)
    configure(monkeypatch)
    calls = []
    async def stream(*args, **kwargs):
        calls.append((copy.deepcopy(args[2]), kwargs))
        yield frame("context_checkpoint", messages=args[2][1:], compactions=3)
        yield frame(delta="Fresh verified final")
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    async def scenario():
        assert restarted.recover_stale() == 1
        assert restarted.get("alice", "qa", "child")["status"] == "recovering"
        assert await restarted.resume_recovering() == 1
        await restarted._tasks["child"]
        row = restarted.get("alice", "qa", "child")
        assert row["status"] == "completed"
        assert "Retained result" in row["result"]
    asyncio.run(scenario())
    assert calls[0][0][1:] == ledger + [{"role":"user", "content":"first pending"},
                                       {"role":"user", "content":"second pending"}]
    assert calls[0][1]["headers"] == {"Authorization": "fresh"}
    assert calls[0][1]["initial_context_compactions"] == 3
    with factory() as db:
        cp = db.query(ChatSubagentEvent).filter_by(kind="context_checkpoint").order_by(ChatSubagentEvent.id.desc()).first()
        assert set(cp.payload["guidance_ids"]) == {"old", "new1", "new2"}
        assert db.query(ChatSubagentRun).count() == 1


def test_recovery_capacity_wait_and_stop_never_dispatch(workers, monkeypatch):
    first, restarted, factory = workers
    seed(factory)
    configure(monkeypatch)
    with factory.begin() as db:
        db.get(ChatSubagentRun, "child").slot = None
        db.flush()
        db.add(ChatSubagentRun(id="other", parent_session_id="qa", owner="alice", ordinal=2,
            objective="QA", name="Other", model="fixture", endpoint_id="fixture", status="running", slot=1,
            worker_id="live", heartbeat_at=children._utcnow()))
    async def scenario():
        restarted.recover_stale()
        assert await restarted.resume_recovering() == 0
        assert restarted.get("alice", "qa", "child")["status"] == "recovering"
        assert (await restarted.stop("alice", "qa", "child"))["status"] == "cancelled"
        assert await restarted.resume_recovering() == 0
    asyncio.run(scenario())


def test_restart_does_not_reset_provider_failure_budget(workers, monkeypatch):
    first, restarted, factory = workers
    seed(factory)
    configure(monkeypatch)
    restarted._event("child", "alice", "qa", "transport_retry",
                     {"consecutive_provider_failures": 10, "provider_retries": 12})
    calls = []
    async def stream(*args, **kwargs):
        calls.append(1)
        yield frame("agent_terminal", data={"failure": {"status": 500, "message": "provider failed"}})
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    async def scenario():
        restarted.recover_stale()
        assert await restarted.resume_recovering() == 1
        await restarted._tasks["child"]
        assert restarted.get("alice", "qa", "child")["status"] == "failed"
    asyncio.run(scenario())
    assert len(calls) == 1


def test_progress_checkpoint_resets_public_streak_but_keeps_lifetime_retries(workers, monkeypatch):
    _, restarted, factory = workers
    seed(factory)
    configure(monkeypatch)
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "child")
        row.metrics = {"consecutive_provider_failures": 7, "provider_retries": 17}
        cp = db.query(ChatSubagentEvent).filter_by(kind="context_checkpoint").first()
        cp.payload = {**cp.payload, "consecutive_provider_failures": 7, "provider_retries": 17}

    async def stream(*args, **kwargs):
        yield frame("context_checkpoint", messages=args[2][1:] + [
            {"role": "assistant", "content": "New verified progress"}], compactions=3)
        yield frame(delta="Verified final result")
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)

    async def scenario():
        assert restarted.recover_stale() == 1
        assert await restarted.resume_recovering() == 1
        await restarted._tasks["child"]
        metrics = restarted.get("alice", "qa", "child")["metrics"]
        assert metrics["consecutive_provider_failures"] == 0
        assert metrics["provider_retries"] == 17
        assert restarted.list("alice", "qa")[0]["metrics"]["consecutive_provider_failures"] == 0
    asyncio.run(scenario())


def test_checkpoint_gap_fails_closed_and_keeps_work(workers, monkeypatch):
    first, restarted, factory = workers
    seed(factory)
    configure(monkeypatch)
    monkeypatch.setattr("src.subagent_delivery.SessionLocal", factory)
    async def no_dispatch(*args):
        return False
    monkeypatch.setattr("src.subagent_delivery.dispatch_if_idle", no_dispatch)
    restarted._event("child", "alice", "qa", "tool_start", {"tool": "write_file"})
    async def scenario():
        restarted.recover_stale()
        assert await restarted.resume_recovering() == 0
        row = restarted.get("alice", "qa", "child")
        assert row["status"] == "interrupted"
        assert row["result"] == "Retained result"
    asyncio.run(scenario())


def test_changed_authority_seal_does_not_claim_or_dispatch(workers, monkeypatch):
    _, restarted, factory = workers
    seed(factory)
    configure(monkeypatch)
    monkeypatch.setattr("src.subagent_recovery_config.validate_recovery_seal", lambda *a, **kw: False)
    async def scenario():
        restarted.recover_stale()
        assert await restarted.resume_recovering() == 0
        assert restarted.get("alice", "qa", "child")["status"] == "recovering"
        assert not restarted._tasks
    asyncio.run(scenario())


def test_waiting_answer_after_restart_preserves_identity(workers, monkeypatch):
    _, restarted, factory = workers
    seed(factory, status="waiting_user")
    configure(monkeypatch)
    inputs = []
    async def stream(*args, **kwargs):
        inputs.extend(args[2])
        yield frame(delta="Completed with answer")
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
    async def scenario():
        assert restarted.recover_stale() == 0
        assert (await restarted.message("alice", "qa", "child", "new answer"))["exit_code"] == 0
        await restarted._tasks["child"]
        assert restarted.get("alice", "qa", "child")["status"] == "completed"
    asyncio.run(scenario())
    assert [m["content"] for m in inputs].count("new answer") == 1


def test_stop_during_fresh_auth_preparation_cannot_restart(workers, monkeypatch):
    _, restarted, factory = workers
    seed(factory)
    config = configure(monkeypatch)
    def revoke(**kwargs):
        with factory.begin() as db:
            row = db.get(ChatSubagentRun, "child")
            row.status, row.cancel_requested = "cancelled", True
            row.revision += 1
        return config
    monkeypatch.setattr("src.subagent_recovery_config.prepare_config", revoke)
    async def scenario():
        restarted.recover_stale()
        assert await restarted.resume_recovering() == 0
        assert not restarted._tasks
        assert restarted.get("alice", "qa", "child")["status"] == "cancelled"
    asyncio.run(scenario())
