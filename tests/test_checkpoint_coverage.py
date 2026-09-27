"""Ordinary Agent follow-ups must reuse only proven, owner-bound coverage."""
import asyncio
import copy
from datetime import datetime, timedelta
import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import database
from core.models import ChatMessage, Session
from routes.chat_routes import _restore_ordinary_checkpoint_messages
from routes.chat_routes import _prepare_stream_messages
from src import agent_runs
from src.attachment_refs import persistable_message_content
from src.checkpoint_coverage import capture_source, restore_messages


@pytest.fixture
def history(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine, tables=[
        database.Session.__table__, database.ChatMessage.__table__,
        database.ChatRunState.__table__,
    ])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_schedule_evict", lambda *_args: None)
    monkeypatch.delenv("ODYSSEUS_DURABLE_CHAT_REPLAY", raising=False)
    session = Session("chat", "Chat", "http://local", "model", owner="alice")
    with factory.begin() as db:
        db.add(database.Session(id=session.id, name="Chat", endpoint_url="http://local",
                                model="model", owner="alice"))
    tick = 0

    def add(role, content, metadata=None, *, live=True, message_id=None, timestamp=None):
        nonlocal tick
        tick += 1
        mid = message_id or f"message-{tick:03}"
        with factory.begin() as db:
            db.add(database.ChatMessage(
                id=mid, session_id=session.id, role=role,
                content=persistable_message_content(content, metadata),
                meta_data=json.dumps(metadata or {}),
                timestamp=timestamp or datetime(2026, 1, 1) + timedelta(seconds=tick),
            ))
        if live:
            session.history.append(ChatMessage(role, content, {**(metadata or {}), "_db_id": mid}))
        return mid

    add("user", "original Goal")
    add("assistant", "old bulky transcript " * 10000)
    add("user", "complete the Goal")
    yield SimpleNamespace(session=session, db=factory, add=add)
    engine.dispose()


def _frame(value):
    return "data: " + json.dumps(value) + "\n\n"


def _complete(history, *, during=None, checkpoint=True, wrong_anchor=False, after_checkpoint=()):
    source = capture_source(history.session, "alice")
    assert source is not None

    async def stream():
        if checkpoint:
            yield _frame({"type": "context_checkpoint", "messages": [
                {"role": "user", "content": "compact verified working state",
                 "_agent_working_summary": True,
                 "metadata": {"trusted": False, "source": "working summary"}},
            ], "compactions": 4})
        if during:
            during()
        for event in after_checkpoint:
            yield _frame(event)
        yield _frame({"delta": "Goal completed."})
        saved = history.add("assistant", "Goal completed.")
        yield _frame({"type": "message_saved", "id": "wrong-anchor" if wrong_anchor else saved})
        yield "data: [DONE]\n\n"

    async def run():
        result = agent_runs.start("chat", stream(), owner="alice",
                                  continuation={"checkpoint_source": source, "goal": True})
        await result.task
        return result

    return asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
