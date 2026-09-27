"""Two executors share a real DB; only the current lease may publish or act."""
import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, ChatSubagentEvent, ChatSubagentRun, ChatToolIntent, ChatWorkEvent, Session
from src import subagent_runtime as children


@pytest.fixture
def workers(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(children, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(Session(id="qa", owner="alice", name="QA", endpoint_url="http://fixture", model="fixture"))
        db.flush()
        db.add(ChatSubagentRun(
            id="child", parent_session_id="qa", owner="alice", ordinal=1,
            objective="Compute a harmless result", assigned_context="", name="Worker",
            model="fixture", endpoint_id="fixture", status="queued", slot=1,
            heartbeat_at=children._utcnow(),
        ))
    yield children.SubagentRuntime(), children.SubagentRuntime(), factory
    engine.dispose()


def start(worker):
    return asyncio.create_task(worker._run_child(
        child_id="child", owner="alice", session_id="qa", endpoint_url="http://fixture",
        model="fixture", headers={}, timeout_seconds=5, workspace=None, access_mode="default",
    ))


def frame(kind=None, **payload):
    return "data: " + json.dumps(({"type": kind} if kind else {}) | payload) + "\n\n"


def test_new_worker_does_not_interrupt_healthy_foreign_executor(workers, monkeypatch):
    first, second, db_factory = workers

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(*args, **kwargs):
            entered.set()
            await release.wait()
            yield frame(delta="Verified 42")
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = start(first)
        await entered.wait()
        assert second.recover_stale() == 0
        row = second.list("alice", "qa")[0]
        assert row["status"] == "running"
        release.set()
        await task
        assert second.get("alice", "qa", "child")["result"] == "Verified 42"
        assert second.get("alice", "qa", "child")["status"] == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["expire", "transfer"])
def test_fenced_executor_cannot_publish_or_begin_next_tool(workers, monkeypatch, transition):
    first, second, factory = workers
    effects = []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(*args, **kwargs):
            yield frame(delta="Retained work")
            yield frame("context_checkpoint", messages=[{"role":"user", "content":"QA"}])
            entered.set()
            await release.wait()
            yield frame("tool_start", tool="python", command="print(42)")
            effects.append("must not execute")
            yield frame(delta="stale result")
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = start(first)
        await entered.wait()
        with factory.begin() as db:
            row = db.get(ChatSubagentRun, "child")
            if transition == "expire":
                row.heartbeat_at = children._utcnow() - timedelta(seconds=91)
            else:
                row.worker_id = "successor-lease"
                row.result = "Successor owns this"
        if transition == "expire":
            assert second.recover_stale() == 1
            assert second.recover_stale() == 0
        release.set()
        await task
        row = second.get("alice", "qa", "child")
        assert effects == []
        assert row["status"] == ("interrupted" if transition == "expire" else "running")
        assert row["result"] == ("Retained work" if transition == "expire" else "Successor owns this")
        with factory() as db:
            assert db.query(ChatSubagentEvent).filter_by(kind="tool_start").count() == 0
            if transition == "expire":
                events = db.query(ChatSubagentEvent).filter_by(kind="status").all()
                assert sum(e.payload.get("reason") == "worker_lease_expired" for e in events) == 1
    asyncio.run(scenario())


def test_stop_on_other_worker_retains_slot_until_executor_acknowledges(workers, monkeypatch):
    first, second, factory = workers
    effects = []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(*args, **kwargs):
            yield frame(delta="Partial evidence")
            entered.set()
            await release.wait()
            yield frame("tool_start", tool="python", command="print(42)")
            effects.append("must not execute")
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = start(first)
        await entered.wait()
        stopped = await second.stop("alice", "qa", "child")
        assert stopped["pending_stop"] and stopped["status"] == "stopping"
        with factory() as db:
            assert db.get(ChatSubagentRun, "child").slot == 1
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        row = first.get("alice", "qa", "child")
        assert row["status"] == "cancelled" and row["result"] == "Partial evidence"
        assert effects == []
        with factory() as db:
            assert db.get(ChatSubagentRun, "child").slot is None
    asyncio.run(scenario())


def test_cancelled_or_wrong_owner_child_never_dispatches(workers, monkeypatch):
    first, second, factory = workers
    async def forbidden(*args, **kwargs):
        pytest.fail("No request may leave a cancelled child")
        yield ""
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", forbidden)
    with factory.begin() as db:
        db.get(ChatSubagentRun, "child").cancel_requested = True
    async def scenario():
        await start(first)
    asyncio.run(scenario())
    assert second.get("mallory", "qa", "child") is None


def test_durable_question_is_not_a_dead_worker_lease(workers):
    _, second, factory = workers
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "child")
        row.status = "waiting_user"
        row.worker_id = "previous-worker"
        row.heartbeat_at = children._utcnow() - timedelta(hours=1)
    assert second.recover_stale() == 0
    assert second.get("alice", "qa", "child")["status"] == "waiting_user"


