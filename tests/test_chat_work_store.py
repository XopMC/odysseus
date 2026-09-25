import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest

from core.database import Base, ChatMessage, Session, SessionLocal, engine
from src import agent_runs
from src.agent_tools import ToolBlock
from src.chat_work_store import ChatWorkStore, WorkConflict, WorkNotFound
from src.chat_work_store import checklist_steps
import src.agent_loop as agent_loop
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block


@pytest.fixture
def owned_chat():
    # The suite uses an in-memory SQLite URL. Earlier threaded route fixtures
    # can replace/close its connection, so import-time init_db is not a stable
    # precondition for this fixture when files are run in collection order.
    Base.metadata.create_all(bind=engine)
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


@pytest.fixture
def canonical_run_db(monkeypatch):
    """Undo a legacy suite's global DB-factory replacement for run tests."""
    from core import database
    monkeypatch.setattr(database, "SessionLocal", SessionLocal)


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


def test_paused_goal_cannot_be_completed_by_an_ordinary_agent_run(owned_chat):
    work = ChatWorkStore()
    goal = work.ensure_goal("alice", owned_chat, "Verify a harmless calculation")
    paused = work.goal_action("alice", owned_chat, "pause", goal["revision"])
    with pytest.raises(WorkNotFound, match="Active goal not found"):
        work.complete_goal("alice", owned_chat, "Result 49", ["Python stdout was 49"])
    unchanged = work.get("alice", owned_chat)["goal"]
    assert unchanged["status"] == "paused"
    assert unchanged["revision"] == paused["revision"]
    assert not any(event["type"] == "goal_completed" for event in work.events("alice", owned_chat))

    resumed = work.goal_action("alice", owned_chat, "resume", paused["revision"])
    completed = work.complete_goal("alice", owned_chat, "Result 49", ["Python stdout was 49"])
    assert resumed["status"] == "active"
    assert completed["status"] == "completed"


def test_repeated_monologue_requires_review_without_fake_question_or_user_pause(owned_chat):
    store = ChatWorkStore()
    goal = store.ensure_goal("alice", owned_chat, "Verify arithmetic")
    assert store.acquire_goal_lease("alice", owned_chat)
    review = store.update_goal(
        "alice", owned_chat, "No new progress after repeated responses.",
        {"reason": "repeated_premature_stop", "round": 6}, review_required=True,
    )
    assert review["status"] == "review_required"
    assert review["checkpoint"]["_wait_reason"] == "repeated_premature_stop"
    metadata = store.wait_metadata("alice", owned_chat)
    assert metadata["status"] == "review_required"
    assert metadata["wait_reason"] == "repeated_premature_stop"
    assert metadata["lease_held"] is False
    from src.run_wait_state import compose_wait_panel
    panel = compose_wait_panel(
        run={"run_id": "finished-run", "status": "done", "started_at": 100},
        goal=metadata, now=200,
    )
    assert panel["phase"] == "review"
    assert panel["recovery_action"] == "resume_goal"
    assert panel["wait_reason"] == "repeated_premature_stop"
    resumed = store.goal_action("alice", owned_chat, "resume", review["revision"])
    assert resumed["status"] == "active"
    assert "_wait_reason" not in resumed["checkpoint"]


def test_stale_action_loop_cannot_mark_a_new_goal_attempt_for_review(owned_chat):
    store = ChatWorkStore()
    first = store.ensure_goal("alice", owned_chat, "Verify arithmetic")
    lease = store.acquire_goal_lease("alice", owned_chat)
    current = store.consume_goal_lease("alice", owned_chat, lease)
    assert current["attempt"] == first["attempt"] + 1

    with pytest.raises(WorkConflict):
        store.update_goal(
            "alice", owned_chat, "Stale loop breaker",
            {"reason": "repeated_action_observation", "round": 8},
            review_required=True, expected_goal_id=first["id"],
            expected_attempt=first["attempt"],
        )
    unchanged = store.get("alice", owned_chat)
    assert unchanged["goal"]["status"] == "active"
    assert unchanged["goal"]["attempt"] == current["attempt"]


