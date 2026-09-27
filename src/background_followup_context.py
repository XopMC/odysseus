"""Revalidate a background job's original authority before a headless turn.

The job owns this non-secret snapshot. Latest-session state is never a source
of missing provenance, and old jobs without a snapshot are delivery-only.
"""
from __future__ import annotations

import json
from functools import wraps

from core.database import ChatRunState, SessionLocal
from routes.prefs_routes import get_access_mode_for_user
from src import bg_jobs, host_execution
from src.chat_work_store import _storage_owner
from src.endpoint_resolver import build_chat_url, build_headers, resolve_endpoint_runtime
from src.prompt_security import untrusted_context_message
from src.settings import get_setting
from src.subagent_recovery_config import (
    RecoveryUnavailable, _base_fingerprint, _binding, _endpoint, _names,
    _session_parent, _workspace,
)
from src.tool_execution import _current_agent_privileges, vet_workspace_for_owner
from src.tool_policy import ToolPolicy, build_effective_tool_policy
from src.tool_security import blocked_tools_for_owner


class BackgroundFollowupContextError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Background continuation authority is unavailable.")


def _deny(code):
    raise BackgroundFollowupContextError(code)


def _safe_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BackgroundFollowupContextError:
            raise
        except RecoveryUnavailable as exc:
            raise BackgroundFollowupContextError(exc.code) from None
        except Exception:
            raise BackgroundFollowupContextError("context_unavailable") from None
    return wrapped


@_safe_errors
def capture_background_followup_context(*, owner, session_id, parent_run_id,
        endpoint_url, model, workspace, access_mode, disabled_tools, tool_policy,
        delegated_credential, allowed_tools=None):
    """Capture restrictions at dispatch, never credentials or transcript text."""
    if access_mode not in {"ask_every_time", "ask_important", "full_access"}:
        _deny("invalid_access_mode")
    if type(delegated_credential) is not bool:
        _deny("invalid_policy")
    if not isinstance(model, str) or not model:
        _deny("model_unavailable")
    policy = tool_policy if tool_policy is not None else ToolPolicy()
    if not isinstance(policy, ToolPolicy):
        _deny("invalid_policy")
    workspace = _workspace(workspace)
    fingerprint = _base_fingerprint(endpoint_url)
    with SessionLocal() as db:
        _session_parent(db, owner, session_id, parent_run_id)
        endpoint = _endpoint(db, owner, None, fingerprint)
        endpoint_id = endpoint.id
    return {
        "version": 1, "owner": owner or "", "session_id": session_id,
        "parent_run_id": parent_run_id, "endpoint_id": endpoint_id,
        "base_fingerprint": fingerprint, "model": model,
        "workspace": workspace, "binding": _binding(owner, workspace),
        "access_mode": access_mode, "delegated_credential": delegated_credential,
        "disabled_tools": _names(disabled_tools or []),
        # This is the foreground relevance selection, not a permission grant.
        "relevant_tools": None if allowed_tools is None else _names(allowed_tools),
        "tool_policy": {
            "disabled_tools": _names(policy.disabled_tools),
            "hidden_tools": _names(policy.hidden_tools), "mode": policy.mode,
            "block_all_tool_calls": policy.block_all_tool_calls,
            "disable_mcp": policy.disable_mcp,
        },
    }


