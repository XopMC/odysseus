"""Real child plan persistence and tool routing must not touch parent work."""
import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatGoal, ChatPlan, ChatSubagentEvent, ChatSubagentRun, Session, utcnow_naive
from src import child_plan_store as plans, subagent_runtime
from src.agent_tools.interaction_tools import CreatePlanTool, UpdatePlanTool, UpdatePlanStepTool
from src.chat_work_store import WorkConflict, WorkNotFound


@pytest.fixture
def case(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(plans, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(Session(id="s", owner="alice", name="Parent", endpoint_url="http://localhost", model="worker"))
        db.flush()
        db.add(ChatPlan(id="parent-plan", session_id="s", owner="alice", title="Parent plan", status="executing",
                        steps=[{"id": "parent-step", "text": "Parent task", "status": "pending"}], revision=7))
        db.add(ChatGoal(id="parent-goal", session_id="s", owner="alice", objective="Parent objective", status="active"))
        for child_id in ("child-a", "child-b"):
            db.add(ChatSubagentRun(id=child_id, owner="alice", parent_session_id="s", parent_run_id="parent-run",
                                  model="worker", objective="Do child work", status="running",
                                  metrics={"tokens": 123}, worker_id="worker-lease", heartbeat_at=utcnow_naive()))
    value = SimpleNamespace(factory=factory, ctx={"owner": "alice", "session_id": "s", "parent_run_id": "child-a"})
    lease_token = subagent_runtime._execution_lease.set(("child-a", "alice", "worker-lease"))
    yield value
    subagent_runtime._execution_lease.reset(lease_token)
    with factory() as db:
        parent = db.get(ChatPlan, "parent-plan")
        assert parent.title == "Parent plan" and parent.revision == 7
        assert parent.steps == [{"id": "parent-step", "text": "Parent task", "status": "pending"}]
        assert db.get(ChatGoal, "parent-goal").status == "active"
    engine.dispose()


def tool(tool_type, data, ctx):
    return asyncio.run(tool_type().execute(json.dumps(data), ctx))[1]


def test_child_tools_persist_autoexecuting_plan_and_private_audit(case, monkeypatch):
    from src.chat_work_store import store
    def no_parent(*args, **kwargs):
        pytest.fail("Child plan invoked parent work store")
    monkeypatch.setattr(store, "get", no_parent)
    monkeypatch.setattr(store, "save_plan", no_parent)
    monkeypatch.setattr(store, "update_plan_step", no_parent)
    created = tool(CreatePlanTool, {"title": "Child task", "steps": [{"id": "inspect", "text": "Inspect"},
                                                                 {"id": "test", "text": "Test"}]}, case.ctx)
    assert created["exit_code"] == 0
    assert created["scope"] == "child" and created["child_id"] == "child-a"
    assert created["plan_update"]["status"] == "executing"
    assert all(step["status"] == "pending" for step in created["plan_update"]["steps"])
    updated = tool(UpdatePlanTool, {"plan": "- [x] Test\n- [x] Inspect", "expected_revision": 1}, case.ctx)
    assert updated["exit_code"] == 0
    assert [step["id"] for step in updated["plan_update"]["steps"]] == ["test", "inspect"]
    assert [step["status"] for step in updated["plan_update"]["steps"]] == ["pending", "pending"]
    assert "0/2" in updated["output"]
    first_done = tool(UpdatePlanStepTool, {"step_id": "inspect", "status": "done", "expected_revision": 2,
                                          "verification": ["Source inspection passed"]}, case.ctx)
    assert first_done["plan_update"]["status"] == "executing"
    completed = tool(UpdatePlanStepTool, {"step_id": "test", "status": "done", "expected_revision": 3,
                                         "verification": ["Unit tests passed"]}, case.ctx)
    assert completed["plan_update"]["status"] == "done"
    assert completed["plan_update"]["revision"] == 4
    lease_token = subagent_runtime._execution_lease.set(None)
    try:
        assert plans.get("alice", "s", "child-b") is None
    finally:
        subagent_runtime._execution_lease.reset(lease_token)
    with case.factory() as db:
        child = db.get(ChatSubagentRun, "child-a")
        assert child.metrics["tokens"] == 123
        assert child.metrics["_child_plan"] == completed["plan_update"]
        events = db.query(ChatSubagentEvent).all()
        assert len(events) == 4
        assert all(event.child_id == "child-a" and event.owner == "alice" and event.parent_session_id == "s"
                   and event.payload["plan_update"]["scope"] == "child" for event in events)


@pytest.mark.parametrize("field,value", [("owner", "bob"), ("session_id", "other")])
@pytest.mark.parametrize("tool_type,data", [
    (CreatePlanTool, {"steps": [{"text": "Inspect"}]}),
    (UpdatePlanTool, {"plan": "- [ ] Inspect"}),
    (UpdatePlanStepTool, {"step_id": "x", "status": "done"}),
])
def test_wrong_scope_never_falls_back_to_parent(case, field, value, tool_type, data):
    result = tool(tool_type, data, {**case.ctx, field: value})
    assert result["exit_code"] == 1 and "scope" in result["error"]
    assert plans.get("alice", "s", "child-a") is None


def test_stale_revision_terminal_recovery_and_copy_isolation(case):
    first = plans.save("alice", "s", "child-a", "Work", [{"text": "Inspect"}], expected_revision=0)
    with pytest.raises(WorkConflict, match="changed"):
        plans.save("alice", "s", "child-a", "Changed", [{"text": "Other"}], expected_revision=0)
    finished = plans.update_step("alice", "s", "child-a", first["steps"][0]["id"], "done", expected_revision=1)
    with pytest.raises(WorkConflict, match="no longer mutable"):
        plans.save("alice", "s", "child-a", "Again", [{"text": "Inspect"}])
    recovered = tool(CreatePlanTool, {"title": "Recovered", "steps": [{"text": "Verify"}],
                                     "expected_revision": finished["revision"]}, {**case.ctx, "plan_recovery": True})
    assert recovered["exit_code"] == 0 and recovered["plan_update"]["status"] == "executing"
    restored = plans.get("alice", "s", "child-a")
    restored["steps"][0]["text"] = "Not persisted"
    assert plans.get("alice", "s", "child-a")["steps"][0]["text"] == "Verify"


def test_child_post_compaction_plan_reprojection_preserves_verified_progress(case):
    original = plans.save("alice", "s", "child-a", "Assigned work", [
        {"id": "inspect", "text": "Inspect assigned files", "status": "pending"},
        {"id": "test", "text": "Run focused tests", "status": "pending"},
    ])
    first = plans.update_step("alice", "s", "child-a", "inspect", "done",
                              expected_revision=original["revision"],
                              summary="Inspected files",
                              progress={"verification": ["Read-only checks passed"]})
    plans.update_step("alice", "s", "child-a", "test", "in_progress",
                      expected_revision=first["revision"], summary="Tests in progress")

    recovered = tool(CreatePlanTool, {
        "title": "Fresh checkpoint projection",
        "steps": [
            {"id": "inspect", "text": "Inspect assigned files", "status": "pending"},
            {"id": "test", "text": "Run focused tests", "status": "pending"},
            {"id": "report", "text": "Return verified result", "status": "pending"},
        ],
    }, {**case.ctx, "plan_recovery": True})

    assert recovered["exit_code"] == 0
    assert [(step["id"], step["status"]) for step in recovered["plan_update"]["steps"]] == [
        ("inspect", "done"), ("test", "in_progress"), ("report", "pending"),
    ]
    assert recovered["plan_update"]["steps"][0]["progress"]["verification"] == ["Read-only checks passed"]


@pytest.mark.parametrize("run_id", [None, "parent-run", "nonexistent"])
@pytest.mark.parametrize("tool_type,data", [
    (CreatePlanTool, {"steps": [{"text": "Inspect"}]}),
    (UpdatePlanTool, {"plan": "- [ ] Inspect"}),
    (UpdatePlanStepTool, {"step_id": "x", "status": "done"}),
])
def test_misbound_child_context_never_routes_to_parent(case, monkeypatch, run_id, tool_type, data):
    from src.chat_work_store import store
    def no_parent(*args, **kwargs):
        pytest.fail("Misbound child invoked parent work store")
    for method in ("get", "save_plan", "update_plan_step"):
        monkeypatch.setattr(store, method, no_parent)
    result = tool(tool_type, data, {**case.ctx, "parent_run_id": run_id})
    assert result["exit_code"] == 1 and "scope" in result["error"]
    assert plans.get("alice", "s", "child-a") is None


def test_real_child_loop_recovers_private_plan_and_settles_own_compaction(case, monkeypatch):
    import src.agent_loop as loop
    from src import context_compaction_ledger as ledger, context_efficiency_state as efficiency
    from src.chat_effect_inbox import inbox
    monkeypatch.setattr(ledger, "SessionLocal", case.factory)
    monkeypatch.setattr(efficiency, "SessionLocal", case.factory)
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(inbox, "unknown", lambda *_: [])
    ledger.record("alice", "s", 1, child_id="child-a", ledger_hash="child-checkpoint",
                  before_tokens=9000, after_tokens=1000, economics={})
    requests = []
    async def stream(_candidates, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        yield 'data: {"delta":"Retained checkpoint inspected."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(loop, "stream_llm_with_fallback", stream)
    async def collect():
        return [chunk async for chunk in loop.stream_agent_loop(
            "http://localhost/v1", "worker", [{"role": "user", "content": "Finish assigned child work"}],
            owner="alice", session_id="s", child_run_id="child-a", max_rounds=2,
            relevant_tools={"create_plan", "update_plan_step"},
        )]
    chunks = asyncio.run(collect())
    events = [json.loads(chunk[6:]) for chunk in chunks
              if chunk.startswith("data: {")]
    assert len(requests) == 2
    assert any(event.get("type") == "context_compaction_settled"
               and event.get("generation") == 1
               and event.get("recovery") == "private_working_plan" for event in events)
    assert not any(event.get("type") == "agent_terminal" for event in events)
    assert ledger.pending("alice", "s", child_id="child-a") is None
    saved = plans.get("alice", "s", "child-a")
    assert saved["status"] == "executing" and len(saved["steps"]) == 3
    assert saved["scope"] == "child" and saved["revision"] == 1
    assert "## ACTIVE PLAN" in json.dumps(requests[1])


@pytest.mark.parametrize("state", ["cancelled", "stale_lease", "removed"])
def test_cancelled_removed_or_fenced_worker_cannot_mutate(case, state):
    with case.factory.begin() as db:
        row = db.get(ChatSubagentRun, "child-a")
        if state == "cancelled":
            row.cancel_requested = True
        elif state == "removed":
            row.removed = True
        else:
            row.heartbeat_at = utcnow_naive() - timedelta(minutes=5)
    token = subagent_runtime._execution_lease.set(("child-a", "alice", "worker-lease"))
    try:
        result = tool(CreatePlanTool, {"steps": [{"text": "Inspect"}]}, case.ctx)
    finally:
        subagent_runtime._execution_lease.reset(token)
    assert result["exit_code"] == 1
    with case.factory() as db:
        assert "_child_plan" not in db.get(ChatSubagentRun, "child-a").metrics
        assert db.query(ChatSubagentEvent).count() == 0


def test_duplicate_step_ids_rejected_without_partial_persistence(case):
    with pytest.raises(ValueError, match="unique"):
        plans.save("alice", "s", "child-a", "Invalid", [{"id": "same", "text": "One"},
                                                           {"id": "same", "text": "Two"}])
    assert plans.get("alice", "s", "child-a") is None


def test_sibling_lease_cannot_mutate_other_child(case):
    with pytest.raises(subagent_runtime.ChildLeaseLost, match="does not match"):
        plans.save("alice", "s", "child-b", "Sibling", [{"text": "Unauthorized work"}])
    with case.factory() as db:
        assert "_child_plan" not in db.get(ChatSubagentRun, "child-b").metrics
        assert db.query(ChatSubagentEvent).count() == 0


def test_writes_require_lease_but_inspection_does_not(case):
    original = plans.save("alice", "s", "child-a", "Work", [{"text": "Inspect"}])
    token = subagent_runtime._execution_lease.set(None)
    try:
        assert plans.get("alice", "s", "child-a") == original
        with pytest.raises(subagent_runtime.ChildLeaseLost, match="require"):
            plans.save("alice", "s", "child-a", "Rewrite", [{"text": "Other"}])
        with pytest.raises(subagent_runtime.ChildLeaseLost, match="require"):
            plans.update_step("alice", "s", "child-a", original["steps"][0]["id"], "done")
    finally:
        subagent_runtime._execution_lease.reset(token)
    assert plans.get("alice", "s", "child-a") == original


def test_total_plan_size_rejected_atomically(case):
    plan = plans.save("alice", "s", "child-a", "Work", [{"id": "one", "text": "Inspect"},
                                                          {"id": "two", "text": "Verify"}])
    progress = {"verification": ["x" * 1500] * 100}
    prior = plans.update_step("alice", "s", "child-a", "one", "in_progress", progress=progress)
    with case.factory() as db:
        prior_revision = db.get(ChatSubagentRun, "child-a").revision
        prior_events = db.query(ChatSubagentEvent).count()
    with pytest.raises(ValueError, match="256 KiB"):
        plans.update_step("alice", "s", "child-a", "two", "in_progress", progress=progress)
    assert plans.get("alice", "s", "child-a") == prior
    with case.factory() as db:
        assert db.get(ChatSubagentRun, "child-a").revision == prior_revision
        assert db.query(ChatSubagentEvent).count() == prior_events