def test_action_loop_review_reason_survives_wait_metadata_and_panel(owned_chat):
    store = ChatWorkStore()
    goal = store.ensure_goal("alice", owned_chat, "Verify arithmetic")
    review = store.update_goal(
        "alice", owned_chat, "Repeated tool evidence cycle stopped for review.",
        {"reason": "repeated_action_observation", "round": 8},
        review_required=True, expected_goal_id=goal["id"],
        expected_attempt=goal["attempt"],
    )
    metadata = store.wait_metadata("alice", owned_chat)
    assert review["status"] == metadata["status"] == "review_required"
    assert metadata["wait_reason"] == "repeated_action_observation"
    from src.run_wait_state import compose_wait_panel
    panel = compose_wait_panel(
        run={"run_id": "loop-run", "status": "done", "started_at": 100},
        goal=metadata, now=200,
    )
    assert panel["phase"] == "review"
    assert panel["wait_reason"] == "repeated_action_observation"
    assert panel["recovery_action"] == "resume_goal"


def test_legacy_plan_update_marks_terminal_when_all_required_steps_done(owned_chat):
    work = ChatWorkStore()
    plan = work.save_plan("alice", owned_chat, "Arithmetic", "- [ ] Direct\n- [ ] Independent")
    original_ids = [step["id"] for step in plan["steps"]]
    plan = work.plan_action("alice", owned_chat, "execute", plan["revision"])
    plan = work.save_plan("alice", owned_chat, "Arithmetic", "- [x] Direct\n- [x] Independent",
                          expected_revision=plan["revision"])
    assert plan["status"] == "done"
    assert plan["current_step_id"] is None
    assert [step["id"] for step in plan["steps"]] == original_ids


def test_post_compaction_recovery_plan_starts_from_pending_draft(owned_chat):
    work = ChatWorkStore()
    plan = work.save_plan("alice", owned_chat, "Post-compaction recovery", [
        {"id": "recovery-1-1", "text": "Re-read the active Goal and durable checkpoint", "status": "pending", "required": True},
        {"id": "recovery-1-2", "text": "Verify remaining work", "status": "pending", "required": True},
    ], replace_terminal=True)
    assert plan["status"] == "draft"
    plan = work.plan_action("alice", owned_chat, "execute", plan["revision"])
    assert plan["status"] == "executing"
    assert [step["status"] for step in plan["steps"]] == ["in_progress", "pending"]


def test_goal_stall_wait_reason_is_durable_owner_scoped_and_cleared_on_resume(owned_chat):
    store = ChatWorkStore()
    goal = store.ensure_goal("alice", owned_chat, "Harmless verification")
    goal = store.update_goal(
        "alice", owned_chat, "No new safe progress after repeated continuation attempts.",
        {"round": 6, "reason": "repeated_premature_stop"}, waiting_user=True,
    )
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "repeated_premature_stop"
    with pytest.raises(WorkNotFound):
        store.wait_metadata("bob", owned_chat)
    goal = store.goal_action("alice", owned_chat, "resume", goal["revision"])
    assert goal["status"] == "active"
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] is None
    goal = store.update_goal(
        "alice", owned_chat, "Question requires an answer",
        {"question_id": "question-1", "question": "Which safe profile?"}, waiting_user=True,
    )
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "ask_user"


