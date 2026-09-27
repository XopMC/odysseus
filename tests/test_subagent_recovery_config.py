"""Recovery refreshes authority and credentials without storing either as grants."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatGoal, ChatRunState, ChatSubagentDelivery, ChatSubagentRun, ModelEndpoint, Session
from src import subagent_recovery_config as recovery
from src.tool_policy import ToolPolicy


@pytest.fixture
def case(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    endpoint = "http://127.0.0.1:9999/v1"
    parent = "a" * 32
    with factory.begin() as db:
        db.add(Session(id="s", owner="alice", name="QA", endpoint_url=endpoint, model="worker"))
        db.flush()
        db.add(ModelEndpoint(id="ep", name="Fixture", owner="alice", base_url=endpoint,
                             is_enabled=True, cached_models='["worker"]'))
        db.add(ChatRunState(run_id=parent, session_id="s", owner="alice", status="running",
                            continuation={"goal": False}))
    settings = {"agent_subagents_mode": "selected_models", "agent_subagent_models": ["worker@ep"],
                "agent_subagent_model_limits": {"worker@ep": 2}, "disabled_tools": []}
    privileges = {"can_use_agent": True, "can_use_bash": True, "can_use_browser": True,
                  "allowed_models": ["worker"]}
    state = SimpleNamespace(access="full_access", key="first-ephemeral-token", resolutions=0)
    monkeypatch.setattr(recovery, "get_setting", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(recovery, "_current_agent_privileges", lambda owner: dict(privileges))
    monkeypatch.setattr(recovery, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(recovery, "get_access_mode_for_user", lambda owner: state.access)
    monkeypatch.setattr(recovery, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(recovery, "AGENT_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "0")

    def credentials(ep, owner):
        assert ep.id == "ep" and owner == "alice"
        state.resolutions += 1
        return endpoint, state.key
    monkeypatch.setattr(recovery, "resolve_endpoint_runtime", credentials)
    capture_args = dict(owner="alice", session_id="s", parent_run_id=parent,
                        endpoint_url=endpoint + "/chat/completions", endpoint_id="ep", model="worker",
                        workspace=str(tmp_path), access_mode="full_access", disabled_tools={"send_email"},
                        tool_policy=ToolPolicy(hidden_tools=frozenset({"manage_memory"})),
                        delegated_credential=False, external_untrusted_context_seen=True,
                        max_active_for_model=4, db_factory=factory)
    value = SimpleNamespace(factory=factory, capture_args=capture_args, settings=settings,
                            privileges=privileges, state=state, endpoint=endpoint, parent=parent)
    yield value
    engine.dispose()


def capture_child(case):
    snapshot = recovery.capture_config(**case.capture_args)
    with case.factory.begin() as db:
        db.get(ChatRunState, case.parent).status = "done"
        db.add(ChatSubagentRun(id="child", parent_session_id="s", parent_run_id=case.parent,
                              owner="alice", ordinal=1, name="Worker", objective="QA",
                              assigned_context="", model="worker", endpoint_id=snapshot["route_id"], status="recovering",
                              policy_snapshot={"recovery_config": snapshot}))
    return snapshot


def prepare(case, snapshot):
    return recovery.prepare_config(owner="alice", session_id="s", child_id="child",
                                   snapshot=snapshot, db_factory=case.factory)


def test_restore_uses_fresh_credentials_and_restrictive_current_policy(case):
    snapshot = capture_child(case)
    assert case.state.resolutions == 0
    encoded = json.dumps(snapshot)
    assert "token" not in encoded and "headers" not in encoded and "api_key" not in encoded
    assert case.endpoint not in encoded
    case.state.key = "rotated-ephemeral-token"
    case.state.access = "ask_every_time"
    case.settings["disabled_tools"] = ["python"]
    restored = prepare(case, snapshot)
    assert restored["headers"]["Authorization"] == "Bearer rotated-ephemeral-token"
    assert restored["endpoint_url"] == case.endpoint + "/chat/completions"
    assert restored["access_mode"] == "ask_every_time"
    assert restored["disabled_tools"] >= {"send_email", "manage_memory", "python"}
    assert restored["tool_policy"].hidden_tools == frozenset({"manage_memory"})
    assert restored["max_active_for_model"] == 2
    assert restored["allowed_tools"] is None and restored["timeout_seconds"] == 600
    assert restored["external_untrusted_context_seen"] is True
    assert json.dumps(snapshot) == encoded


def test_current_full_access_cannot_widen_original_approval_mode(case):
    case.capture_args["access_mode"] = "ask_important"
    case.capture_args["tool_policy"] = ToolPolicy(mode="guide_only", block_all_tool_calls=True,
                                                disable_mcp=True, reasons={"x": "not persisted"})
    snapshot = capture_child(case)
    restored = prepare(case, snapshot)
    assert restored["access_mode"] == "ask_important"
    assert restored["tool_policy"].block_all_tool_calls is True
    assert restored["tool_policy"].disable_mcp is True
    assert "not persisted" not in json.dumps(snapshot)


@pytest.mark.parametrize("change", ["owner", "endpoint_disabled", "endpoint_owner", "endpoint_base",
                                    "parent_stop", "parent_missing", "model_hidden", "model_allowlist",
                                    "global_off", "privilege", "delegated", "host_switch", "workspace"])
def test_authority_revocation_fails_before_credentials(case, change, monkeypatch):
    if change == "delegated":
        case.capture_args["delegated_credential"] = True
    snapshot = capture_child(case)
    with case.factory.begin() as db:
        if change == "owner":
            db.get(Session, "s").owner = "bob"
        elif change == "endpoint_disabled":
            db.get(ModelEndpoint, "ep").is_enabled = False
        elif change == "endpoint_owner":
            db.get(ModelEndpoint, "ep").owner = "bob"
        elif change == "endpoint_base":
            db.get(ModelEndpoint, "ep").base_url = "http://127.0.0.1:8888/v1"
        elif change == "parent_stop":
            row = db.get(ChatRunState, case.parent)
            row.status, row.continuation = "stopped", {"terminal_reason": "user_stop"}
        elif change == "parent_missing":
            db.delete(db.get(ChatRunState, case.parent))
        elif change == "model_hidden":
            db.get(ModelEndpoint, "ep").hidden_models = '["worker"]'
    if change == "model_allowlist":
        case.settings["agent_subagent_models"] = ["other@ep"]
    elif change == "global_off":
        case.settings["agent_subagents_mode"] = "off"
    elif change == "privilege":
        case.privileges["can_use_agent"] = False
    elif change == "host_switch":
        monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
        monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "alice")
    elif change == "workspace":
        monkeypatch.setattr(recovery, "vet_workspace_for_owner", lambda *_: None)
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        prepare(case, snapshot)
    assert caught.value.retryable is False
    assert case.state.resolutions == 0


@pytest.mark.parametrize("url", ["https://user:password@example.org/v1",
                                  "https://example.org/v1?key=secret",
                                  "https://example.org/v1#secret"])
def test_capture_rejects_url_credentials_without_echo(case, url):
    case.capture_args["endpoint_url"] = url
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        recovery.capture_config(**case.capture_args)
    assert "secret" not in str(caught.value) and "password" not in str(caught.value)


def test_capture_requires_unique_exact_endpoint_not_label(case):
    case.capture_args["endpoint_id"] = None
    assert recovery.capture_config(**case.capture_args)["endpoint_id"] == "ep"
    with case.factory.begin() as db:
        db.add(ModelEndpoint(id="duplicate", name="Different label", owner="alice",
                             base_url=case.endpoint, is_enabled=True))
    with pytest.raises(recovery.RecoveryUnavailable):
        recovery.capture_config(**case.capture_args)
    case.capture_args["endpoint_id"] = "Fixture"
    with pytest.raises(recovery.RecoveryUnavailable):
        recovery.capture_config(**case.capture_args)


@pytest.mark.parametrize("status", ["paused", "cancelled", "completed", "waiting_user", "review_required"])
def test_goal_recovery_requires_same_active_objective(case, status):
    with case.factory.begin() as db:
        db.get(ChatRunState, case.parent).continuation = {"goal": True}
        db.add(ChatGoal(id="goal", owner="alice", session_id="s", objective="Original QA", status="active"))
    snapshot = capture_child(case)
    assert prepare(case, snapshot)["model"] == "worker"
    with case.factory.begin() as db:
        db.get(ChatGoal, "goal").status = status
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)


def test_revised_goal_and_tampered_snapshot_are_rejected(case):
    with case.factory.begin() as db:
        db.get(ChatRunState, case.parent).continuation = {"goal": True}
        db.add(ChatGoal(id="goal", owner="alice", session_id="s", objective="Original QA", status="active"))
    snapshot = capture_child(case)
    with case.factory.begin() as db:
        db.get(ChatGoal, "goal").objective = "Changed QA"
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)
    altered = copy.deepcopy(snapshot)
    altered["access_mode"] = "ask_every_time"
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, altered)


def test_process_restarted_parent_is_eligible_but_not_arbitrary_error(case):
    snapshot = capture_child(case)
    with case.factory.begin() as db:
        row = db.get(ChatRunState, case.parent)
        row.status, row.continuation = "interrupted", {"terminal_reason": "process_restarted"}
    assert prepare(case, snapshot)["model"] == "worker"
    with case.factory.begin() as db:
        db.get(ChatRunState, case.parent).continuation = {"terminal_reason": "user_stop"}
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)


def test_host_binding_and_validated_workspace_are_preserved(case, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "alice")
    monkeypatch.setenv("ODYSSEUS_HOST_TARGET", "fixture@host")
    monkeypatch.setenv("ODYSSEUS_HOST_HELPER", "/fixed/helper.py")
    monkeypatch.setenv("ODYSSEUS_HOST_CWD", "/default")
    case.capture_args["workspace"] = "/selected"
    monkeypatch.setattr(recovery, "vet_workspace_for_owner", lambda path, owner: path)
    snapshot = capture_child(case)
    assert "fixture@host" not in json.dumps(snapshot)
    assert prepare(case, snapshot)["workspace"] == "/selected"
    monkeypatch.setenv("ODYSSEUS_HOST_HELPER", "/other/helper.py")
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)


def test_provider_refresh_errors_are_fixed_and_retryable(case, monkeypatch):
    snapshot = capture_child(case)
    def unavailable(*args, **kwargs):
        raise OSError("private bearer token and provider response")
    monkeypatch.setattr(recovery, "resolve_endpoint_runtime", unavailable)
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        prepare(case, snapshot)
    assert caught.value.retryable is True
    assert "private" not in str(caught.value)


def test_latest_delivery_run_needs_exact_original_parent_lineage(case):
    snapshot = capture_child(case)
    newer = "b" * 32
    with case.factory.begin() as db:
        parent = db.get(ChatRunState, case.parent)
        db.add(ChatRunState(run_id=newer, session_id="s", owner="alice", status="done",
                            started_at=parent.started_at + timedelta(seconds=1), continuation={}))
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        prepare(case, snapshot)
    assert caught.value.code == "parent_superseded"
    with case.factory.begin() as db:
        db.add(ChatSubagentRun(id="sibling", owner="alice", parent_session_id="s",
                              parent_run_id=case.parent, ordinal=2, name="QA", objective="QA",
                              assigned_context="", model="worker", status="completed"))
        db.flush()
        db.add(ChatSubagentDelivery(child_id="sibling", owner="alice", parent_session_id="s",
                                   parent_run_id=case.parent, delivered_run_id=newer, status="delivered"))
    assert prepare(case, snapshot)["model"] == "worker"
    with case.factory.begin() as db:
        db.get(ChatSubagentRun, "sibling").parent_run_id = "c" * 32
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)


@pytest.mark.parametrize("status,retryable", [("paused", True), ("waiting_user", True),
                                             ("completed", False), ("cancelled", False)])
def test_goal_wait_retry_classification(case, status, retryable):
    with case.factory.begin() as db:
        db.get(ChatRunState, case.parent).continuation = {"goal": True}
        db.add(ChatGoal(id="goal", owner="alice", session_id="s", objective="QA", status="active"))
    snapshot = capture_child(case)
    with case.factory.begin() as db:
        db.get(ChatGoal, "goal").status = status
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        prepare(case, snapshot)
    assert caught.value.retryable is retryable


def test_mount_identity_change_is_not_a_valid_local_restart(case, monkeypatch):
    snapshot = capture_child(case)
    replacement = case.capture_args["workspace"] + "/replacement"
    import os
    os.mkdir(replacement)
    monkeypatch.setattr(recovery, "DATA_DIR", replacement)
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        prepare(case, snapshot)
    assert caught.value.code == "execution_binding_changed"


def test_credential_revocation_is_terminal_and_sanitized(case, monkeypatch):
    snapshot = capture_child(case)
    def revoked(*args, **kwargs):
        raise recovery.ChatGPTSubscriptionReauthRequired("private provider response")
    monkeypatch.setattr(recovery, "resolve_endpoint_runtime", revoked)
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        prepare(case, snapshot)
    assert caught.value.retryable is False
    assert caught.value.code == "credentials_revoked"
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("change", ["parent_stop", "child_cancel", "session_owner", "endpoint_owner",
                                    "endpoint_disabled", "endpoint_route", "goal_revision"])
def test_recovery_seal_rejects_changes_during_or_after_preparation(case, change):
    if change == "goal_revision":
        with case.factory.begin() as db:
            db.get(ChatRunState, case.parent).continuation = {"goal": True}
            db.add(ChatGoal(id="goal", owner="alice", session_id="s", objective="QA", status="active"))
    snapshot = capture_child(case)
    prepared = prepare(case, snapshot)
    with case.factory.begin() as db:
        assert recovery.validate_recovery_seal(db, owner="alice", session_id="s", child_id="child",
                                               seal=prepared["recovery_seal"])
    with case.factory.begin() as db:
        if change == "parent_stop":
            db.get(ChatRunState, case.parent).status = "stopped"
        elif change == "child_cancel":
            db.get(ChatSubagentRun, "child").cancel_requested = True
        elif change == "session_owner":
            db.get(Session, "s").owner = "bob"
        elif change == "endpoint_owner":
            db.get(ModelEndpoint, "ep").owner = "bob"
        elif change == "endpoint_disabled":
            db.get(ModelEndpoint, "ep").is_enabled = False
        elif change == "endpoint_route":
            db.get(ModelEndpoint, "ep").base_url = "http://127.0.0.1:8888/v1"
        else:
            db.get(ChatGoal, "goal").revision += 1
    with case.factory.begin() as db:
        assert not recovery.validate_recovery_seal(db, owner="alice", session_id="s", child_id="child",
                                                   seal=prepared["recovery_seal"])


def test_prepare_does_not_seal_a_parent_revocation_that_occurs_during_credentials(case, monkeypatch):
    snapshot = capture_child(case)
    original = recovery.resolve_endpoint_runtime
    def credentials(*args, **kwargs):
        with case.factory.begin() as db:
            db.get(ChatRunState, case.parent).status = "stopped"
        return original(*args, **kwargs)
    monkeypatch.setattr(recovery, "resolve_endpoint_runtime", credentials)
    prepared = prepare(case, snapshot)
    with case.factory.begin() as db:
        assert not recovery.validate_recovery_seal(db, owner="alice", session_id="s", child_id="child",
                                                   seal=prepared["recovery_seal"])


def test_same_model_url_route_retains_exact_endpoint_binding(case):
    case.settings["agent_subagents_mode"] = "same_model"
    case.capture_args["endpoint_id"] = None
    snapshot = capture_child(case)
    assert snapshot["endpoint_id"] == "ep"
    assert snapshot["route_id"].startswith("url-")
    assert prepare(case, snapshot)["max_active_for_model"] == 3
    with case.factory.begin() as db:
        db.get(ChatSubagentRun, "child").endpoint_id = "other"
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)


def test_seal_reads_fresh_db_rows_without_discarding_pending_cas_fields(case):
    snapshot = capture_child(case)
    sealed = prepare(case, snapshot)["recovery_seal"]
    with case.factory() as db:
        child = db.get(ChatSubagentRun, "child")
        child.worker_id = "pending-claim"
        assert recovery.validate_recovery_seal(db, owner="alice", session_id="s", child_id="child", seal=sealed)
        assert child.worker_id == "pending-claim"
        db.rollback()


def test_shared_endpoint_does_not_allow_another_owners_oauth_identity(case):
    from core.database import ProviderAuthSession
    with case.factory.begin() as db:
        endpoint = db.get(ModelEndpoint, "ep")
        endpoint.owner = None
        db.add(ProviderAuthSession(id="auth", owner="bob", provider="chatgpt", base_url=case.endpoint))
        endpoint.provider_auth_id = "auth"
    with pytest.raises(recovery.RecoveryUnavailable) as caught:
        recovery.capture_config(**case.capture_args)
    assert caught.value.code == "credential_owner_changed"


def test_current_private_model_and_tool_privileges_are_reapplied(case):
    snapshot = capture_child(case)
    case.privileges["allowed_models"] = []
    case.privileges["allowed_models_restricted"] = True
    with pytest.raises(recovery.RecoveryUnavailable):
        prepare(case, snapshot)
    case.privileges["allowed_models"] = ["worker"]
    case.privileges["can_use_bash"] = False
    case.privileges["can_use_browser"] = False
    case.settings["agent_subagent_model_limits"] = {"worker@ep": 1}
    restored = prepare(case, snapshot)
    assert restored["disabled_tools"] >= {"python", "write_file", "read_file", "bash", "builtin_browser"}
    assert restored["tool_policy"].disable_mcp is True
    assert restored["max_active_for_model"] == 1