def test_completed_goal_ordinary_followup_keeps_small_ledger_and_live_image(history, restart):
    run = _complete(history)
    checkpoint = run.continuation["working_checkpoint"]
    assert checkpoint["coverage"]["run_id"] == run.run_id
    with history.db() as db:
        saved = db.get(database.ChatRunState, run.run_id)
        assert saved.continuation["working_checkpoint"] == checkpoint
    if restart:
        agent_runs._RUNS.clear()
        # Rehydrate transcript from persisted media-safe rows, as after restart.
        with history.db() as db:
            rows = db.query(database.ChatMessage).order_by(
                database.ChatMessage.timestamp, database.ChatMessage.id).all()
            history.session.history = [ChatMessage(row.role, row.content,
                {**json.loads(row.meta_data), "_db_id": row.id}) for row in rows]
    history.add("user", "intervening instruction")
    history.add("assistant", "intervening reply")
    image = [{"type": "text", "text": "inspect this image"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    history.add("user", image)
    restored = agent_runs.covered_checkpoint_messages(history.session, "alice")
    assert [item["content"] for item in restored] == [
        "compact verified working state", "Goal completed.",
        "intervening instruction", "intervening reply", image,
    ]
    assert len(json.dumps(restored)) < 2000
    project = {"role": "user", "content": "fresh project context",
               "metadata": {"trusted": False, "source": "project memory and skills"}}
    ctx = SimpleNamespace(preface=[{"role": "system", "content": "fresh runtime"}],
                          messages=[], route_messages=[{}, project])
    _restore_ordinary_checkpoint_messages(ctx, restored)
    assert ctx.messages == ctx.route_messages
    assert ctx.messages[:2] == [ctx.preface[0], project]
    assert sum(item["content"] == image for item in ctx.messages) == 1
    assert ctx.messages is not ctx.route_messages


@pytest.mark.parametrize("change", ["edit", "delete", "anchor_delete", "anchor_edit",
                                  "owner", "manual_compaction", "wrong_run", "ledger_edit"])
def test_coverage_refuses_changed_history_or_identity(history, change):
    run = _complete(history)
    checkpoint = copy.deepcopy(run.continuation["working_checkpoint"])
    with history.db.begin() as db:
        if change in {"edit", "delete", "anchor_delete", "anchor_edit"}:
            mid = checkpoint["coverage"]["anchor_id"] if change.startswith("anchor") else "message-001"
            row = db.get(database.ChatMessage, mid)
            if "delete" in change:
                db.delete(row)
            else:
                row.content = "changed"
        elif change == "owner":
            db.get(database.Session, "chat").owner = "bob"
        elif change == "manual_compaction":
            db.get(database.Session, "chat").context_checkpoint_count = 1
            db.get(database.Session, "chat").context_checkpoint = {"role": "system", "content": "new summary"}
        elif change == "wrong_run":
            checkpoint["coverage"]["run_id"] = "another-run"
        elif change == "ledger_edit":
            checkpoint["messages"][0]["content"] = "unverified replacement"
    assert restore_messages(history.session, "alice", run.run_id, checkpoint) is None
    assert agent_runs.covered_checkpoint_messages(history.session, "bob") is None


@pytest.mark.parametrize("change", ["edit", "guidance", "manual_compaction"])
def test_terminal_seal_refuses_concurrent_source_changes(history, change):
    def during():
        if change == "guidance":
            history.add("user", "unconsumed guidance", {"goal_guidance": True}, live=False)
        else:
            with history.db.begin() as db:
                if change == "edit":
                    db.get(database.ChatMessage, "message-001").content = "edited during run"
                else:
                    db.get(database.Session, "chat").context_checkpoint_count = 1
    run = _complete(history, during=during)
    assert "coverage" not in run.continuation["working_checkpoint"]
    assert agent_runs.covered_checkpoint_messages(history.session, "alice") is None


def test_no_seal_for_wrong_saved_anchor_or_inherited_checkpoint(history):
    wrong = _complete(history, wrong_anchor=True)
    assert "coverage" not in wrong.continuation["working_checkpoint"]
    inherited = _complete(history, checkpoint=False)
    assert "coverage" not in inherited.continuation["working_checkpoint"]
    assert agent_runs.covered_checkpoint_messages(history.session, "alice") is None


def test_capture_refuses_unconsumed_db_row_or_live_edit(history):
    history.session.history[0].content = "unsaved edit"
    assert capture_source(history.session, "alice") is None
    history.session.history[0].content = "original Goal"
    history.add("user", "concurrent guidance", live=False)
    assert capture_source(history.session, "alice") is None


def test_suffix_uses_timestamp_then_id_and_excludes_slash(history):
    _complete(history)
    stamp = datetime(2026, 1, 2)
    history.add("user", "second", message_id="b", timestamp=stamp, live=False)
    history.add("user", "first", message_id="a", timestamp=stamp, live=False)
    history.session.history += [ChatMessage("user", "first", {"_db_id": "a"}),
                                ChatMessage("user", "second", {"_db_id": "b"})]
    history.add("user", "/setup", {"source": "slash"}, timestamp=stamp + timedelta(seconds=1))
    restored = agent_runs.covered_checkpoint_messages(history.session, "alice")
    assert [item["content"] for item in restored][-2:] == ["first", "second"]


def test_stale_memory_cannot_hide_newer_unsealed_run(history):
    run = _complete(history)
    with history.db.begin() as db:
        db.add(database.ChatRunState(run_id="newer", session_id="chat", owner="alice",
            status="done", started_at=datetime.utcfromtimestamp(run.started_at) + timedelta(seconds=1),
            continuation={"working_checkpoint": run.continuation["working_checkpoint"]}))
    assert agent_runs.covered_checkpoint_messages(history.session, "alice") is None
    agent_runs._RUNS.clear()
    assert agent_runs.covered_checkpoint_messages(history.session, "alice") is None


def test_unserialized_legacy_system_summary_cannot_gain_coverage(history):
    checkpoint = ChatMessage("system", "legacy summary")
    history.session.context_checkpoint = checkpoint
    history.session.context_checkpoint_count = 2
    with history.db.begin() as db:
        row = db.get(database.Session, "chat")
        row.context_checkpoint = checkpoint.to_dict()
        row.context_checkpoint_count = 2
    assert capture_source(history.session, "alice") is None


@pytest.mark.parametrize("event", [
    {"type": "tool_start", "tool": "uncheckpointed-tool"},
    {"type": "agent_terminal", "data": {"failure": {"kind": "provider"}}},
    {"error": "failed provider request"},
])
def test_incomplete_or_failed_ledger_is_not_sealed(history, event):
    run = _complete(history, after_checkpoint=[event])
    assert "coverage" not in run.continuation["working_checkpoint"]
    assert agent_runs.covered_checkpoint_messages(history.session, "alice") is None


def test_stop_during_terminal_sealing_joins_worker_and_removes_coverage(history, monkeypatch):
    source = capture_source(history.session, "alice")
    entered = threading.Event()
    released = threading.Event()
    original = agent_runs._seal_terminal_checkpoint

    def held_seal(run, status):
        entered.set()
        assert released.wait(5)
        original(run, status)

    monkeypatch.setattr(agent_runs, "_seal_terminal_checkpoint", held_seal)

    async def stream():
        yield _frame({"type": "context_checkpoint", "messages": [
            {"role": "user", "content": "working summary"}], "compactions": 1})
        yield _frame({"delta": "done"})
        saved = history.add("assistant", "done")
        yield _frame({"type": "message_saved", "id": saved})

    async def exercise():
        run = agent_runs.start("chat", stream(), owner="alice",
                               continuation={"checkpoint_source": source})
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            run.task.cancel()
            await asyncio.sleep(0)
            assert not run.task.done()
        finally:
            released.set()
        await run.task
        assert run.status == "stopped"
        assert "coverage" not in run.continuation["working_checkpoint"]
        with history.db() as db:
            stored = db.get(database.ChatRunState, run.run_id)
            assert stored.status == "stopped"
            assert "coverage" not in stored.continuation["working_checkpoint"]

    asyncio.run(exercise())


def test_child_result_follows_restored_checkpoint_once_as_untrusted_evidence(history):
    _complete(history)
    restored = agent_runs.covered_checkpoint_messages(history.session, "alice")
    ctx = SimpleNamespace(preface=[{"role": "system", "content": "current permissions"}],
                          messages=[], route_messages=[])
    _restore_ordinary_checkpoint_messages(ctx, restored)
    result = _prepare_stream_messages(ctx.route_messages, "child result: verified output",
                                      child_delivery=True)
    assert len(result) == len(ctx.route_messages) + 1
    assert sum("child result: verified output" in str(item["content"]) for item in result) == 1
    assert result[-1]["metadata"]["trusted"] is False
    assert result[-1]["metadata"]["source"] == "child-agent results"
    assert result[0] == ctx.preface[0]
    assert "old bulky transcript" not in str(result)
