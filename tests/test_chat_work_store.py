import asyncio
from datetime import datetime, timedelta
import json
import uuid

import pytest

from core.database import ChatMessage, Session, SessionLocal
from src import agent_runs
from src.agent_tools import ToolBlock
from src.chat_work_store import ChatWorkStore, WorkConflict, WorkNotFound
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
    plan = store.update_plan_step("alice", owned_chat, "verify", "done", expected_revision=plan["revision"])
    assert plan["status"] == "done"

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