def test_cross_worker_stop_cancels_blocked_model_request(workers, monkeypatch):
    first, second, factory = workers
    monkeypatch.setattr(children, "CHILD_HEARTBEAT_SECONDS", 0.01)
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def stream(*args, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield frame(delta="unreachable")
            finally:
                closed.set()
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = start(first)
        await entered.wait()
        assert (await second.stop("alice", "qa", "child"))["pending_stop"]
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert closed.is_set()
        assert first.get("alice", "qa", "child")["status"] == "cancelled"
    asyncio.run(scenario())


def test_lease_covers_setup_before_model_stream_starts(workers, monkeypatch):
    first, second, _ = workers
    monkeypatch.setattr(children, "CHILD_HEARTBEAT_SECONDS", 0.01)
    async def scenario():
        entered = asyncio.Event()
        async def preparing(**kwargs):
            entered.set()
            await asyncio.Event().wait()  # e.g. attachment preparation
        monkeypatch.setattr(first, "_run_claimed_child", preparing)
        task = start(first)
        await entered.wait()
        assert (await second.stop("alice", "qa", "child"))["pending_stop"]
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert second.get("alice", "qa", "child")["status"] == "cancelled"
    asyncio.run(scenario())


def test_transient_heartbeat_write_failure_does_not_abandon_renewal(workers, monkeypatch):
    first, second, _ = workers
    monkeypatch.setattr(children, "CHILD_HEARTBEAT_SECONDS", 0.01)
    async def scenario():
        renewed = asyncio.Event()
        original = first._update
        attempts = []
        def update(child_id, owner, **changes):
            if set(changes) == {"heartbeat_at"}:
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("Synthetic transient storage failure")
                result = original(child_id, owner, **changes)
                renewed.set()
                return result
            return original(child_id, owner, **changes)
        monkeypatch.setattr(first, "_update", update)
        async def stream(*args, **kwargs):
            await renewed.wait()
            yield frame(delta="Verified result")
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        await asyncio.wait_for(start(first), 2)
        assert len(attempts) >= 2
        assert second.get("alice", "qa", "child")["status"] == "completed"
    asyncio.run(scenario())


def test_wrong_owner_cannot_claim_execution(workers, monkeypatch):
    first, _, factory = workers
    async def forbidden(*args, **kwargs):
        pytest.fail("Owner mismatch must be rejected before model dispatch")
        yield ""
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", forbidden)
    asyncio.run(first._run_child(
        child_id="child", owner="mallory", session_id="qa", endpoint_url="http://fixture",
        model="fixture", headers={}, timeout_seconds=5, workspace=None, access_mode="default",
    ))
    with factory() as db:
        assert db.get(ChatSubagentRun, "child").worker_id is None
        assert db.get(ChatSubagentRun, "child").status == "queued"


@pytest.mark.parametrize("waiting", [False, True])
def test_stop_wins_over_late_answer_or_question(workers, monkeypatch, waiting):
    first, second, factory = workers
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def stream(*args, **kwargs):
            entered.set()
            await release.wait()
            yield frame(delta="Late answer remains partial evidence")
            if waiting:
                yield frame("ask_user", data={"question": "Which option?"})
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = start(first)
        await entered.wait()
        await second.stop("alice", "qa", "child")
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert second.get("alice", "qa", "child")["status"] == "cancelled"
        with factory() as db:
            events = db.query(ChatSubagentEvent).filter_by(kind="status").all()
            assert not any(e.payload.get("status") in ("completed", "waiting_user") for e in events)
    asyncio.run(scenario())


def test_rejected_claim_finalizer_cannot_fail_healthy_successor(workers):
    first, second, factory = workers
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "child")
        row.status, row.worker_id, row.result = "running", "successor", "Successor evidence"
    async def scenario():
        task = start(first)
        first._tasks["child"] = task
        await task
        await first._finalize_unexpected("child", "alice", task)
        row = second.get("alice", "qa", "child")
        assert row["status"] == "running" and row["result"] == "Successor evidence"
    asyncio.run(scenario())


def test_prestart_cancellation_only_releases_own_reservation(workers):
    first, second, factory = workers
    with factory.begin() as db:
        db.get(ChatSubagentRun, "child").worker_id = first._worker_id
    async def scenario():
        task = start(first)
        first._tasks["child"] = task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await first._finalize_unexpected("child", "alice", task)
        assert second.get("alice", "qa", "child")["status"] == "cancelled"
        with factory() as db:
            assert db.get(ChatSubagentRun, "child").slot is None
    asyncio.run(scenario())