def test_duplicate_ask_user_checkpoint_is_idempotent_and_conflicting_question_is_fenced(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Harmless verification")
    first = store.update_goal(
        "alice", owned_chat, "Waiting for the user's decision",
        {"question_id": "question-same", "question": "safe prompt"}, waiting_user=True,
    )

    duplicate = store.update_goal(
        "alice", owned_chat, "Waiting for the user's decision",
        {"question_id": "question-same", "question": "safe prompt"}, waiting_user=True,
    )
    assert duplicate["revision"] == first["revision"]
    assert duplicate["checkpoint"] == first["checkpoint"]

    with pytest.raises(WorkConflict, match="different user decision"):
        store.update_goal(
            "alice", owned_chat, "Waiting for another decision",
            {"question_id": "question-other", "question": "other safe prompt"}, waiting_user=True,
        )
    assert store.get("alice", owned_chat)["goal"]["revision"] == first["revision"]


def test_repeated_provider_failure_has_explicit_wait_reason(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Harmless verification")
    for _ in range(3):
        goal = store.record_goal_failure("alice", owned_chat, "Provider unavailable")
    assert goal["status"] == "waiting_user"
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "provider_failure"


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
    with pytest.raises(WorkConflict, match="Execute the plan"):
        work.save_plan(
            "alice", owned_chat, "Release", "- [x] Verify\n- [ ] Verify",
            expected_revision=plan["revision"],
        )
    unchanged = work.get("alice", owned_chat)["plan"]
    assert [step["id"] for step in unchanged["steps"]] == original_ids
    assert [step["status"] for step in unchanged["steps"]] == ["pending", "pending"]


def test_new_draft_cannot_claim_completed_steps(owned_chat):
    work = ChatWorkStore()
    with pytest.raises(WorkConflict, match="Execute the plan"):
        work.save_plan("alice", owned_chat, "False progress", "- [x] No verified work")
    assert work.get("alice", owned_chat)["plan"] is None


def test_legacy_update_plan_tool_cannot_complete_a_draft(owned_chat):
    work = ChatWorkStore()
    plan = work.save_plan("alice", owned_chat, "Arithmetic", "- [ ] Ask user\n- [ ] Verify")
    _description, result = asyncio.run(execute_tool_block(
        ToolBlock("update_plan", json.dumps({"plan": "- [x] Ask user\n- [x] Verify"})),
        owner="alice", session_id=owned_chat, security_context=NO_TOOL_SECURITY_CONTEXT,
    ))
    assert result["exit_code"] == 1
    assert "Execute the plan" in result["error"]
    current = work.get("alice", owned_chat)["plan"]
    assert current["revision"] == plan["revision"]
    assert [step["status"] for step in current["steps"]] == ["pending", "pending"]


def test_active_goal_prompt_requires_durable_question_not_prose():
    note = agent_loop.build_active_goal_note({
        "status": "active", "objective": "Harmless arithmetic",
        "checkpoint": {}, "attempt": 1,
    })
    assert "call `ask_user`" in note
    assert "a prose question is not a wait state" in note
    assert "no choice is received until a new user answer exists" in note


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


def test_goal_prose_checkpoint_does_not_reset_provider_failure_budget(owned_chat):
    work = ChatWorkStore()
    work.ensure_goal("alice", owned_chat, "Finish despite model failures")
    for expected in (1, 2):
        failed = work.record_goal_failure("alice", owned_chat, "Empty model output")
        assert failed["failure_count"] == expected
        prose = work.update_goal(
            "alice", owned_chat, "Model said it will continue",
            {"response_excerpt": "I will continue"}, reset_failures=False,
        )
        assert prose["failure_count"] == expected
    parked = work.record_goal_failure("alice", owned_chat, "Empty model output")
    assert parked["failure_count"] == 3
    assert parked["status"] == "waiting_user"


def test_manual_goal_resume_dispatch_failure_waits_immediately(owned_chat):
    store = ChatWorkStore()
    goal = store.ensure_goal("alice", owned_chat, "Harmless verification")
    goal = store.goal_action("alice", owned_chat, "pause", goal["revision"])
    goal = store.goal_action("alice", owned_chat, "resume", goal["revision"])
    failed = store.record_goal_failure(
        "alice", owned_chat, "Goal continuation HTTP 503",
        {"reason": "continuation_dispatch_failed"}, force_wait_user=True,
    )
    assert failed["status"] == "waiting_user"
    assert failed["failure_count"] == 1
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "dispatch_failure"


def test_goal_tool_budget_waits_with_durable_exact_run_snapshot(owned_chat):
    store = ChatWorkStore()
    initial = store.ensure_goal("alice", owned_chat, "Harmless bounded task")
    goal = store.wait_on_goal_budget(
        "alice", owned_chat, resource="tool_calls", used=2, limit=2,
        run_id="a" * 32, expected_goal_id=initial["id"],
        expected_attempt=initial["attempt"],
    )
    assert goal["status"] == "waiting_user"
    assert goal["checkpoint"]["budget"] == {
        "resource": "tool_calls", "used": 2, "limit": 2, "run_id": "a" * 32,
    }
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "resource_budget"
    assert store.events("alice", owned_chat)[-1]["type"] == "goal_budget_exceeded"
    with pytest.raises(WorkConflict):
        store.wait_on_goal_budget("alice", owned_chat, resource="tool_calls",
                                  used=2, limit=2, run_id="a" * 32,
                                  expected_goal_id=initial["id"],
                                  expected_attempt=initial["attempt"])


def test_goal_model_round_budget_waits_instead_of_resetting_attempt(owned_chat):
    store = ChatWorkStore()
    initial = store.ensure_goal("alice", owned_chat, "Harmless bounded task")
    goal = store.wait_on_goal_budget(
        "alice", owned_chat, resource="model_rounds", used=4, limit=4,
        run_id="b" * 32, expected_goal_id=initial["id"],
        expected_attempt=initial["attempt"],
    )
    assert goal["status"] == "waiting_user"
    assert goal["attempt"] == initial["attempt"]
    assert goal["checkpoint"]["budget"]["resource"] == "model_rounds"
    assert store.wait_metadata("alice", owned_chat)["budget"]["limit"] == 4


def test_goal_model_token_budget_waits_with_exact_usage(owned_chat):
    store = ChatWorkStore()
    initial = store.ensure_goal("alice", owned_chat, "Harmless bounded task")
    goal = store.wait_on_goal_budget(
        "alice", owned_chat, resource="model_tokens", used=1200, limit=1000,
        usage_source="estimated",
        run_id="c" * 32, expected_goal_id=initial["id"],
        expected_attempt=initial["attempt"],
    )
    assert goal["status"] == "waiting_user"
    assert goal["checkpoint"]["budget"] == {
        "resource": "model_tokens", "used": 1200, "limit": 1000,
        "run_id": "c" * 32, "usage_source": "estimated",
    }
    assert store.wait_metadata("alice", owned_chat)["budget"]["used"] == 1200


def test_goal_model_request_budget_waits_with_exact_run(owned_chat):
    store = ChatWorkStore()
    initial = store.ensure_goal("alice", owned_chat, "Harmless bounded task")
    goal = store.wait_on_goal_budget(
        "alice", owned_chat, resource="model_requests", used=2, limit=2,
        run_id="d" * 32, expected_goal_id=initial["id"],
        expected_attempt=initial["attempt"],
    )
    assert goal["status"] == "waiting_user"
    assert goal["checkpoint"]["budget"] == {
        "resource": "model_requests", "used": 2, "limit": 2, "run_id": "d" * 32,
    }


def test_goal_lease_cas_rejects_a_revised_attempt(owned_chat):
    store = ChatWorkStore()
    initial = store.ensure_goal("alice", owned_chat, "Harmless task")
    revised = store.revise_goal(
        "alice", owned_chat, "Revised harmless task", initial["revision"],
    )
    assert store.acquire_goal_lease(
        "alice", owned_chat, expected_goal_id=initial["id"],
        expected_attempt=initial["attempt"],
    ) is None
    token = store.acquire_goal_lease(
        "alice", owned_chat, expected_goal_id=revised["id"],
        expected_attempt=revised["attempt"],
    )
    assert token
    assert store.consume_goal_lease("alice", owned_chat, token)["attempt"] == revised["attempt"] + 1


def test_goal_dispatch_rejects_stale_attempt_before_model_request(monkeypatch, owned_chat):
    import src.goal_controller as controller
    from src.chat_effect_inbox import inbox

    work = ChatWorkStore()
    initial = work.ensure_goal("alice", owned_chat, "Harmless task")
    revised = work.revise_goal(
        "alice", owned_chat, "Revised harmless task", initial["revision"],
    )
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])
    monkeypatch.setattr(agent_runs, "is_active", lambda session: False)

    async def same_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(controller.asyncio, "to_thread", same_thread)

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("A stale attempt must not dispatch a model request")

    monkeypatch.setattr(controller.httpx, "AsyncClient", ForbiddenClient)
    assert asyncio.run(controller.dispatch_goal_continuation(
        "alice", owned_chat, reason="terminal_done",
        expected_goal_id=initial["id"], expected_attempt=initial["attempt"],
    )) is False
    current = work.get("alice", owned_chat)["goal"]
    assert (current["status"], current["attempt"]) == ("active", revised["attempt"])


