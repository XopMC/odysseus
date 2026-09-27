"""Background continuation restores the job's authority, never session guesses."""
import json
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatRunState, ModelEndpoint, Session
from core.models import Session as ChatSession, ChatMessage
from src import background_followup_context as followup, subagent_recovery_config as recovery
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
                             is_enabled=True, api_key="old-token"))
        db.add(ChatRunState(run_id=parent, session_id="s", owner="alice", status="done"))
    privileges = {"can_use_agent": True, "can_use_bash": True, "allowed_models": ["worker"]}
    state = SimpleNamespace(access="full_access", disabled=[])
    monkeypatch.setattr(followup, "SessionLocal", factory)
    monkeypatch.setattr(followup, "_current_agent_privileges", lambda owner: dict(privileges))
    monkeypatch.setattr(followup, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(followup, "get_access_mode_for_user", lambda owner: state.access)
    monkeypatch.setattr(followup, "get_setting", lambda key, default=None: state.disabled if key == "disabled_tools" else default)
    monkeypatch.setattr(recovery, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(recovery, "AGENT_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "0")
    sess = ChatSession(id="s", name="QA", endpoint_url=endpoint, model="worker", owner="alice",
                       headers={"Authorization": "Bearer stale-session-token"},
                       history=[ChatMessage("user", "Finish the project")])
    args = dict(owner="alice", session_id="s", parent_run_id=parent, endpoint_url=endpoint,
                model="worker", workspace=str(tmp_path), access_mode="full_access",
                disabled_tools={"send_email"}, tool_policy=ToolPolicy(hidden_tools=frozenset({"manage_memory"})),
                delegated_credential=False)
    rec = {"id": "job", "session_id": "s", "status": "done", "exit_code": 0,
           "command": "printf done", "output": "done", "followup_context": followup.capture_background_followup_context(**args)}
    yield SimpleNamespace(factory=factory, sess=sess, args=args, rec=rec, state=state,
                          privileges=privileges, parent=parent, workspace=str(tmp_path))
    engine.dispose()


def test_fresh_credentials_workspace_and_restrictive_policy(case):
    with case.factory.begin() as db:
        db.get(ModelEndpoint, "ep").api_key = "rotated-token"
    case.state.access = "ask_every_time"
    case.state.disabled = ["python"]
    case.privileges["can_use_bash"] = False
    result = followup.prepare_background_followup(case.sess, case.rec)
    assert result["headers"]["Authorization"] == "Bearer rotated-token"
    assert result["workspace"] == case.workspace
    assert result["access_mode"] == "ask_every_time"
    assert result["history_session"] is case.sess
    assert result["disabled_tools"] >= {"send_email", "manage_memory", "python", "bash", "apply_patch"}
    assert result["external_untrusted_context_seen"] is True
    assert len(case.sess.history) == 1
    assert "Background job job finished" in result["messages"][-1]["content"]
    encoded = json.dumps(case.rec["followup_context"])
    assert "token" not in encoded and "headers" not in encoded and "Finish the project" not in encoded


@pytest.mark.parametrize("change,code", [
    ("legacy", "provenance_unavailable"), ("owner", "parent_unavailable"),
    ("agent", "agent_revoked"), ("endpoint", "endpoint_unavailable"),
    ("stopped", "parent_stopped"), ("model", "model_revoked"),
    ("delegated", "delegated_continuation_unavailable"),
    ("host_switch", "execution_binding_changed"),
])
def test_revocation_never_calls_provider(case, monkeypatch, change, code):
    def no_provider(*args, **kwargs):
        pytest.fail("Provider credentials resolved after authority rejection")
    monkeypatch.setattr(followup, "resolve_endpoint_runtime", no_provider)
    with case.factory.begin() as db:
        if change == "owner":
            db.get(Session, "s").owner = "bob"
        if change == "endpoint":
            db.get(ModelEndpoint, "ep").is_enabled = False
        if change == "stopped":
            db.get(ChatRunState, case.parent).continuation = {"terminal_reason": "user_stop"}
    if change == "legacy":
        case.rec.pop("followup_context")
    elif change == "agent":
        case.privileges["can_use_agent"] = False
    elif change == "model":
        case.privileges["allowed_models"] = ["other"]
    elif change == "delegated":
        case.rec["followup_context"]["delegated_credential"] = True
    elif change == "host_switch":
        monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
        monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "alice")
        monkeypatch.setenv("ODYSSEUS_HOST_TARGET", "alice@fixture")
    with pytest.raises(followup.BackgroundFollowupContextError) as error:
        followup.prepare_background_followup(case.sess, case.rec)
    assert error.value.code == code


def test_host_revocation_never_falls_back_to_container(case, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "alice")
    monkeypatch.setenv("ODYSSEUS_HOST_TARGET", "alice@fixture")
    case.rec["followup_context"] = followup.capture_background_followup_context(**case.args)
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "0")
    with pytest.raises(followup.BackgroundFollowupContextError, match="authority") as error:
        followup.prepare_background_followup(case.sess, case.rec)
    assert error.value.code == "execution_binding_changed"


def test_original_approval_mode_and_current_user_denial_survive(case):
    case.rec["followup_context"]["access_mode"] = "ask_important"
    case.sess.history.append(ChatMessage("user", "Do not use tools"))
    result = followup.prepare_background_followup(case.sess, case.rec)
    assert result["access_mode"] == "ask_important"
    assert result["tool_policy"].block_all_tool_calls


def test_new_parent_cannot_supply_job_context(case):
    with case.factory.begin() as db:
        db.add(ChatRunState(run_id="b" * 32, session_id="s", owner="alice", status="done",
                            continuation={"workspace": "/different"}))
    with pytest.raises(followup.BackgroundFollowupContextError) as error:
        followup.prepare_background_followup(case.sess, case.rec)
    assert error.value.code == "parent_superseded"


def test_launch_persists_exact_dispatch_context_and_restores_relevance(case, tmp_path, monkeypatch):
    from src import bg_jobs, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext

    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=123))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="bash", content="#!bg\nprintf done"),
        owner="alice", session_id="s", workspace=case.workspace,
        current_endpoint_url=case.sess.endpoint_url, current_model="worker",
        durable_run_id=case.parent,
        security_context=ToolRunSecurityContext(access_mode="full_access"),
        disabled_tools={"send_email"}, tool_policy=ToolPolicy(), allowed_tools={"bash", "read_file"},
    ))
    assert result["bg_job_id"]
    saved = bg_jobs._load()[result["bg_job_id"]]
    assert saved["followup_context"]["workspace"] == case.workspace
    assert saved["followup_context"]["parent_run_id"] == case.parent
    assert saved["followup_context"]["access_mode"] == "full_access"
    assert saved["followup_context"]["disabled_tools"] == ["send_email"]
    prepared = followup.prepare_background_followup(case.sess, saved)
    assert prepared["relevant_tools"] == {"bash", "read_file"}


def test_unexplained_interrupted_parent_cannot_continue(case):
    with case.factory.begin() as db:
        db.get(ChatRunState, case.parent).status = "interrupted"
    with pytest.raises(followup.BackgroundFollowupContextError) as error:
        followup.prepare_background_followup(case.sess, case.rec)
    assert error.value.code == "parent_not_ready"
