"""Unknown Agent effects require owner-scoped, explicit reconciliation."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, Session
from src.chat_work_store import WorkConflict, WorkNotFound
from src.chat_effect_inbox import needs_effect_intent


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    from core.database import ChatToolIntent, ChatWorkEvent
    from src import chat_effect_inbox

    engine = create_engine(f"sqlite:///{tmp_path / 'effects.db'}")
    Base.metadata.create_all(bind=engine, tables=[Session.__table__, ChatToolIntent.__table__, ChatWorkEvent.__table__])
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(chat_effect_inbox, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(Session(id="owned-chat", name="Safe fixture", owner="alice",
                       endpoint_url="http://fixture.invalid/v1", model="fixture"))
    yield chat_effect_inbox.ChatEffectInbox(), factory
    engine.dispose()


def test_unknown_effect_is_durable_redacted_and_owner_scoped(inbox):
    store, factory = inbox
    secret_action = '{"command":"echo private-marker"}'
    intent = store.record_intent("alice", "owned-chat", "a" * 32, "call-1", "bash", secret_action)
    assert intent["created"] is True
    assert intent["status"] == "intent"
    assert intent["action_hash"] == hashlib.sha256(secret_action.encode()).hexdigest()
    assert "private-marker" not in json.dumps(intent)
    assert store.record_intent("alice", "owned-chat", "a" * 32, "call-1", "bash", secret_action)["created"] is False
    with pytest.raises(WorkConflict):
        store.record_intent("alice", "owned-chat", "a" * 32, "call-1", "bash", "different")
    store.mark_unknown("alice", "owned-chat", intent["id"])
    assert store.unresolved("alice", "owned-chat")[0]["status"] == "unknown"
    with pytest.raises(WorkNotFound):
        store.unresolved("bob", "owned-chat")
    with pytest.raises(WorkNotFound):
        store.mark_unknown("bob", "owned-chat", intent["id"])
    from core.database import ChatToolIntent
    with factory() as db:
        row = db.query(ChatToolIntent).filter_by(id=intent["id"]).one()
        assert "private-marker" not in json.dumps({"action_hash": row.action_hash, "receipt": row.receipt})


@pytest.mark.parametrize("action", ["resume", "restore", "lease"])
@pytest.mark.parametrize("effect_status", ["unknown", "verified_not_applied"])
def test_goal_continuation_retains_real_effect_receipt_and_exact_action_fence(inbox, monkeypatch, action, effect_status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import chat_work_routes
    from src import agent_runs, chat_effect_inbox, goal_controller

    effects, _factory = inbox
    payload = '{"command":"printf owned-test"}'
    intent = effects.record_intent("alice", "owned-chat", "a" * 32, "call-1", "bash", payload)
    receipt = effects.mark_unknown("alice", "owned-chat", intent["id"])
    if effect_status == "verified_not_applied":
        receipt = effects.verify("alice", "owned-chat", intent["id"],
            expected_revision=receipt["revision"], outcome="not_applied", evidence="checked without replay")
    before = effects.pending_actions("alice", "owned-chat")
    dispatched = []

    class GoalStore:
        def goal_action(self, owner, session_id, requested, revision):
            assert (owner, session_id, requested, revision) == ("alice", "owned-chat", action, 5)
            return {"status": "active", "revision": 6}

        def acquire_goal_lease(self, owner, session_id):
            assert (owner, session_id) == ("alice", "owned-chat")
            return "lease-token"

    async def dispatch(owner, session_id, *, reason):
        dispatched.append((owner, session_id, reason))
        return True

    monkeypatch.setattr(chat_effect_inbox, "inbox", effects)
    monkeypatch.setattr(chat_work_routes, "store", GoalStore())
    monkeypatch.setattr(chat_work_routes, "_verify_session_owner", lambda request, session_id: None)
    monkeypatch.setattr(chat_work_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(agent_runs, "is_active", lambda session_id: False)
    monkeypatch.setattr(goal_controller, "dispatch_goal_continuation", dispatch)
    app = FastAPI(); app.include_router(chat_work_routes.setup_chat_work_routes())
    path = "goal-lease" if action == "lease" else f"goal/{action}"
    with TestClient(app) as client:
        response = client.post(f"/api/chat/work/owned-chat/{path}",
            json={} if action == "lease" else {"expected_revision": 5})
    assert response.status_code == 200, response.text
    assert effects.pending_actions("alice", "owned-chat") == before
    assert dispatched == ([] if action == "lease" else [("alice", "owned-chat", f"goal_{'resumed' if action == 'resume' else 'restored'}")])
    if effect_status == "unknown":
        repeated = effects.record_intent("alice", "owned-chat", "b" * 32, "call-2", "bash", payload)
        assert repeated["id"] == intent["id"] and repeated["created"] is False
    else:
        with pytest.raises(WorkConflict, match="explicit retry authorization required"):
            effects.record_intent("alice", "owned-chat", "b" * 32, "call-2", "bash", payload)
    independent = effects.record_intent("alice", "owned-chat", "b" * 32, "call-3", "bash", '{"command":"printf independent"}')
    assert independent["created"] is True


def test_parallel_children_can_record_same_round_tool_ordinal(inbox):
    store, _factory = inbox
    first = store.record_intent(
        "alice", "owned-chat", "a" * 32, "round-1-tool-0", "python", '{"expr":"19*19"}',
    )
    second = store.record_intent(
        "alice", "owned-chat", "b" * 32, "round-1-tool-0", "python", '{"expr":"23*23"}',
    )
    assert first["created"] is True
    assert second["created"] is True
    assert first["id"] != second["id"]


def test_operator_reconciles_logged_pre_spawn_failure_without_replay(inbox):
    store, factory = inbox
    intent = store.record_intent("alice", "owned-chat", "c" * 32,
                                 "round-51-tool-0", "bash", "#!bg\necho safe")
    unknown = store.mark_unknown("alice", "owned-chat", intent["id"])
    evidence = hashlib.sha256(b"Popen ENOENT cwd; no child created").hexdigest()
    with pytest.raises(WorkConflict):
        store.reconcile_proven_not_started(
            "alice", "owned-chat", intent["id"], expected_revision=unknown["revision"],
            run_id="d" * 32, tool_call_id="round-51-tool-0", evidence_sha256=evidence,
        )
    settled = store.reconcile_proven_not_started(
        "alice", "owned-chat", intent["id"], expected_revision=unknown["revision"],
        run_id="c" * 32, tool_call_id="round-51-tool-0", evidence_sha256=evidence,
    )
    assert settled["status"] == "no_retry"
    assert store.unknown("alice", "owned-chat") == []
    from core.database import ChatToolIntent
    with factory() as db:
        receipt = db.query(ChatToolIntent).filter_by(id=intent["id"]).one().receipt
    assert receipt["server_non_dispatch"]["evidence_sha256"] == evidence
    assert "safe" not in json.dumps(receipt)


def test_reconciliation_is_cas_fenced_and_never_dispatches(inbox):
    store, _factory = inbox
    intent = store.record_intent("alice", "owned-chat", "b" * 32, "call-2", "write_file", "path+body")
    store.mark_unknown("alice", "owned-chat", intent["id"])
    current = store.unresolved("alice", "owned-chat")[0]
    with pytest.raises(WorkConflict):
        store.no_retry("alice", "owned-chat", intent["id"],
                       expected_revision=current["revision"] + 1)
    resolved = store.no_retry("alice", "owned-chat", intent["id"],
                              expected_revision=current["revision"])
    assert resolved["status"] == "no_retry"
    assert store.unresolved("alice", "owned-chat") == []
    with pytest.raises(WorkConflict):
        store.no_retry("alice", "owned-chat", intent["id"],
                       expected_revision=resolved["revision"])


def test_interrupted_run_promotes_open_intents_without_replaying(inbox):
    store, _factory = inbox
    open_intent = store.record_intent("alice", "owned-chat", "d" * 32, "call-open", "bash", "effect")
    done_intent = store.record_intent("alice", "owned-chat", "d" * 32, "call-done", "write_file", "known")
    done = store.record_result("alice", "owned-chat", done_intent["id"], {"exit_code": 0, "output": "ok"})
    assert done["status"] == "done"
    assert store.mark_interrupted_run_unknown("alice", "owned-chat", "d" * 32) == 1
    assert store.mark_interrupted_run_unknown("alice", "owned-chat", "d" * 32) == 0
    assert [item["id"] for item in store.unresolved("alice", "owned-chat")] == [open_intent["id"]]
    assert store.unresolved("alice", "owned-chat")[0]["status"] == "unknown"


def test_unknown_tool_result_is_not_recorded_as_success(inbox):
    store, _factory = inbox
    intent = store.record_intent("alice", "owned-chat", "e" * 32, "call-unknown", "bash", "effect")
    result = store.record_result("alice", "owned-chat", intent["id"], {
        "exit_code": 1, "outcome_unknown": True, "error": "reply lost",
    })
    assert result["status"] == "unknown"
    assert result["receipt_hash"] is not None
    assert "reply lost" not in json.dumps(result)


def test_unknown_exact_action_is_returned_as_fenced_not_reexecuted(inbox):
    store, _factory = inbox
    action = '{"command":"echo fixture"}'
    first = store.record_intent("alice", "owned-chat", "e" * 32, "call-first", "bash", action)
    unknown = store.mark_unknown("alice", "owned-chat", first["id"])

    assert store.unknown_match("alice", "owned-chat", "bash", action)["id"] == first["id"]
    retry = store.record_intent("alice", "owned-chat", "f" * 32, "call-second", "bash", action)
    assert retry["created"] is False
    assert retry["id"] == first["id"]
    assert retry["status"] == "unknown"
    # The fence is exact-action scoped; unrelated progress is still possible.
    other = store.record_intent(
        "alice", "owned-chat", "f" * 32, "call-other", "bash", '{"command":"pwd"}',
    )
    assert other["created"] is True
    assert unknown["status"] == "unknown"


def test_no_retry_preserves_prior_unknown_result_digest(inbox):
    store, factory = inbox
    item = store.record_intent("alice", "owned-chat", "e" * 32, "call-preserve", "bash", "effect")
    unknown = store.record_result("alice", "owned-chat", item["id"], {
        "exit_code": 1, "outcome_unknown": True, "error": "reply lost",
    })
    prior_digest = unknown["receipt_hash"]
    store.no_retry("alice", "owned-chat", item["id"], expected_revision=unknown["revision"])
    from core.database import ChatToolIntent
    with factory() as db:
        row = db.query(ChatToolIntent).filter_by(id=item["id"]).one()
        assert row.receipt["result_sha256"] == prior_digest
        assert row.receipt["decision"]["kind"] == "user_no_retry"


def test_no_retry_requires_exact_revision_and_generates_server_receipt(inbox):
    store, factory = inbox
    item = store.record_intent("alice", "owned-chat", "f" * 32, "call-no-retry", "bash", "private command")
    unknown = store.mark_unknown("alice", "owned-chat", item["id"])
    with pytest.raises(WorkConflict):
        store.no_retry("alice", "owned-chat", item["id"], expected_revision=unknown["revision"] + 1)
    settled = store.no_retry("alice", "owned-chat", item["id"], expected_revision=unknown["revision"])
    assert settled["status"] == "no_retry"
    assert len(settled["receipt_hash"]) == 64
    assert store.unknown("alice", "owned-chat") == []
    with pytest.raises(WorkConflict):
        store.no_retry("alice", "owned-chat", item["id"], expected_revision=unknown["revision"])
    from core.database import ChatToolIntent, ChatWorkEvent
    with factory() as db:
        row = db.query(ChatToolIntent).filter_by(id=item["id"]).one()
        assert row.receipt["decision"]["kind"] == "user_no_retry"
        assert "private command" not in json.dumps(row.receipt)
        event = db.query(ChatWorkEvent).filter_by(entity_id=item["id"], kind="effect_reconciled").one()
        assert event.payload == {"intent_id": item["id"], "status": "no_retry"}


def test_no_retry_fences_exact_action_within_goal_but_not_other_actions(inbox):
    store, _factory = inbox
    goal_started = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    action = '{"code":"import time; time.sleep(90); print(49)"}'
    item = store.record_intent("alice", "owned-chat", "a" * 32, "call-old",
                               "python", action, goal_created_at=goal_started)
    unknown = store.mark_unknown("alice", "owned-chat", item["id"])
    store.no_retry("alice", "owned-chat", item["id"], expected_revision=unknown["revision"])
    assert store.no_retry_match("alice", "owned-chat", "python", action,
                                goal_created_at=goal_started)["id"] == item["id"]
    with pytest.raises(WorkConflict, match="Do not retry"):
        store.record_intent("alice", "owned-chat", "b" * 32, "call-repeated",
                            "python", action, goal_created_at=goal_started)
    assert store.record_intent(
        "alice", "owned-chat", "b" * 32, "call-short", "python",
        '{"code":"print(49)"}', goal_created_at=goal_started,
    )["created"]
    # A later, unrelated Goal must not inherit this user decision.
    later_goal = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    assert store.no_retry_match("alice", "owned-chat", "python", action,
                                goal_created_at=later_goal) is None
    assert store.record_intent("alice", "owned-chat", "c" * 32, "call-new-goal",
                               "python", action, goal_created_at=later_goal)["created"]


def test_verified_not_applied_requires_one_shot_exact_hash_retry_authorization(inbox):
    store, factory = inbox
    action = '{"path":"fixture.txt","content":"safe"}'
    item = store.record_intent("alice", "owned-chat", "1" * 32, "call-unknown",
                               "write_file", action)
    unknown = store.mark_unknown("alice", "owned-chat", item["id"])
    with pytest.raises(WorkConflict):
        store.verify(
            "alice", "owned-chat", item["id"], expected_revision=unknown["revision"] + 1,
            outcome="not_applied", evidence="stale evidence",
        )
    with pytest.raises(ValueError):
        store.verify(
            "alice", "owned-chat", item["id"], expected_revision=unknown["revision"],
            outcome="maybe", evidence="invalid outcome",
        )
    verified = store.verify(
        "alice", "owned-chat", item["id"], expected_revision=unknown["revision"],
        outcome="not_applied", evidence="Independent check: destination is absent",
    )
    assert verified["status"] == "verified_not_applied"
    assert len(verified["receipt_hash"]) == 64
    assert "destination is absent" not in json.dumps(verified)
    assert "Independent check" not in json.dumps(verified)
    assert store.pending_actions("alice", "owned-chat")[0]["status"] == "verified_not_applied"
    with pytest.raises(WorkNotFound):
        store.verify(
            "bob", "owned-chat", item["id"], expected_revision=verified["revision"],
            outcome="not_applied", evidence="foreign owner evidence",
        )

    with pytest.raises(WorkConflict, match="explicit retry authorization"):
        store.record_intent("alice", "owned-chat", "2" * 32, "call-unapproved",
                            "write_file", action)
    authorized = store.authorize_retry(
        "alice", "owned-chat", item["id"], expected_revision=verified["revision"],
    )
    assert authorized["status"] == "retry_authorized"
    with pytest.raises(WorkConflict):
        store.authorize_retry("alice", "owned-chat", item["id"],
                              expected_revision=verified["revision"])

    unrelated = store.record_intent(
        "alice", "owned-chat", "2" * 32, "call-other-tool", "bash", action,
    )
    assert unrelated["created"] is True
    assert unrelated["retry_authorization_id"] is None
    retried = store.record_intent(
        "alice", "owned-chat", "2" * 32, "call-authorized", "write_file", action,
    )
    assert retried["created"] is True
    assert retried["retry_authorization_id"] == item["id"]
    with factory() as db:
        from core.database import ChatToolIntent
        original = db.query(ChatToolIntent).filter_by(id=item["id"]).one()
        assert original.status == "retry_consumed"
        assert original.receipt["retry_consumed"]["action_hash"] == item["action_hash"]


def test_verified_applied_is_terminal_and_does_not_offer_retry(inbox):
    store, _factory = inbox
    item = store.record_intent("alice", "owned-chat", "3" * 32, "call-applied", "bash", "safe")
    unknown = store.mark_unknown("alice", "owned-chat", item["id"])
    verified = store.verify(
        "alice", "owned-chat", item["id"], expected_revision=unknown["revision"],
        outcome="applied", evidence="Independent check: expected marker exists",
    )
    assert verified["status"] == "verified"
    assert store.pending_actions("alice", "owned-chat") == []
    with pytest.raises(WorkConflict):
        store.authorize_retry("alice", "owned-chat", item["id"],
                              expected_revision=verified["revision"])


def test_unknown_fence_is_not_hidden_behind_200_open_intents(inbox):
    store, factory = inbox
    from core.database import ChatToolIntent
    with factory.begin() as db:
        for index in range(201):
            db.add(ChatToolIntent(
                id=f"intent-{index:03}", owner="alice", session_id="owned-chat",
                run_id="a" * 32, tool_call_id=f"call-{index}", tool_name="bash",
                action_hash="b" * 64, status="intent", revision=1,
            ))
        db.add(ChatToolIntent(
            id="unknown-last", owner="alice", session_id="owned-chat",
            run_id="a" * 32, tool_call_id="call-unknown", tool_name="bash",
            action_hash="c" * 64, status="unknown", revision=2,
        ))
    assert [row["id"] for row in store.unknown("alice", "owned-chat")] == ["unknown-last"]


def test_effect_classification_fails_closed_for_unknown_tool():
    assert needs_effect_intent("bash", "echo safe") is True
    assert needs_effect_intent("write_file", "{}") is True
    assert needs_effect_intent("unknown_extension_tool", "{}") is True
    assert needs_effect_intent("read_file", "{}") is False