def test_stale_run_error_cannot_mark_revised_goal_failed(owned_chat):
    work = ChatWorkStore()
    initial = work.ensure_goal("alice", owned_chat, "Harmless task")
    revised = work.revise_goal(
        "alice", owned_chat, "Revised harmless task", initial["revision"],
    )
    with pytest.raises(WorkConflict):
        work.record_goal_failure(
            "alice", owned_chat, "Old run failed",
            expected_goal_id=initial["id"],
            expected_attempt=initial["attempt"],
        )
    current = work.get("alice", owned_chat)["goal"]
    assert current["status"] == "active"
    assert current["attempt"] == revised["attempt"]
    assert current["failure_count"] == 0


def test_stale_goal_budget_cannot_park_a_new_attempt(owned_chat):
    store = ChatWorkStore()
    initial = store.ensure_goal("alice", owned_chat, "Harmless bounded task")
    revised = store.revise_goal(
        "alice", owned_chat, "Revised harmless task", initial["revision"],
    )
    assert revised["attempt"] > initial["attempt"]
    with pytest.raises(WorkConflict):
        store.wait_on_goal_budget(
            "alice", owned_chat, resource="tool_calls", used=2, limit=2,
            run_id="a" * 32, expected_goal_id=initial["id"],
            expected_attempt=initial["attempt"],
        )
    assert store.get("alice", owned_chat)["goal"]["status"] == "active"


