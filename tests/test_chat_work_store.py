import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import uuid

import pytest

from core.database import ChatMessage, Session, SessionLocal
from src import agent_runs
from src.agent_tools import ToolBlock
from src.chat_work_store import ChatWorkStore, WorkConflict, WorkNotFound
from src.chat_work_store import checklist_steps
import src.agent_loop as agent_loop
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block


@pytest.fixture
def owned_chat():
    session_id = "work-" + uuid.uuid4().hex
    with SessionLocal.begin() as db:
        db.add(Session(
            id=session_id, name="Work", endpoint_url="http://model.test/v1",
            model="test-model", owner="alice",
        ))
    try:
        yield session_id
    finally:
        with SessionLocal.begin() as db:
            row = db.query(Session).filter_by(id=session_id).first()
            if row is not None:
                db.delete(row)


def test_plan_goal_revision_lease_and_owner_isolation(owned_chat):
    store = ChatWorkStore()
    plan = store.save_plan("alice", owned_chat, "Release", [
        {"id": "verify", "text": "Run verification", "status": "pending", "required": True},
    ], expected_revision=0)
    with pytest.raises(WorkConflict):
        store.plan_action("alice", owned_chat, "execute", plan["revision"] + 1)
    plan = store.plan_action("alice", owned_chat, "execute", plan["revision"])
    assert plan["steps"][0]["status"] == "in_progress"
    plan = store.update_plan_step("alice", owned_chat, "verify", "done", expected_revision=plan["revision"],
                                  progress={"files_changed": ["src/a.py"],
                                            "verification": ["pytest: passed"],
                                            "decisions": ["kept API additive"],
                                            "next_work": ["deploy candidate"]})
    assert plan["status"] == "done"
    assert plan["steps"][0]["progress"] == {
        "files_changed": ["src/a.py"], "verification": ["pytest: passed"],
        "decisions": ["kept API additive"],
        "next_work": ["deploy candidate"],
    }

    goal = store.ensure_goal("alice", owned_chat, "Ship a verified release")
    store.update_goal("alice", owned_chat, "Tests are running", {"suite": "focused"})
    token = store.acquire_goal_lease("alice", owned_chat)
    assert token and store.acquire_goal_lease("alice", owned_chat) is None
    goal = store.consume_goal_lease("alice", owned_chat, token)
    assert goal["attempt"] == 2
    with pytest.raises(ValueError):
        store.complete_goal("alice", owned_chat, "Done", [])
    goal = store.complete_goal("alice", owned_chat, "Released", ["tests passed"])
    assert goal["status"] == "completed"
    assert store.events("alice", owned_chat)
    with pytest.raises(WorkNotFound):
        store.get("bob", owned_chat)


def test_single_user_goal_and_model_tools_accept_null_request_owner(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from core.database import Base, ChatGoal, ChatPlan, ChatWorkEvent, ChatMessage as DbChatMessage
    import src.chat_work_store as work_module
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Session.__table__, DbChatMessage.__table__, ChatPlan.__table__, ChatGoal.__table__, ChatWorkEvent.__table__])
    monkeypatch.setattr(work_module, "SessionLocal", sessionmaker(bind=engine, autocommit=False, autoflush=False))
    session_id = "single-work-" + uuid.uuid4().hex
    factory = work_module.SessionLocal
    with factory.begin() as db:
        db.add(Session(
            id=session_id, name="Single user work",
            endpoint_url="http://model.test/v1", model="test-model", owner=None,
        ))
    try:
        store = ChatWorkStore()
        goal = store.ensure_goal(None, session_id, "Finish the local goal")
        assert goal["owner"] is None
        _desc, result = asyncio.run(execute_tool_block(
            ToolBlock("update_goal_progress", json.dumps({
                "progress": "Stage one verified", "checkpoint": {"stage": 1},
            })),
            owner=None, session_id=session_id,
            security_context=NO_TOOL_SECURITY_CONTEXT,
        ))
        assert result["exit_code"] == 0
        assert store.get(None, session_id)["goal"]["checkpoint"]["stage"] == 1
    finally:
        engine.dispose()


def test_legacy_plan_steps_have_stable_ids_and_cancel_fence(owned_chat):
    first = checklist_steps("- [ ] Inspect source\n- [ ] Run tests")
    reordered = checklist_steps("- [ ] Run tests\n- [ ] Inspect source")
    assert first[0]["id"] != first[1]["id"]
    assert first[0]["id"].rsplit('-', 1)[0] == reordered[1]["id"].rsplit('-', 1)[0]
    work = ChatWorkStore()
    plan = work.save_plan("alice", owned_chat, "Release", "- [ ] Inspect source")
    plan = work.plan_action("alice", owned_chat, "cancel", plan["revision"])
    with pytest.raises(WorkConflict):
        work.update_plan_step("alice", owned_chat, plan["steps"][0]["id"], "done", expected_revision=plan["revision"])


