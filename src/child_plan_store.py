"""Private durable plans for child agents; never accesses a parent Plan or Goal."""
from __future__ import annotations

import copy
import json

from core.database import ChatSubagentEvent, ChatSubagentRun, SessionLocal, utcnow_naive
from src.chat_work_store import (
    PLAN_STATES, WorkConflict, WorkNotFound, _clean_string_list, _clean_text,
    _stable_step_id, checklist_steps,
)


def child_id_for_context(ctx):
    """Recognize exact child identity before owner checks, preventing fallback."""
    from src.subagent_runtime import ChildLeaseLost, _execution_lease
    lease = _execution_lease.get()
    if lease:
        if (not isinstance(ctx, dict)
                or ctx.get("parent_run_id") != lease[0]
                or (ctx.get("owner") or "") != lease[1]):
            raise ChildLeaseLost("Child plan execution context does not match its scope")
        return lease[0]
    if not isinstance(ctx, dict) or not ctx.get("parent_run_id"):
        return None
    child_id = ctx["parent_run_id"]
    with SessionLocal() as db:
        return child_id if db.query(ChatSubagentRun.id).filter_by(id=child_id).first() else None


def _row(db, owner, session_id, child_id, *, writing=False):
    from src.subagent_runtime import ChildLeaseLost, _execution_lease, _locked_child
    lease = _execution_lease.get()
    if lease and lease[:2] != (child_id, owner or ""):
        raise ChildLeaseLost("Child plan execution lease does not match its scope")
    if writing and not lease:
        raise ChildLeaseLost("Child plan updates require a current execution lease")
    row = _locked_child(db, child_id, owner)
    if row is None or row.parent_session_id != session_id or row.removed:
        raise WorkNotFound("Child plan scope not found")
    if writing and (row.cancel_requested or row.status not in {"queued", "running", "recovering", "waiting_user"}):
        raise WorkConflict("Child is no longer accepting plan updates")
    return row


def get(owner, session_id, child_id):
    with SessionLocal() as db:
        row = _row(db, owner, session_id, child_id)
        return copy.deepcopy((row.metrics or {}).get("_child_plan"))


def _check_revision(plan, expected_revision):
    if expected_revision is not None:
        if type(expected_revision) is not int or expected_revision != (plan or {}).get("revision", 0):
            raise WorkConflict("Child plan changed; reload")


def _persist(db, row, plan, kind):
    plan["revision"] = int(plan.get("revision", 0)) + 1
    plan["updated_at"] = utcnow_naive().isoformat()
    plan.setdefault("created_at", plan["updated_at"])
    unfinished = [step for step in plan["steps"] if step.get("required", True) and step["status"] != "done"]
    plan["status"] = "executing" if unfinished else "done"
    plan["current_step_id"] = next((step["id"] for step in plan["steps"]
                                    if step["status"] == "in_progress"), None)
    if not plan["current_step_id"] and unfinished:
        plan["current_step_id"] = unfinished[0]["id"]
    if len(json.dumps(plan, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > 256 * 1024:
        raise ValueError("Child plan exceeds the 256 KiB storage limit")
    row.metrics = {**dict(row.metrics or {}), "_child_plan": plan}
    row.revision = int(row.revision or 0) + 1
    db.add(ChatSubagentEvent(child_id=row.id, owner=row.owner,
                            parent_session_id=row.parent_session_id, kind=kind,
                            payload={"child_id": row.id, "plan_update": copy.deepcopy(plan)}))
    db.commit()
    return copy.deepcopy(plan)


def save(owner, session_id, child_id, title, steps, *, expected_revision=None, replace_terminal=False):
    title = _clean_text(title or "Plan", "plan title", 1000)
    if isinstance(steps, str):
        steps = checklist_steps(steps)
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("Plan needs 1-100 steps")
    normalized = []
    for index, item in enumerate(steps, 1):
        if not isinstance(item, dict) or (item.get("status") or "pending") not in PLAN_STATES:
            raise ValueError("Invalid plan step")
        text = _clean_text(item.get("text"), "plan step", 1000)
        normalized.append({"id": _clean_text(item.get("id") or _stable_step_id(text, index), "step ID", 120),
                           "text": text, "status": item.get("status") or "pending",
                           "required": item.get("required") is not False})
    with SessionLocal() as db:
        row = _row(db, owner, session_id, child_id, writing=True)
        current = copy.deepcopy((row.metrics or {}).get("_child_plan"))
        _check_revision(current, expected_revision)
        if current and current.get("status") in {"done", "cancelled"} and not replace_terminal:
            raise WorkConflict("Child plan is no longer mutable")
        old_by_text = {}
        for step in (current or {}).get("steps", []):
            old_by_text.setdefault(step["text"].strip().casefold(), []).append(step["id"])
        for step in normalized:
            matches = old_by_text.get(step["text"].strip().casefold(), [])
            if matches:
                step["id"] = matches.pop(0)
        if len({step["id"] for step in normalized}) != len(normalized):
            raise ValueError("Plan step IDs must be unique")
        plan = {**(current or {}), "id": "child-plan-" + child_id,
                "scope": "child", "child_id": child_id, "session_id": session_id,
                "title": title, "steps": normalized}
        return _persist(db, row, plan, "plan_saved")


def update_step(owner, session_id, child_id, step_id, status, *, summary="", progress=None, expected_revision=None):
    if status not in PLAN_STATES:
        raise ValueError("Invalid plan step status")
    if progress is not None:
        keys = {"files_changed", "verification", "decisions", "next_work"}
        if not isinstance(progress, dict) or set(progress) - keys:
            raise ValueError("Invalid plan progress snapshot")
        progress = {key: _clean_string_list(progress.get(key), key) for key in keys}
    with SessionLocal() as db:
        row = _row(db, owner, session_id, child_id, writing=True)
        plan = copy.deepcopy((row.metrics or {}).get("_child_plan"))
        if plan is None:
            raise WorkNotFound("Child plan not found")
        _check_revision(plan, expected_revision)
        if plan.get("status") != "executing":
            raise WorkConflict("Child plan is no longer mutable")
        step = next((step for step in plan["steps"] if step["id"] == step_id), None)
        if step is None:
            raise WorkNotFound("Plan step not found")
        step["status"] = status
        if summary:
            step["summary"] = _clean_text(summary, "summary", 2000)
        if progress is not None:
            step["progress"] = progress
        return _persist(db, row, plan, "plan_step_updated")


def execute_plan_action(owner, session_id, child_id, action, data, *, plan_recovery=False):
    if not isinstance(data, dict):
        raise ValueError("Plan action requires an object")
    if action == "update_plan_step":
        keys = ("files_changed", "verification", "decisions", "next_work")
        return update_step(owner, session_id, child_id, data.get("step_id"), data.get("status"),
                           summary=data.get("summary") or "",
                           progress={key: data.get(key) or [] for key in keys} if any(key in data for key in keys) else None,
                           expected_revision=data.get("expected_revision"))
    if action not in {"create_plan", "update_plan"}:
        raise ValueError("Invalid child plan action")
    current = get(owner, session_id, child_id) if action == "update_plan" else None
    return save(owner, session_id, child_id,
                data.get("title") or (current or {}).get("title") or "Plan",
                data.get("plan") if action == "update_plan" else data.get("steps"),
                expected_revision=data.get("expected_revision", (current or {}).get("revision")),
                replace_terminal=bool(plan_recovery))