def test_goal_controller_http_failure_parks_manual_resume_without_success(monkeypatch, owned_chat):
    import src.goal_controller as controller
    from src.chat_effect_inbox import inbox

    work = ChatWorkStore()
    goal = work.ensure_goal("alice", owned_chat, "Harmless verification")
    goal = work.goal_action("alice", owned_chat, "pause", goal["revision"])
    work.goal_action("alice", owned_chat, "resume", goal["revision"])
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])
    monkeypatch.setattr(agent_runs, "is_active", lambda session: False)
    monkeypatch.setattr(agent_runs, "continuation_for_session", lambda session: {})

    async def same_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(controller.asyncio, "to_thread", same_thread)

    class Response:
        status_code = 503

        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Client:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def stream(self, *_, **__): return Response()

    monkeypatch.setattr(controller.httpx, "AsyncClient", Client)
    assert asyncio.run(controller.dispatch_goal_continuation(
        "alice", owned_chat, reason="goal_resumed",
    )) is False
    state = work.get("alice", owned_chat)["goal"]
    assert state["status"] == "waiting_user"
    assert state["checkpoint"]["_wait_reason"] == "dispatch_failure"


def test_goal_controller_reports_missing_selected_model_without_echoing_response(monkeypatch, owned_chat):
    import src.goal_controller as controller
    from src.chat_effect_inbox import inbox

    work = ChatWorkStore()
    goal = work.ensure_goal("alice", owned_chat, "Harmless verification")
    goal = work.goal_action("alice", owned_chat, "pause", goal["revision"])
    work.goal_action("alice", owned_chat, "resume", goal["revision"])
    monkeypatch.setattr(inbox, "unknown", lambda owner, session: [])
    monkeypatch.setattr(agent_runs, "is_active", lambda session: False)
    monkeypatch.setattr(agent_runs, "continuation_for_session", lambda session: {})

    async def same_thread(func, *args, **kwargs): return func(*args, **kwargs)
    monkeypatch.setattr(controller.asyncio, "to_thread", same_thread)

    class Response:
        status_code = 400
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def aread(self):
            return b'{"detail":"No model selected for this chat. private-marker"}'

    class Client:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def stream(self, *_, **__): return Response()

    monkeypatch.setattr(controller.httpx, "AsyncClient", Client)
    assert asyncio.run(controller.dispatch_goal_continuation(
        "alice", owned_chat, reason="goal_resumed",
    )) is False
    state = work.get("alice", owned_chat)["goal"]
    assert state["checkpoint"]["failure_code"] == "model_unselected"
    assert "Choose a model" in state["progress"]
    assert "private-marker" not in json.dumps(state)