@pytest.mark.parametrize("terminal_action", ["cancel", "complete"])
def test_create_plan_tool_replaces_terminal_plan_and_starts_it_for_active_goal(
    owned_chat, terminal_action,
):
    work = ChatWorkStore()
    plan = work.save_plan("alice", owned_chat, "Old", "- [ ] Old step")
    if terminal_action == "cancel":
        plan = work.plan_action("alice", owned_chat, "cancel", plan["revision"])
    else:
        plan = work.plan_action("alice", owned_chat, "execute", plan["revision"])
        plan = work.update_plan_step(
            "alice", owned_chat, plan["steps"][0]["id"], "done",
            expected_revision=plan["revision"],
        )
    assert plan["status"] in {"cancelled", "done"}
    work.ensure_goal("alice", owned_chat, "Continue after compaction")

    _desc, stale = asyncio.run(execute_tool_block(
        ToolBlock("create_plan", json.dumps({
            "title": "Stale plan",
            "steps": [{"id": "stale-1", "text": "Must remain fenced", "status": "pending"}],
        })),
        owner="alice", session_id=owned_chat,
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert stale["exit_code"] == 1
    assert "no longer mutable" in stale["error"]

    _desc, result = asyncio.run(execute_tool_block(
        ToolBlock("create_plan", json.dumps({
            "title": "Fresh checkpoint plan",
            "steps": [{"id": "fresh-1", "text": "Re-read checkpoint", "status": "pending"}],
        })),
        owner="alice", session_id=owned_chat,
        security_context=NO_TOOL_SECURITY_CONTEXT,
        plan_recovery=True,
    ))

    assert result["exit_code"] == 0
    assert result["plan_update"]["status"] == "executing"
    assert result["plan_update"]["steps"][0]["status"] == "in_progress"
    assert result["plan_update"]["steps"][0]["id"] == "fresh-1"


def test_draft_plan_stays_pending_and_duplicate_text_keeps_distinct_ids(owned_chat):
    work = ChatWorkStore()
    plan = work.save_plan("alice", owned_chat, "Release", "- [ ] Verify\n- [ ] Verify")
    assert [step["status"] for step in plan["steps"]] == ["pending", "pending"]
    original_ids = [step["id"] for step in plan["steps"]]
    assert len(set(original_ids)) == 2
    updated = work.save_plan(
        "alice", owned_chat, "Release", "- [x] Verify\n- [ ] Verify",
        expected_revision=plan["revision"],
    )
    assert [step["id"] for step in updated["steps"]] == original_ids


def test_goal_revision_preserves_audit_and_advances_attempt(owned_chat):
    work = ChatWorkStore()
    goal = work.ensure_goal("alice", owned_chat, "Old objective")
    revised = work.revise_goal("alice", owned_chat, "New objective", goal["revision"])
    assert revised["objective"] == "New objective"
    assert revised["attempt"] == 2
    assert revised["status"] == "active"
    assert work.events("alice", owned_chat)[-1]["type"] == "goal_revised"


def test_goal_model_failures_retry_then_wait_for_user(owned_chat):
    store = ChatWorkStore()
    goal = store.ensure_goal("alice", owned_chat, "Finish despite transient model errors")
    goal = store.goal_action("alice", owned_chat, "pause", goal["revision"])
    resumed = store.goal_action("alice", owned_chat, "resume", goal["revision"])
    assert resumed["attempt"] == goal["attempt"]
    for expected in (1, 2):
        goal = store.record_goal_failure("alice", owned_chat, "Model request failed")
        assert goal["failure_count"] == expected
        assert goal["status"] == "active"
    goal = store.record_goal_failure("alice", owned_chat, "Model request failed")
    assert goal["failure_count"] == 3
    assert goal["status"] == "waiting_user"


def test_goal_retriable_checkpoint_failure_stays_active_with_backoff_count(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Keep the durable goal alive")
    for expected in range(1, 7):
        goal = store.record_goal_failure(
            "alice", owned_chat, "Context checkpoint failed",
            {"reason": "context_compaction"}, keep_active=True,
        )
        assert goal["failure_count"] == expected
        assert goal["status"] == "active"
    assert goal["checkpoint"]["reason"] == "context_compaction"
    recovered = store.clear_goal_failure(
        "alice", owned_chat, reason="context_compaction_succeeded",
    )
    assert recovered["status"] == "active"
    assert recovered["failure_count"] == 0
    assert recovered["last_error"] is None
    assert recovered["checkpoint"]["reason"] == "context_compaction"


def test_goal_tools_receive_the_validated_owner_and_session(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Persist checkpoints")
    _desc, result = asyncio.run(execute_tool_block(
        ToolBlock("update_goal_progress", json.dumps({
            "progress": "Round one verified",
            "checkpoint": {"round": 1},
        })),
        owner="alice",
        session_id=owned_chat,
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert result["exit_code"] == 0
    assert result["goal_update"]["progress"] == "Round one verified"
    assert store.get("alice", owned_chat)["goal"]["checkpoint"] == {"round": 1}


def test_goal_guidance_is_durable_and_does_not_pause_goal(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Ship the release")
    result = store.add_goal_guidance("alice", owned_chat, "Also verify HTTP and HTTPS")
    assert result["goal"]["status"] == "active"
    assert result["guidance"]["text"] == "Also verify HTTP and HTTPS"
    current = store.get("alice", owned_chat)["goal"]
    assert current["status"] == "active"
    assert current["checkpoint"]["guidance"][-1]["id"] == result["guidance"]["id"]
    with SessionLocal() as db:
        saved = db.query(ChatMessage).filter_by(
            session_id=owned_chat, role="user", content="Also verify HTTP and HTTPS",
        ).one()
        assert json.loads(saved.meta_data)["goal_guidance"] is True


def test_goal_background_context_is_hidden_untrusted_and_durable(owned_chat):
    from src.prompt_security import untrusted_context_message
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Ship the release")
    context = untrusted_context_message("background job output", "tool result")
    result = store.add_goal_background_context("alice", owned_chat, context, "job-1")
    item = result["guidance"]
    assert result["goal"]["status"] == "active"
    assert item["context_message"]["metadata"]["trusted"] is False
    current = store.get("alice", owned_chat)["goal"]
    assert current["checkpoint"]["guidance"][-1]["id"] == item["id"]
    with SessionLocal() as db:
        saved = db.query(ChatMessage).filter_by(
            session_id=owned_chat, role="user", content=context["content"],
        ).one()
        metadata = json.loads(saved.meta_data)
    assert metadata["hidden"] == 1
    assert metadata["hidden_from_user_view"] is True
    assert metadata["trusted"] is False
    assert metadata["bg_job_id"] == "job-1"


def test_agent_loop_preserves_background_context_as_untrusted_data():
    source = (Path(__file__).resolve().parents[1] / "src/agent_loop.py").read_text()
    block = source.split("context_message = item.get", 1)[1].split(
        "_round_had_correction = True", 1,
    )[0]
    assert '"metadata": dict(context_message.get("metadata") or {})' in block
    assert '"Additional user guidance' in block


def test_terminal_run_attaches_timeline_v2_without_removing_legacy_metadata(monkeypatch, owned_chat):
    # Some legacy suites temporarily replace core.database.SessionLocal at
    # module scope. Pin the durable writer to the fixture's actual database so
    # this integration assertion remains order-independent.
    import core.database as database
    monkeypatch.setattr(database, "SessionLocal", SessionLocal)
    with SessionLocal.begin() as db:
        db.add(ChatMessage(
            id=uuid.uuid4().hex, session_id=owned_chat, role="assistant", content="Done",
            meta_data=json.dumps({"round_texts": ["Done"], "tool_events": []}),
        ))
    run = agent_runs._Run()
    run.status = "done"
    run.context_usage = {
        "used_tokens": 30000, "context_length": 100000,
        "model": "test-model", "source": "estimated", "round": 3,
    }
    agent_runs._publish(run, 'data: {"delta":"Done"}\n\n')
    agent_runs._publish(run, 'data: {"type":"tool_start","tool":"bash"}\n\n')
    agent_runs._publish(run, 'data: {"type":"tool_output","tool":"bash","output":"ok","exit_code":0}\n\n')
    agent_runs._persist_timeline_v2(owned_chat, run)
    with SessionLocal() as db:
        row = db.query(ChatMessage).filter_by(session_id=owned_chat, role="assistant").first()
        metadata = json.loads(row.meta_data)
    assert metadata["round_texts"] == ["Done"]
    assert metadata["tool_events"] == []
    assert metadata["timeline_v2"]["run_id"] == run.run_id
    assert metadata["working_context"]["used_tokens"] == 30000
    assert [item["seq"] for item in metadata["timeline_v2"]["events"]] == [0, 1, 2]
    assert metadata["timeline_v2"]["events"][1]["data"]["tool_call_id"] == metadata["timeline_v2"]["events"][2]["data"]["tool_call_id"]


def test_stopped_run_persists_rich_partial_after_latest_user(monkeypatch, owned_chat):
    import core.database as database
    monkeypatch.setattr(database, "SessionLocal", SessionLocal)
    before = datetime.utcnow() - timedelta(seconds=2)
    with SessionLocal.begin() as db:
        old_id = uuid.uuid4().hex
        db.add(ChatMessage(
            id=old_id, session_id=owned_chat, role="assistant", content="Older reply",
            meta_data=json.dumps({"model": "old-model"}), timestamp=before,
        ))
        db.add(ChatMessage(
            id=uuid.uuid4().hex, session_id=owned_chat, role="user",
            content="Run the task", timestamp=datetime.utcnow(),
        ))
    run = agent_runs._Run()
    agent_runs._publish(run, 'data: {"type":"agent_step","round":1}\n\n')
    agent_runs._publish(run, 'data: {"delta":"checking","thinking":true}\n\n')
    agent_runs._publish(run, 'data: {"delta":"Starting verification."}\n\n')
    agent_runs._publish(run, 'data: {"type":"tool_start","tool":"bash","command":"pytest","round":1}\n\n')
    agent_runs._publish(run, 'data: {"type":"tool_progress","tool":"bash","tail":"collected 3 tests","round":1}\n\n')
    run.status = "stopped"
    agent_runs._persist_timeline_v2(owned_chat, run)

    with SessionLocal() as db:
        rows = db.query(ChatMessage).filter_by(session_id=owned_chat, role="assistant").order_by(ChatMessage.timestamp).all()
        assert len(rows) == 2
        assert json.loads(rows[0].meta_data) == {"model": "old-model"}
        metadata = json.loads(rows[1].meta_data)
        assert rows[1].content == "Starting verification."
        assert metadata["stopped"] is True
        assert "<think>\nchecking\n</think>" in metadata["round_texts"][0]
        assert metadata["tool_events"][0]["tool"] == "bash"
        assert metadata["tool_events"][0]["exit_code"] == 130
        assert "collected 3 tests" in metadata["tool_events"][0]["output"]
        assert metadata["timeline_v2"]["run_id"] == run.run_id


def test_stopped_long_run_keeps_timeline_tail_not_only_oldest_events(monkeypatch, owned_chat):
    import core.database as database
    monkeypatch.setattr(database, "SessionLocal", SessionLocal)
    message_id = uuid.uuid4().hex
    with SessionLocal.begin() as db:
        db.add(ChatMessage(
            id=message_id, session_id=owned_chat, role="assistant", content="partial",
            meta_data="{}", timestamp=datetime.utcnow(),
        ))
    run = agent_runs._Run()
    run.buffer = [f'data: {json.dumps({"delta": f"old-{index}"})}\n\n' for index in range(5105)]
    run.buffer.append('data: ' + json.dumps({"delta": "LATEST-TAIL-MARKER"}) + '\n\n')
    run.buffer.append('data: ' + json.dumps({"type": "message_saved", "id": message_id}) + '\n\n')
    run.status = "stopped"
    agent_runs._persist_timeline_v2(owned_chat, run)
    with SessionLocal() as db:
        row = db.query(ChatMessage).filter_by(id=message_id).first()
        timeline = json.loads(row.meta_data)["timeline_v2"]
    assert timeline["truncated"] is True
    assert timeline["events"][0]["seq"] > 0
    assert any(item["data"].get("delta") == "LATEST-TAIL-MARKER" for item in timeline["events"])


def test_goal_prose_does_not_stop_detached_server_run(monkeypatch, owned_chat):
    work = ChatWorkStore()
    goal = work.ensure_goal("alice", owned_chat, "Finish only after verification")
    rounds = 0

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)

    async def transport(_candidates, _messages, **_kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            yield 'data: {"delta":"I think this is done."}\n\n'
        elif rounds == 2:
            yield 'data: {"delta":"```complete_goal\\n{\\"summary\\":\\"Verified\\",\\"evidence\\":[\\"test passed\\"]}\\n```"}\n\n'
        else:
            yield 'data: {"delta":"Verified completion recorded."}\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, **_kwargs):
        args = json.loads(block.content)
        updated = work.complete_goal("alice", owned_chat, args["summary"], args["evidence"])
        return block.tool_type, {"goal_update": updated, "output": "Goal completed", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", transport)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)

    async def collect():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "http://model.test/v1", "test-model",
            [{"role": "user", "content": "Finish only after verification"}],
            owner="alice", session_id=owned_chat, active_goal=goal,
            relevant_tools={"complete_goal"}, max_rounds=5,
            _is_teacher_run=True,
        )]

    chunks = asyncio.run(collect())
    assert rounds == 3
    assert work.get("alice", owned_chat)["goal"]["status"] == "completed"
    assert any('"type": "goal_update"' in chunk for chunk in chunks)
