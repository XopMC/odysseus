"""Non-secret child recovery description and fresh, fail-closed preparation.

These synchronous helpers do not claim leases or resume tasks. Call them off
the event loop; callers must recheck their child lease after preparation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from functools import wraps
from urllib.parse import urlsplit, urlunsplit

from core.database import ChatGoal, ChatRunState, ChatSubagentDelivery, ChatSubagentRun, ModelEndpoint, ProviderAuthSession, Session
from routes.prefs_routes import get_access_mode_for_user
from src import host_execution
from src.access_policy import ACCESS_MODES
from src.chat_work_store import _storage_owner
from src.chatgpt_subscription import ChatGPTSubscriptionAuthNotFound, ChatGPTSubscriptionReauthRequired
from src.constants import AGENT_WORKSPACE_DIR, DATA_DIR
from src.endpoint_resolver import build_chat_url, build_headers, normalize_base, resolve_endpoint_runtime
from src.settings import get_setting
from src.subagent_limits import MAX_ACTIVE_ON_PARENT_MODEL, MAX_ACTIVE_PER_MODEL
from src.tool_execution import _current_agent_privileges, vet_workspace_for_owner
from src.tool_policy import ToolPolicy
from src.tool_security import blocked_tools_for_owner


class RecoveryUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _deny(code, *, retryable=False):
    # Neither provider exception text nor stored task content enters errors.
    raise RecoveryUnavailable(code, "Child recovery requirements are not satisfied.", retryable=retryable) from None


def _safe_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RecoveryUnavailable:
            raise
        except Exception:
            raise RecoveryUnavailable("recovery_unavailable", "Child recovery preparation is unavailable.",
                                      retryable=True) from None
    return wrapped


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _base_fingerprint(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 for c in url):
        _deny("invalid_endpoint")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in url or "#" in url):
            _deny("invalid_endpoint")
        parsed.port  # Validate before normalization; never echo a malformed URL.
        normalized = normalize_base(urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                                                parsed.path, "", "")))
    except ValueError:
        _deny("invalid_endpoint")
    return _digest(normalized)


def _names(values):
    if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > 2048:
        _deny("invalid_policy")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value)
           for value in values):
        _deny("invalid_policy")
    return sorted(set(values))


def _owner(owner):
    if owner is not None and (not isinstance(owner, str) or len(owner) > 200):
        _deny("invalid_owner")
    return owner or None


def _session_parent(db, owner, session_id, parent_run_id):
    if not isinstance(parent_run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", parent_run_id):
        _deny("parent_unavailable")
    session = db.query(Session).filter_by(id=session_id, owner=owner).first()
    parent = db.query(ChatRunState).filter_by(
        run_id=parent_run_id, session_id=session_id, owner=_storage_owner(owner)).first()
    if session is None or parent is None:
        _deny("parent_unavailable")
    return session, parent


def _endpoint(db, owner, endpoint_id, fingerprint):
    query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled.is_(True))
    query = query.filter((ModelEndpoint.owner == owner) | ModelEndpoint.owner.is_(None))
    if endpoint_id is not None:
        if not isinstance(endpoint_id, str) or not endpoint_id or len(endpoint_id) > 200:
            _deny("endpoint_unavailable")
        rows = query.filter(ModelEndpoint.id == endpoint_id).all()
    else:
        rows = []
        for candidate in query.all():
            try:
                if _base_fingerprint(candidate.base_url) == fingerprint:
                    rows.append(candidate)
            except RecoveryUnavailable:
                continue
    if len(rows) != 1 or _base_fingerprint(rows[0].base_url) != fingerprint:
        _deny("endpoint_unavailable")
    endpoint = rows[0]
    if endpoint.provider_auth_id:
        auth = db.query(ProviderAuthSession).filter_by(id=endpoint.provider_auth_id, owner=owner).first()
        if auth is None:
            _deny("credential_owner_changed")
    return endpoint


def _workspace(value):
    if value in (None, ""):
        return None
    if (not isinstance(value, str) or len(value) > 4096 or not os.path.isabs(value)
            or any(c in value for c in ("\0", "\n", "\r"))):
        _deny("invalid_workspace")
    return value


def _binding(owner, workspace):
    if host_execution.enabled_for(owner):
        values = [os.environ.get("ODYSSEUS_HOST_TARGET", ""),
                  os.environ.get("ODYSSEUS_HOST_HELPER", "/home/xopmc/services/odysseus-host/host_exec.py"),
                  os.environ.get("ODYSSEUS_HOST_CWD", "/home/xopmc")]
        if not values[0] or any(not value or "\0" in value for value in values):
            _deny("host_unavailable")
        return {"kind": "host", "fingerprint": _digest(values)}
    # Bind to persistent mounts, not the recreated container's hostname or
    # interpreter. A replacement data/workspace mount is a different authority.
    values = []
    for path in (DATA_DIR, AGENT_WORKSPACE_DIR, workspace or AGENT_WORKSPACE_DIR):
        resolved = os.path.realpath(path)
        try:
            identity = os.stat(resolved)
        except OSError:
            _deny("workspace_unavailable", retryable=True)
        if not stat.S_ISDIR(identity.st_mode):
            _deny("workspace_unavailable")
        values.append([resolved, identity.st_dev, identity.st_ino, identity.st_uid])
    return {"kind": "local", "fingerprint": _digest(values)}


def _selection_mode():
    mode = get_setting("agent_subagents_mode", "off")
    if mode not in {"same_model", "selected_models"}:
        _deny("subagents_disabled")
    return mode


def _goal_identity(db, owner, session_id, expected=None):
    goal = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
    if goal is None:
        _deny("goal_unavailable")
    identity = {"id": goal.id, "objective_hash": _digest(goal.objective)}
    if expected is not None and identity != expected:
        _deny("goal_changed")
    if goal.status != "active":
        _deny("goal_not_active", retryable=goal.status in {"paused", "waiting_user", "review_required"})
    return identity


@_safe_errors
def capture_config(*, owner, session_id, parent_run_id, endpoint_url, endpoint_id, model,
                   workspace, access_mode, disabled_tools, tool_policy, delegated_credential,
                   external_untrusted_context_seen, max_active_for_model, db_factory) -> dict:
    owner = _owner(owner)
    if not isinstance(model, str) or not 1 <= len(model) <= 512:
        _deny("invalid_model")
    if access_mode not in ACCESS_MODES:
        _deny("invalid_access_mode")
    if type(max_active_for_model) is not int or not 1 <= max_active_for_model <= MAX_ACTIVE_PER_MODEL:
        _deny("invalid_capacity")
    if type(delegated_credential) is not bool or type(external_untrusted_context_seen) is not bool:
        _deny("invalid_policy")
    policy = tool_policy if tool_policy is not None else ToolPolicy()
    if (not isinstance(policy, ToolPolicy) or policy.mode not in {"normal", "guide_only"}
            or type(policy.block_all_tool_calls) is not bool or type(policy.disable_mcp) is not bool):
        _deny("invalid_policy")
    workspace = _workspace(workspace)
    fingerprint = _base_fingerprint(endpoint_url)
    with db_factory() as db:
        _, parent = _session_parent(db, owner, session_id, parent_run_id)
        endpoint = _endpoint(db, owner, endpoint_id, fingerprint)
        goal = _goal_identity(db, owner, session_id) if (parent.continuation or {}).get("goal") is True else None
        resolved_id = endpoint.id
    return {
        "version": 1, "owner": owner or "", "session_id": session_id,
        "parent_run_id": parent_run_id, "goal": goal,
        "endpoint_id": resolved_id, "base_fingerprint": fingerprint, "model": model,
        "route_id": endpoint_id or ("url-" + hashlib.sha256(endpoint_url.encode()).hexdigest()[:16]),
        "selection_mode": _selection_mode(), "binding": _binding(owner, workspace),
        "workspace": workspace, "access_mode": access_mode,
        "disabled_tools": _names(disabled_tools or []),
        "tool_policy": {"disabled_tools": _names(policy.disabled_tools),
                        "hidden_tools": _names(policy.hidden_tools), "mode": policy.mode,
                        "block_all_tool_calls": policy.block_all_tool_calls, "disable_mcp": policy.disable_mcp},
        "delegated_credential": delegated_credential,
        "external_untrusted_context_seen": external_untrusted_context_seen,
        "max_active_for_model": max_active_for_model,
    }


def _current_capacity(snapshot, endpoint, session, privileges):
    mode = _selection_mode()
    if mode != snapshot["selection_mode"]:
        _deny("model_policy_changed")
    model, spec = snapshot["model"], snapshot["model"] + "@" + endpoint.id
    allowed = privileges.get("allowed_models", [])
    if (privileges.get("block_all_models") is True
            or ((privileges.get("allowed_models_restricted") or allowed)
                and (not isinstance(allowed, list) or model not in allowed))):
        _deny("model_revoked")
    try:
        hidden = json.loads(endpoint.hidden_models or "[]")
        cached = json.loads(endpoint.cached_models or "[]") + json.loads(endpoint.pinned_models or "[]")
    except (ValueError, TypeError):
        _deny("model_policy_invalid")
    if not isinstance(hidden, list) or not isinstance(cached, list) or model in hidden or (cached and model not in cached):
        _deny("model_revoked")
    if mode == "same_model":
        if session.model != model or _base_fingerprint(session.endpoint_url) != snapshot["base_fingerprint"]:
            _deny("model_policy_changed")
        cap = MAX_ACTIVE_ON_PARENT_MODEL
    else:
        allowed = get_setting("agent_subagent_models", [])
        if isinstance(allowed, str):
            allowed = [item.strip() for item in allowed.split(",") if item.strip()]
        if not isinstance(allowed, list) or spec not in allowed:
            _deny("model_revoked")
        limits = get_setting("agent_subagent_model_limits", {})
        if not isinstance(limits, dict):
            _deny("model_policy_invalid")
        cap = limits.get(spec, MAX_ACTIVE_PER_MODEL)
        if type(cap) is not int or not 1 <= cap <= MAX_ACTIVE_PER_MODEL:
            _deny("model_policy_invalid")
        if session.model == model and _base_fingerprint(session.endpoint_url) == snapshot["base_fingerprint"]:
            cap = min(cap, MAX_ACTIVE_ON_PARENT_MODEL)
    return min(cap, snapshot["max_active_for_model"])


def _database_seal(db, owner, session_id, child_id, parent_run_id, endpoint_id):
    """Read authorization identities only; no credential refresh or host I/O."""
    # Column queries bypass the ORM identity cache without expiring/discarding
    # the caller's pending CAS changes or flushing them during validation.
    def columns(model, fields):
        return db.query(*(getattr(model, field) for field in fields))

    session = columns(Session, ("owner", "model", "endpoint_url", "project_id")).filter(
        Session.id == session_id, Session.owner == owner).first()
    parent = columns(ChatRunState, ("run_id", "status", "context_revision", "continuation")).filter(
        ChatRunState.run_id == parent_run_id, ChatRunState.session_id == session_id,
        ChatRunState.owner == _storage_owner(owner)).first()
    child = columns(ChatSubagentRun, (
        "id", "owner", "parent_session_id", "parent_run_id", "model", "endpoint_id",
        "revision", "worker_id", "status", "cancel_requested", "removed", "policy_snapshot",
    )).filter(ChatSubagentRun.id == child_id, ChatSubagentRun.owner == (owner or ""),
              ChatSubagentRun.parent_session_id == session_id).first()
    endpoint = columns(ModelEndpoint, (
        "id", "owner", "is_enabled", "base_url", "provider_auth_id", "model_type",
        "hidden_models", "cached_models", "pinned_models", "updated_at",
    )).filter(ModelEndpoint.id == endpoint_id).first()
    if session is None or parent is None or child is None or endpoint is None:
        _deny("authority_unavailable")
    goal = columns(ChatGoal, ("id", "status", "revision", "objective")).filter(
        ChatGoal.session_id == session_id, ChatGoal.owner == _storage_owner(owner)).first()
    newest = columns(ChatRunState, ("run_id", "status", "continuation")).filter(
        ChatRunState.session_id == session_id, ChatRunState.owner == _storage_owner(owner)).order_by(
        ChatRunState.started_at.desc(), ChatRunState.run_id.desc()).first()
    deliveries = db.query(
        ChatSubagentDelivery.child_id, ChatSubagentDelivery.parent_run_id, ChatSubagentDelivery.status,
        ChatSubagentDelivery.delivered_run_id, ChatSubagentRun.owner,
        ChatSubagentRun.parent_session_id, ChatSubagentRun.parent_run_id,
    ).join(
        ChatSubagentRun, ChatSubagentRun.id == ChatSubagentDelivery.child_id,
    ).filter(
        ChatSubagentDelivery.parent_session_id == session_id,
        ChatSubagentDelivery.owner == (owner or ""),
        ChatSubagentDelivery.delivered_run_id == (newest.run_id if newest else None),
    ).order_by(ChatSubagentDelivery.child_id).all()
    auth = columns(ProviderAuthSession, ("id", "owner", "provider", "base_url")).filter(
        ProviderAuthSession.id == endpoint.provider_auth_id).first() if endpoint.provider_auth_id else None
    projection = {
        "session": [session.owner, session.model, _base_fingerprint(session.endpoint_url), session.project_id],
        "child": [child.id, child.owner, child.parent_session_id, child.parent_run_id,
                  child.model, child.endpoint_id, child.revision, child.worker_id, child.status,
                  child.cancel_requested, child.removed, _digest(child.policy_snapshot or {})],
        "parent": [parent.run_id, parent.status, parent.context_revision, _digest(parent.continuation or {})],
        "newest": [newest.run_id, newest.status, _digest(newest.continuation or {})] if newest else None,
        "goal": [goal.id, goal.status, goal.revision, _digest(goal.objective)] if goal else None,
        "endpoint": [endpoint.id, endpoint.owner, endpoint.is_enabled, _base_fingerprint(endpoint.base_url),
                     endpoint.provider_auth_id, endpoint.model_type, endpoint.hidden_models,
                     endpoint.cached_models, endpoint.pinned_models,
                     endpoint.updated_at.isoformat() if endpoint.updated_at else None],
        "auth": [auth.id, auth.owner, auth.provider, _base_fingerprint(auth.base_url)] if auth else None,
        "deliveries": [list(row) for row in deliveries],
    }
    return {"version": 1, "owner": owner or "", "session_id": session_id, "child_id": child_id,
            "parent_run_id": parent_run_id, "endpoint_id": endpoint_id, "fingerprint": _digest(projection)}


def validate_recovery_seal(db, *, owner, session_id, child_id, seal) -> bool:
    """Recheck preparation's DB authority under the caller's claim transaction.

    Preferences/settings and execution-host configuration are not DB-backed;
    they are checked during prepare and again by the normal tool dispatcher.
    """
    try:
        owner = _owner(owner)
        if (not isinstance(seal, dict) or seal.get("version") != 1
                or seal.get("owner") != (owner or "") or seal.get("session_id") != session_id
                or seal.get("child_id") != child_id):
            return False
        with db.no_autoflush:
            return _database_seal(db, owner, session_id, child_id,
                                  seal.get("parent_run_id"), seal.get("endpoint_id")) == seal
    except (RecoveryUnavailable, ValueError, TypeError):
        return False


@_safe_errors
def prepare_config(*, owner, session_id, child_id, snapshot, db_factory) -> dict:
    owner = _owner(owner)
    if (not isinstance(snapshot, dict) or snapshot.get("version") != 1
            or snapshot.get("owner") != (owner or "") or snapshot.get("session_id") != session_id):
        _deny("invalid_snapshot")
    if snapshot.get("delegated_credential") is not False:
        _deny("delegated_recovery_unavailable")
    if (type(snapshot.get("max_active_for_model")) is not int
            or not 1 <= snapshot["max_active_for_model"] <= MAX_ACTIVE_PER_MODEL
            or type(snapshot.get("external_untrusted_context_seen")) is not bool):
        _deny("invalid_snapshot")
    privileges = _current_agent_privileges(owner)
    if not isinstance(privileges, dict) or privileges.get("can_use_agent") is not True:
        _deny("agent_revoked")
    with db_factory() as db:
        # Seal before evaluating authority, so a concurrent change cannot be
        # blessed by taking a newer seal after checks used older ORM rows.
        seal = _database_seal(db, owner, session_id, child_id,
                              snapshot.get("parent_run_id"), snapshot.get("endpoint_id"))
        session, parent = _session_parent(db, owner, session_id, snapshot.get("parent_run_id"))
        child = db.query(ChatSubagentRun).filter_by(id=child_id, owner=owner or "", parent_session_id=session_id).first()
        if (child is None or child.parent_run_id != parent.run_id or child.model != snapshot.get("model")
                or child.endpoint_id != snapshot.get("route_id")
                or child.cancel_requested or child.removed or child.status in {"completed", "cancelled", "failed"}
                or (child.policy_snapshot or {}).get("recovery_config") != snapshot):
            _deny("child_changed")
        continuation = parent.continuation or {}
        reason = continuation.get("terminal_reason")
        if snapshot.get("goal"):
            if continuation.get("goal") is not True:
                _deny("goal_changed")
            _goal_identity(db, owner, session_id, snapshot["goal"])
            if reason and reason != "process_restarted":
                _deny("parent_stopped")
        else:
            if reason and reason != "process_restarted":
                _deny("parent_stopped")
            if continuation.get("goal") is True:
                _deny("parent_changed")
            if parent.status != "done" and not (parent.status == "interrupted" and reason == "process_restarted"):
                _deny("parent_not_ready", retryable=parent.status == "running")
            newest = db.query(ChatRunState).filter_by(session_id=session_id, owner=_storage_owner(owner)).order_by(
                ChatRunState.started_at.desc(), ChatRunState.run_id.desc()).first()
            if newest is None:
                _deny("parent_superseded")
            if newest.run_id != parent.run_id:
                # A sibling result can wake a new parent turn. Permit only a
                # recorded delivery from this exact original parent lineage.
                delivery = db.query(ChatSubagentDelivery).join(
                    ChatSubagentRun, ChatSubagentRun.id == ChatSubagentDelivery.child_id,
                ).filter(
                    ChatSubagentDelivery.owner == (owner or ""),
                    ChatSubagentDelivery.parent_session_id == session_id,
                    ChatSubagentDelivery.parent_run_id == parent.run_id,
                    ChatSubagentDelivery.delivered_run_id == newest.run_id,
                    ChatSubagentDelivery.status == "delivered",
                    ChatSubagentRun.owner == (owner or ""),
                    ChatSubagentRun.parent_session_id == session_id,
                    ChatSubagentRun.parent_run_id == parent.run_id,
                ).first()
                if delivery is None:
                    _deny("parent_superseded")
                new_reason = (newest.continuation or {}).get("terminal_reason")
                if new_reason and new_reason != "process_restarted":
                    _deny("parent_stopped")
                if newest.status != "done" and not (newest.status == "interrupted" and new_reason == "process_restarted"):
                    _deny("parent_not_ready", retryable=newest.status == "running")
            goal = db.query(ChatGoal).filter_by(session_id=session_id, owner=_storage_owner(owner)).first()
            if goal is not None and goal.status in {"active", "paused", "waiting_user", "review_required"}:
                _deny("parent_goal_changed", retryable=goal.status != "active")
        endpoint = _endpoint(db, owner, snapshot.get("endpoint_id"), snapshot.get("base_fingerprint"))
        capacity = _current_capacity(snapshot, endpoint, session, privileges)
        workspace = _workspace(snapshot.get("workspace"))
        if _binding(owner, workspace) != snapshot.get("binding"):
            _deny("execution_binding_changed")
        if workspace is not None:
            try:
                vetted = vet_workspace_for_owner(workspace, owner)
            except (ValueError, OSError):
                _deny("workspace_unavailable", retryable=True)
            if vetted != workspace:
                _deny("workspace_changed")
        modes = ["ask_every_time", "ask_important", "full_access"]
        current_access = get_access_mode_for_user(owner)
        original_access = snapshot.get("access_mode")
        if current_access not in modes or original_access not in modes:
            _deny("invalid_access_mode")
        access = modes[min(modes.index(current_access), modes.index(original_access))]
        saved_policy = snapshot.get("tool_policy")
        if (not isinstance(saved_policy, dict) or saved_policy.get("mode") not in {"normal", "guide_only"}
                or type(saved_policy.get("block_all_tool_calls")) is not bool
                or type(saved_policy.get("disable_mcp")) is not bool):
            _deny("invalid_policy")
        global_disabled = get_setting("disabled_tools", [])
        disabled = set(_names(snapshot.get("disabled_tools"))) | set(_names(global_disabled))
        disabled.update(_names(saved_policy.get("disabled_tools")))
        hidden = frozenset(_names(saved_policy.get("hidden_tools")))
        disabled.update(hidden)
        owner_disabled = blocked_tools_for_owner(owner)
        disabled.update(owner_disabled)
        for privilege, tools in (
            ("can_use_bash", host_execution.TOOLS),
            ("can_use_documents", {"create_document", "edit_document", "update_document", "suggest_document"}),
            ("can_generate_images", {"generate_image"}),
            ("can_manage_memory", {"manage_memory", "manage_skills"}),
        ):
            if privileges.get(privilege, True) is not True:
                disabled.update(tools)
        disable_mcp = saved_policy["disable_mcp"] or bool(owner_disabled) or privileges.get("can_use_browser", True) is not True
        if privileges.get("can_use_browser", True) is not True:
            disabled.add("builtin_browser")
        policy = ToolPolicy(disabled_tools=frozenset(disabled), hidden_tools=hidden,
                            mode=saved_policy["mode"], block_all_tool_calls=saved_policy["block_all_tool_calls"],
                            disable_mcp=disable_mcp)
        try:
            base, key = resolve_endpoint_runtime(endpoint, owner=owner)
        except (ChatGPTSubscriptionAuthNotFound, ChatGPTSubscriptionReauthRequired):
            _deny("credentials_revoked")
        if _base_fingerprint(base) != snapshot["base_fingerprint"]:
            _deny("credential_route_changed")
        url, headers = build_chat_url(base), build_headers(key, base)
    return {"endpoint_url": url, "model": snapshot["model"], "headers": headers,
            "workspace": workspace, "access_mode": access, "disabled_tools": disabled,
            "allowed_tools": None, "tool_policy": policy, "timeout_seconds": 600,
            "external_untrusted_context_seen": snapshot.get("external_untrusted_context_seen") is True,
            "delegated_credential": False, "max_active_for_model": capacity, "recovery_seal": seal}