def test_goal_alternating_provider_errors_still_exhaust_retry_budget(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Harmless verification")
    for expected, error in enumerate(("HTTP 503", "Transport timeout", "HTTP 502"), 1):
        goal = store.record_goal_failure("alice", owned_chat, error)
        assert goal["failure_count"] == expected
    assert goal["status"] == "waiting_user"
    assert goal["last_error"] == "HTTP 502"


@pytest.mark.parametrize("last_seq,expected_next", [(-1, 0), (0, 1)])
def test_recovered_run_snapshot_uses_exact_zero_cursor(owned_chat, canonical_run_db, monkeypatch, last_seq, expected_next):
    from core.database import ChatRunState, utcnow_naive

    run_id = uuid.uuid4().hex
    with SessionLocal.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id=owned_chat, owner="alice",
            status="interrupted", started_at=utcnow_naive(),
            last_seq=last_seq, durable_seq=last_seq,
            context_revision=1, ledger_hash="a" * 64,
        ))
    monkeypatch.delitem(agent_runs._RUNS, owned_chat, raising=False)
    snapshot = agent_runs.describe_run(owned_chat)
    assert snapshot["run_id"] == run_id
    assert snapshot["last_seq"] == last_seq
    assert snapshot["next_seq"] == expected_next


def test_restart_recovery_fences_run_and_replays_once(owned_chat, canonical_run_db, monkeypatch, tmp_path):
    from core.database import ChatRunState, ChatMessage as DbChatMessage, utcnow_naive
    from src.chat_replay_log import ReplayLog

    run_id = uuid.uuid4().hex
    log = ReplayLog(str(tmp_path), run_id, owned_chat, create=True)
    log.append('data: {"delta":"partial safe fixture"}\n\n')
    with SessionLocal.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id=owned_chat, owner="alice", status="running",
            started_at=utcnow_naive(), last_seq=0, durable_seq=0,
            context_revision=4, ledger_hash="a" * 64,
            context_snapshot={"model": "fixture", "used_tokens": 40, "context_length": 1000},
            continuation={"allow_bash": False},
        ))
    monkeypatch.setattr(agent_runs, "replay_root", lambda: str(tmp_path))
    monkeypatch.delitem(agent_runs._RUNS, owned_chat, raising=False)

    recovered = agent_runs.recover_durable_runs(
        before_started_at=datetime.now(timezone.utc).timestamp() + 60,
    )
    assert [state["run_id"] for state in recovered] == [run_id]
    snapshot = agent_runs.describe_run(owned_chat)
    assert snapshot["status"] == "interrupted"
    assert snapshot["terminal_reason"] == "process_restarted"
    assert snapshot["durable_seq"] == 0
    assert snapshot["next_seq"] == 1
    assert snapshot["context_revision"] == 4
    assert agent_runs.event_page(owned_chat, after_seq=-1)["events"][0]["seq"] == 0
    with SessionLocal() as db:
        first_count = db.query(DbChatMessage).filter_by(session_id=owned_chat, role="assistant").count()
    assert first_count == 1
    assert agent_runs.recover_durable_runs() == []
    with SessionLocal() as db:
        assert db.query(DbChatMessage).filter_by(session_id=owned_chat, role="assistant").count() == first_count