@pytest.mark.parametrize("transition", ["stop", "successor"])
def test_waiting_resume_cas_respects_concurrent_stop_or_successor(workers, monkeypatch, transition):
    first, second, factory = workers
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "child")
        row.status, row.worker_id = "waiting_user", "old-lease"
    first._configs["child"] = dict(endpoint_url="http://fixture", model="fixture", headers={},
                                     timeout_seconds=5, workspace=None, access_mode="default")
    def interleaved_checkpoint(*args):
        with factory.begin() as db:
            row = db.get(ChatSubagentRun, "child")
            row.revision += 1
            if transition == "stop":
                row.status, row.cancel_requested, row.slot = "cancelled", True, None
            else:
                row.status, row.worker_id = "running", "successor"
        return None
    monkeypatch.setattr(first, "_continuation_checkpoint", interleaved_checkpoint)
    reply = asyncio.run(first.message("alice", "qa", "child", "Use option A"))
    assert reply["exit_code"] == 1 and reply["policy"] == "stale_revision"
    assert not first._tasks
    assert second.get("alice", "qa", "child")["status"] == (
        "cancelled" if transition == "stop" else "running")


def test_expired_child_promotes_only_its_pending_effects_before_delivery(workers, monkeypatch):
    _, second, factory = workers
    from src import chat_effect_inbox
    monkeypatch.setattr(chat_effect_inbox, "SessionLocal", factory)
    with factory.begin() as db:
        child = db.get(ChatSubagentRun, "child")
        child.worker_id = "dead-worker"
        child.heartbeat_at = children._utcnow() - timedelta(seconds=91)
        for ident, owner, run_id, status in (
            ("pending", "alice", "child", "intent"),
            ("finished", "alice", "child", "succeeded"),
            ("foreign", "bob", "child", "intent"),
            ("other-run", "alice", "other", "intent"),
        ):
            db.add(ChatToolIntent(id=ident, owner=owner, session_id="qa", run_id=run_id,
                                 tool_call_id=ident, tool_name="write_file", action_hash="hash",
                                 status=status, revision=1))
    assert second.recover_stale() == 1
    assert second.get("alice", "qa", "child")["status"] == "interrupted"
    unknown = chat_effect_inbox.inbox.unknown("alice", "qa")
    assert [x["id"] for x in unknown] == ["pending"]
    with factory() as db:
        assert db.get(ChatToolIntent, "pending").revision == 2
        assert db.get(ChatToolIntent, "finished").status == "succeeded"
        assert db.get(ChatToolIntent, "foreign").status == "intent"
        assert db.get(ChatToolIntent, "other-run").status == "intent"
        assert db.query(ChatWorkEvent).filter_by(kind="effect_unknown").count() == 1
    assert second.recover_stale() == 0


def test_background_sweep_recovers_before_notifying_parent(workers, monkeypatch):
    _, second, factory = workers
    from src import bg_monitor, subagent_delivery, chat_work_store
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "child")
        row.worker_id = "expired"
        row.heartbeat_at = children._utcnow() - timedelta(seconds=91)
    monkeypatch.setattr(children, "runtime", second)
    observed = []
    def backfill():
        assert second.get("alice", "qa", "child")["status"] == "interrupted"
        observed.append("backfill")
        return [("alice", "qa")]
    async def dispatch(owner, session):
        assert (owner, session) == ("alice", "qa")
        observed.append("dispatch")
    def end_tick():
        raise asyncio.CancelledError()
    monkeypatch.setattr(subagent_delivery, "backfill_terminal_deliveries", backfill)
    monkeypatch.setattr(subagent_delivery, "dispatch_if_idle", dispatch)
    monkeypatch.setattr(chat_work_store.store, "expired_goal_questions", end_tick)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bg_monitor._loop())
    assert observed == ["backfill", "dispatch"]


def test_stop_closes_provider_before_releasing_model_slot(workers, monkeypatch):
    first, second, factory = workers
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        closing, closed = asyncio.Event(), asyncio.Event()
        async def stream(*args, **kwargs):
            try:
                entered.set()
                await release.wait()
                yield frame("tool_start", tool="python", command="print(42)")
                pytest.fail("Stopped tool must not execute")
            finally:
                closing.set()
                await closed.wait()
        monkeypatch.setattr("src.agent_loop.stream_agent_loop", stream)
        task = start(first)
        await entered.wait()
        await second.stop("alice", "qa", "child")
        release.set()
        await asyncio.wait_for(closing.wait(), 2)
        try:
            assert not task.done()
            with factory() as db:
                row = db.get(ChatSubagentRun, "child")
                assert row.status == "stopping" and row.slot == 1
        finally:
            closed.set()
            await asyncio.gather(task, return_exceptions=True)
        assert second.get("alice", "qa", "child")["status"] == "cancelled"
    asyncio.run(scenario())
