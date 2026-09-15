import asyncio
import json
import uuid

import pytest

from core.database import ChatMessage, Session, SessionLocal
from src import agent_runs
from src.chat_work_store import ChatWorkStore, WorkConflict, WorkNotFound
import src.agent_loop as agent_loop


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