def test_startup_recovery_never_interrupts_a_new_process_run(owned_chat, canonical_run_db, monkeypatch):
    from core.database import ChatRunState

    cutoff = agent_runs.time.time()
    run_id = uuid.uuid4().hex
    with SessionLocal.begin() as db:
        db.add(ChatRunState(
            run_id=run_id, session_id=owned_chat, owner="alice", status="running",
            started_at=datetime.fromtimestamp(cutoff + 5, timezone.utc).replace(tzinfo=None),
            last_seq=0, durable_seq=0,
        ))
    monkeypatch.delitem(agent_runs._RUNS, owned_chat, raising=False)
    assert agent_runs.recover_durable_runs(before_started_at=cutoff) == []
    with SessionLocal() as db:
        assert db.get(ChatRunState, run_id).status == "running"

    # Even a manually invoked recovery with a later cutoff must not claim a
    # run that is still owned by this process's in-memory registry.
    current = agent_runs._Run()
    current.run_id = run_id
    current.session_id = owned_chat
    monkeypatch.setitem(agent_runs._RUNS, owned_chat, current)
    assert agent_runs.recover_durable_runs(before_started_at=cutoff + 10) == []
    with SessionLocal() as db:
        assert db.get(ChatRunState, run_id).status == "running"


def test_non_durable_checkpoint_never_rolls_zero_durable_cursor_back(owned_chat, canonical_run_db):
    from core.database import ChatRunState, utcnow_naive

    run = agent_runs._Run()
    run.run_id = uuid.uuid4().hex
    run.session_id = owned_chat
    run.owner = "alice"
    with SessionLocal.begin() as db:
        db.add(ChatRunState(
            run_id=run.run_id, session_id=owned_chat, owner="alice", status="running",
            started_at=utcnow_naive(), last_seq=0, durable_seq=0,
            context_revision=0,
        ))
    agent_runs._persist_run_state(run, status="running", durable=False)
    with SessionLocal() as db:
        row = db.query(ChatRunState).filter_by(run_id=run.run_id).one()
        assert row.durable_seq == 0
        assert row.ledger_hash == run.ledger_hash
        assert row.ledger_hash is not None


def test_unchanged_run_context_checkpoint_does_not_scan_prior_runs(owned_chat, canonical_run_db):
    from core.database import ChatRunState, engine, utcnow_naive
    from sqlalchemy import event

    run = agent_runs._Run()
    run.run_id = uuid.uuid4().hex
    run.session_id = owned_chat
    run.owner = "alice"
    run.context_usage = {"model": "fixture", "used_tokens": 1000,
                         "context_length": 8000, "source": "backend"}
    with SessionLocal.begin() as db:
        db.add(ChatRunState(
            run_id=run.run_id, session_id=owned_chat, owner="alice", status="running",
            started_at=utcnow_naive(), last_seq=-1, durable_seq=-1,
            context_revision=1, context_snapshot=dict(run.context_usage),
        ))
        for _ in range(10):
            db.add(ChatRunState(
                run_id=uuid.uuid4().hex, session_id=owned_chat, owner="alice", status="done",
                started_at=utcnow_naive(), last_seq=0, durable_seq=0,
                context_revision=1, context_snapshot=dict(run.context_usage),
            ))
    selects = []

    def count_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "chat_run_states" in statement:
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_select)
    try:
        agent_runs._persist_run_state(run, status="running", durable=False)
    finally:
        event.remove(engine, "before_cursor_execute", count_select)
    assert len(selects) == 1, selects