@_safe_errors
def prepare_background_followup(sess, rec):
    """Return fresh stream arguments, including messages; raise before effects."""
    snapshot = rec.get("followup_context")
    owner = getattr(sess, "owner", None)
    if (not isinstance(snapshot, dict) or snapshot.get("version") != 1
            or snapshot.get("owner") != (owner or "")
            or snapshot.get("session_id") != sess.id or rec.get("session_id") != sess.id):
        _deny("provenance_unavailable")
    if snapshot.get("delegated_credential") is not False:
        # A bearer token's identity/revocation state is intentionally not saved.
        _deny("delegated_continuation_unavailable")
    privileges = _current_agent_privileges(owner)
    if not isinstance(privileges, dict) or privileges.get("can_use_agent") is not True:
        _deny("agent_revoked")
    with SessionLocal() as db:
        session, parent = _session_parent(db, owner, sess.id, snapshot.get("parent_run_id"))
        latest = db.query(ChatRunState).filter_by(
            session_id=sess.id, owner=_storage_owner(owner)).order_by(
                ChatRunState.started_at.desc(), ChatRunState.run_id.desc()).first()
        if latest is None or latest.run_id != parent.run_id:
            _deny("parent_superseded")
        reason = (parent.continuation or {}).get("terminal_reason")
        if reason and reason != "process_restarted":
            _deny("parent_stopped")
        if parent.status != "done" and not (parent.status == "interrupted" and reason == "process_restarted"):
            _deny("parent_not_ready")
        model = snapshot.get("model")
        if (session.model != model or sess.model != model
                or _base_fingerprint(session.endpoint_url) != snapshot.get("base_fingerprint")):
            _deny("model_changed")
        endpoint = _endpoint(db, owner, snapshot.get("endpoint_id"), snapshot.get("base_fingerprint"))
        allowed = privileges.get("allowed_models", [])
        hidden = json.loads(endpoint.hidden_models or "[]")
        if (not isinstance(hidden, list) or privileges.get("block_all_models") is True
                or model in hidden or ((privileges.get("allowed_models_restricted") or allowed)
                                      and (not isinstance(allowed, list) or model not in allowed))):
            _deny("model_revoked")
        workspace = _workspace(snapshot.get("workspace"))
        if _binding(owner, workspace) != snapshot.get("binding"):
            _deny("execution_binding_changed")
        if workspace and vet_workspace_for_owner(workspace, owner) != workspace:
            _deny("workspace_changed")
        modes = ["ask_every_time", "ask_important", "full_access"]
        current_access = get_access_mode_for_user(owner)
        original_access = snapshot.get("access_mode")
        if current_access not in modes or original_access not in modes:
            _deny("invalid_access_mode")
        access = modes[min(modes.index(current_access), modes.index(original_access))]
        saved = snapshot.get("tool_policy")
        if (not isinstance(saved, dict) or saved.get("mode") not in {"normal", "guide_only"}
                or type(saved.get("block_all_tool_calls")) is not bool
                or type(saved.get("disable_mcp")) is not bool):
            _deny("invalid_policy")
        disabled = set(_names(snapshot.get("disabled_tools")))
        disabled.update(_names(saved.get("disabled_tools")))
        disabled.update(_names(saved.get("hidden_tools")))
        disabled.update(_names(get_setting("disabled_tools", [])))
        owner_disabled = blocked_tools_for_owner(owner)
        disabled.update(owner_disabled)
        for privilege, tools in (
            ("can_use_bash", host_execution.TOOLS),
            ("can_use_documents", {"create_document", "edit_document", "update_document", "suggest_document"}),
            ("can_generate_images", {"generate_image"}),
            ("can_manage_memory", {"manage_memory", "manage_skills"}),
            ("can_use_research", {"trigger_research", "manage_research"}),
        ):
            if privileges.get(privilege, True) is not True:
                disabled.update(tools)
        messages = list(sess.get_context_messages())
        latest_user = next((m.get("content", "") for m in reversed(messages)
                            if m.get("role") == "user"), "")
        current_policy = build_effective_tool_policy(
            disabled_tools=disabled,
            last_user_message=latest_user if isinstance(latest_user, str) else "",
        )
        policy = ToolPolicy(
            disabled_tools=frozenset(current_policy.all_disabled_names()),
            hidden_tools=frozenset(_names(saved.get("hidden_tools"))),
            mode="guide_only" if "guide_only" in {saved["mode"], current_policy.mode} else "normal",
            block_all_tool_calls=saved["block_all_tool_calls"] or current_policy.block_all_tool_calls,
            disable_mcp=(saved["disable_mcp"] or current_policy.disable_mcp
                         or bool(owner_disabled) or privileges.get("can_use_browser", True) is not True),
        )
        # Resolve credentials last, from current owner-scoped endpoint state.
        base, key = resolve_endpoint_runtime(endpoint, owner=owner)
        if _base_fingerprint(base) != snapshot["base_fingerprint"]:
            _deny("credential_route_changed")
        url, headers = build_chat_url(base), build_headers(key, base)
    if snapshot["binding"]["kind"] == "host":
        messages.append({"role": "system", "content": (
            "Shell and file tools execute on the configured host over SSH. "
            "The original selected workspace remains bound. Never substitute the application container."
        )})
    messages.append(untrusted_context_message("background job output", (
        f"[Background job {rec['id']} finished]\n\n{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Do not repeat completed work. "
        "If the task is complete, give the user the final result."
    )))
    return {"endpoint_url": url, "model": model, "headers": headers,
            "messages": messages, "workspace": workspace, "owner": owner,
            "history_session": sess, "access_mode": access,
            "disabled_tools": policy.all_disabled_names(), "tool_policy": policy,
            "external_untrusted_context_seen": True, "delegated_credential": False,
            "relevant_tools": (None if snapshot.get("relevant_tools") is None
                               else set(_names(snapshot["relevant_tools"]))),
            "context_length": getattr(sess, "context_length", 0) or 0}