def test_goal_repeated_checkpoint_failure_waits_without_losing_ledger(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Keep the durable goal alive")
    for expected in range(1, 4):
        goal = store.record_goal_failure(
            "alice", owned_chat, "Context checkpoint failed",
            {"reason": "context_compaction", "ledger_hash": "a" * 64},
        )
        assert goal["failure_count"] == expected
        assert goal["status"] == ("waiting_user" if expected == 3 else "active")
    assert goal["checkpoint"]["reason"] == "context_compaction"
    assert goal["checkpoint"]["ledger_hash"] == "a" * 64
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "context_compaction"
    with pytest.raises(WorkNotFound):
        store.record_goal_failure("alice", owned_chat, "Context checkpoint failed",
                                  {"reason": "context_compaction"})
    resumed = store.goal_action("alice", owned_chat, "resume", goal["revision"])
    assert resumed["status"] == "active"
    assert resumed["checkpoint"]["ledger_hash"] == "a" * 64


def test_goal_checkpoint_failure_can_wait_immediately_without_losing_ledger(owned_chat):
    store = ChatWorkStore()
    store.ensure_goal("alice", owned_chat, "Keep the durable goal alive")
    failed = store.record_goal_failure(
        "alice", owned_chat, "Context checkpoint failed",
        {"reason": "context_compaction", "failure_code": "context_uncompactable",
         "ledger_hash": "b" * 64}, force_wait_user=True,
    )
    assert failed["status"] == "waiting_user"
    assert failed["failure_count"] == 1
    assert failed["checkpoint"]["ledger_hash"] == "b" * 64
    assert failed["checkpoint"]["failure_code"] == "context_uncompactable"
    assert store.wait_metadata("alice", owned_chat)["wait_reason"] == "context_compaction"
    assert store.wait_metadata("alice", owned_chat)["failure_code"] == "context_uncompactable"


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
    # This fixture exercises Goal prose flow, not the separately tested effect
    # ledger. In-memory SQLite connections can differ after route fixtures.
    from src import chat_effect_inbox
    monkeypatch.setattr(chat_effect_inbox, "needs_effect_intent", lambda *_: False)
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
    # The prose checkpoint requires a second round, but verified completion
    # must stop immediately rather than asking the model for a third round.
    assert rounds == 2
    assert work.get("alice", owned_chat)["goal"]["status"] == "completed"
    assert any('"type": "goal_update"' in chunk for chunk in chunks)


def test_completed_goal_fences_later_native_calls_in_same_batch(monkeypatch, owned_chat):
    """A model may send completion and stale progress in one native batch."""
    from src import chat_effect_inbox

    monkeypatch.setattr(chat_effect_inbox, "needs_effect_intent", lambda *_: False)
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, False))
    work = ChatWorkStore()
    goal = work.ensure_goal("alice", owned_chat, "Finish after verification")
    executed = []

    async def transport(_candidates, _messages, **_kwargs):
        yield 'data: ' + json.dumps({"type": "tool_calls", "calls": [
            {"name": "complete_goal", "arguments": json.dumps({
                "summary": "Verified result 2", "evidence": ["Python stdout was 2"],
            })},
            {"name": "update_goal_progress", "arguments": json.dumps({
                "progress": "Continue after completion",
            })},
        ]}) + '\n\n'
        yield 'data: [DONE]\n\n'

    async def execute(block, **_kwargs):
        executed.append(block.tool_type)
        if block.tool_type == "complete_goal":
            updated = work.complete_goal("alice", owned_chat, "Verified result 2", ["Python stdout was 2"])
            return block.tool_type, {"goal_update": updated, "output": "Goal completed", "exit_code": 0}
        return block.tool_type, {"error": "should not execute", "exit_code": 1}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", transport)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)

    async def collect():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            "http://model.test/v1", "kat-coder-v2.5-test",
            [{"role": "user", "content": "Finish after verification"}],
            owner="alice", session_id=owned_chat, active_goal=goal,
            relevant_tools={"complete_goal", "update_goal_progress"},
            max_rounds=1, _is_teacher_run=True,
        )]

    chunks = asyncio.run(collect())
    assert executed == ["complete_goal"]
    assert work.get("alice", owned_chat)["goal"]["status"] == "completed"
    assert any('"type": "context_checkpoint"' in chunk for chunk in chunks)
