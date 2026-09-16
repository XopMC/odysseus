"""Durable owner-scoped Plan and Goal state for ordinary chats.

The tables live beside chat sessions so backups and rollback-compatible builds
keep the transcript and its work state together.  Every mutation uses a
revision check when initiated by the UI; model tools operate on the exact
session/owner context supplied by the server-side dispatcher.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import re
import uuid
from sqlalchemy import or_

from core.database import (
    ChatGoal, ChatPlan, ChatWorkEvent, Session as DbSession, SessionLocal,
    utcnow_naive,
)


PLAN_STATES = {"pending", "in_progress", "done", "blocked"}
PLAN_STATUS = {"draft", "approved", "executing", "done", "cancelled"}
GOAL_STATUS = {"active", "paused", "waiting_user", "completed", "cancelled"}


class WorkConflict(RuntimeError):
    pass


class WorkNotFound(LookupError):
    pass


def _clean_text(value, name, limit=8192):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} is too long")
    return value


def _session(db, owner, session_id):
    row = db.query(DbSession).filter(DbSession.id == session_id, DbSession.owner == owner).first()
    if row is None:
        raise WorkNotFound("Chat not found")
    return row


def _public_plan(row):
    if row is None:
        return None
    return {
        "id": row.id, "session_id": row.session_id, "title": row.title,
        "status": row.status, "steps": list(row.steps or []),
        "current_step_id": row.current_step_id, "revision": row.revision,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _public_goal(row):
    if row is None:
        return None
    return {
        "id": row.id, "session_id": row.session_id, "objective": row.objective,
        "status": row.status, "attempt": row.attempt, "progress": row.progress,
        "checkpoint": dict(row.checkpoint or {}), "last_error": row.last_error,
        "failure_count": row.failure_count, "revision": row.revision,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


def checklist_steps(markdown):
    """Convert legacy markdown checklists into stable structured steps."""
    lines = re.findall(r"^\s*-\s*\[([ xX])\]\s+(.+?)\s*$", str(markdown or ""), re.M)
    if not lines:
        text = _clean_text(markdown, "plan", 8192)
        lines = [(" ", line.strip(" -*\t")) for line in text.splitlines() if line.strip()]
    steps = []
    for index, (checked, text) in enumerate(lines[:100], 1):
        clean = _clean_text(text, "plan step", 1000)
        steps.append({
            "id": _stable_step_id(clean, index), "text": clean,
            "status": "done" if checked.lower() == "x" else "pending",
            "required": True,
        })
    if not steps:
        raise ValueError("Plan needs at least one step")
    first = next((step for step in steps if step["status"] != "done"), None)
    if first:
        first["status"] = "in_progress"
    return steps


def _stable_step_id(text, ordinal=1):
    """Return an opaque, deterministic ID for legacy markdown steps.

    IDs must survive a reparse/reorder of the same plan.  The ordinal only
    disambiguates duplicate step text; authored IDs are still preferred.
    """
    digest = hashlib.sha256(str(text).strip().casefold().encode("utf-8")).hexdigest()[:20]
    return f"step-{digest}-{int(ordinal)}"


class ChatWorkStore:
    def _event(self, db, owner, session_id, kind, entity_id, revision, payload):
        db.add(ChatWorkEvent(
            session_id=session_id, owner=owner, kind=kind,
            entity_id=entity_id, revision=revision, payload=payload or {},
        ))

    def get(self, owner, session_id):
        with SessionLocal() as db:
            _session(db, owner, session_id)
            plan = db.query(ChatPlan).filter_by(owner=owner, session_id=session_id).first()
            goal = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            cursor = db.query(ChatWorkEvent.id).filter_by(owner=owner, session_id=session_id).order_by(ChatWorkEvent.id.desc()).limit(1).scalar()
            return {"plan": _public_plan(plan), "goal": _public_goal(goal), "cursor": cursor or 0}

    def list_active_goals(self):
        """Return owner/session pairs that must be resumed by the server controller."""
        with SessionLocal() as db:
            rows = db.query(ChatGoal).filter(ChatGoal.status == "active").all()
            return [_public_goal(row) for row in rows]

    def events(self, owner, session_id, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Invalid event cursor")
        with SessionLocal() as db:
            _session(db, owner, session_id)
            rows = db.query(ChatWorkEvent).filter(
                ChatWorkEvent.owner == owner, ChatWorkEvent.session_id == session_id,
                ChatWorkEvent.id > after,
            ).order_by(ChatWorkEvent.id).limit(limit).all()
            return [{
                "seq": row.id, "type": row.kind, "entity_id": row.entity_id,
                "revision": row.revision, "data": dict(row.payload or {}),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            } for row in rows]

    def save_plan(self, owner, session_id, title, steps, *, expected_revision=None):
        title = _clean_text(title or "Plan", "plan title", 1000)
        if isinstance(steps, str):
            steps = checklist_steps(steps)
        if not isinstance(steps, list) or not steps or len(steps) > 100:
            raise ValueError("Plan needs 1-100 steps")
        normalized = []
        for index, item in enumerate(steps, 1):
            if not isinstance(item, dict):
                raise ValueError("Invalid plan step")
            status = item.get("status") or "pending"
            if status not in PLAN_STATES:
                raise ValueError("Invalid plan step status")
            normalized.append({
                "id": str(item.get("id") or _stable_step_id(item.get("text"), index))[:120],
                "text": _clean_text(item.get("text"), "plan step", 1000),
                "status": status, "required": item.get("required") is not False,
            })
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatPlan).filter_by(owner=owner, session_id=session_id).first()
            if row is None:
                if expected_revision not in (None, 0):
                    raise WorkConflict("Plan changed; reload")
                row = ChatPlan(id=uuid.uuid4().hex, owner=owner, session_id=session_id)
                db.add(row)
                db.flush()
            else:
                if row.status in {"cancelled", "done"}:
                    raise WorkConflict("Plan is no longer mutable")
                if expected_revision is not None and row.revision != expected_revision:
                    raise WorkConflict("Plan changed; reload")
                row.revision += 1
            # Legacy update_plan is an adapter.  Once execution has started it
            # must not silently turn the plan back into a draft.  Reconcile
            # omitted IDs by matching old text so old clients keep stable IDs.
            if row is not None and row.steps:
                old_by_text = {str(step.get("text", "")).strip().casefold(): step.get("id") for step in row.steps if step.get("id")}
                for step in normalized:
                    # Text is the only identity available to legacy clients;
                    # preserve a previously authored opaque ID whenever it
                    # matches, including old positional IDs such as step-1.
                    step["id"] = old_by_text.get(step["text"].strip().casefold(), step["id"])
            prior_status = row.status if row is not None else "draft"
            status = prior_status if prior_status in {"executing", "done"} else "draft"
            row.title, row.steps, row.status = title, normalized, status
            row.current_step_id = next((s["id"] for s in normalized if s["status"] in {"pending", "in_progress"}), None)
            self._event(db, owner, session_id, "plan_saved", row.id, row.revision, _public_plan(row))
            db.flush()
            return _public_plan(row)

    def plan_action(self, owner, session_id, action, expected_revision):
        if action not in {"execute", "cancel"}:
            raise ValueError("Invalid plan action")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatPlan).filter_by(owner=owner, session_id=session_id).first()
            if row is None:
                raise WorkNotFound("Plan not found")
            if row.revision != expected_revision:
                raise WorkConflict("Plan changed; reload")
            if action == "execute" and row.status not in {"draft", "approved"}:
                raise WorkConflict("Plan cannot be executed in its current state")
            row.status = "executing" if action == "execute" else "cancelled"
            row.revision += 1
            if action == "execute":
                steps = [dict(step) for step in (row.steps or [])]
                first = next((step for step in steps if step.get("status") != "done"), None)
                if first:
                    first["status"] = "in_progress"
                    row.current_step_id = first["id"]
                    row.steps = steps
            event_kind = "plan_executed" if action == "execute" else "plan_cancelled"
            self._event(db, owner, session_id, event_kind, row.id, row.revision, _public_plan(row))
            db.flush()
            return _public_plan(row)

    def revise_goal(self, owner, session_id, objective, expected_revision):
        objective = _clean_text(objective, "goal", 12000)
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled"}:
                raise WorkNotFound("Active goal not found")
            if row.status != "active":
                raise WorkConflict("Resume the Goal before completing it")
            if row.revision != expected_revision:
                raise WorkConflict("Goal changed; reload")
            row.objective = objective
            row.status = "active"
            row.attempt = int(row.attempt or 0) + 1
            row.progress = "Goal revised; continuing with the new objective."
            row.checkpoint = {**dict(row.checkpoint or {}), "revision_reason": "goal_revised"}
            row.lease_token = None
            row.lease_expires_at = None
            row.revision += 1
            self._event(db, owner, session_id, "goal_revised", row.id, row.revision, _public_goal(row))
            db.flush()
            return _public_goal(row)

    def update_plan_step(self, owner, session_id, step_id, status, *, summary="", expected_revision=None):
        if status not in PLAN_STATES:
            raise ValueError("Invalid plan step status")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatPlan).filter_by(owner=owner, session_id=session_id).first()
            if row is None:
                raise WorkNotFound("Plan not found")
            if row.status in {"cancelled", "done"}:
                raise WorkConflict("Plan is no longer mutable")
            if row.status != "executing":
                raise WorkConflict("Approve the plan before updating its steps")
            if expected_revision is not None and row.revision != expected_revision:
                raise WorkConflict("Plan changed; reload")
            steps = [dict(step) for step in (row.steps or [])]
            target = next((step for step in steps if step.get("id") == step_id), None)
            if target is None:
                raise WorkNotFound("Plan step not found")
            target["status"] = status
            if summary:
                target["summary"] = str(summary)[:2000]
            unfinished = [s for s in steps if s.get("required", True) and s.get("status") != "done"]
            row.steps = steps
            row.status = "done" if not unfinished else "executing"
            row.current_step_id = next((s["id"] for s in steps if s.get("status") == "in_progress"), None) or (unfinished[0]["id"] if unfinished else None)
            row.revision += 1
            self._event(db, owner, session_id, "plan_step_updated", row.id, row.revision, {"step_id": step_id, "status": status, "summary": summary})
            db.flush()
            return _public_plan(row)

    def ensure_goal(self, owner, session_id, objective):
        objective = _clean_text(objective, "goal", 12000)
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled"}:
                if row is not None:
                    db.delete(row)
                    db.flush()
                row = ChatGoal(id=uuid.uuid4().hex, owner=owner, session_id=session_id, objective=objective)
                db.add(row)
                db.flush()
                self._event(db, owner, session_id, "goal_created", row.id, row.revision, _public_goal(row))
            return _public_goal(row)

    def goal_action(self, owner, session_id, action, expected_revision):
        statuses = {"pause": "paused", "resume": "active", "cancel": "cancelled"}
        if action not in statuses:
            raise ValueError("Invalid goal action")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None:
                raise WorkNotFound("Goal not found")
            if row.revision != expected_revision:
                raise WorkConflict("Goal changed; reload")
            if row.status in {"completed", "cancelled"}:
                raise WorkConflict("Goal is already terminal")
            row.status = statuses[action]
            row.revision += 1
            event_kind = {"pause": "goal_paused", "resume": "goal_resumed", "cancel": "goal_cancelled"}[action]
            self._event(db, owner, session_id, event_kind, row.id, row.revision, _public_goal(row))
            db.flush()
            return _public_goal(row)

    def update_goal(self, owner, session_id, progress, checkpoint=None, *, waiting_user=False):
        progress = _clean_text(progress, "goal progress", 12000)
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError("Goal checkpoint must be an object")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled", "paused", "waiting_user"}:
                raise WorkNotFound("Active goal not found")
            row.progress = progress
            if checkpoint is not None:
                # Progress/tool checkpoints are partial updates. Never erase
                # the durable model ledger, prior tool results, or approval
                # provenance when a later event only carries one field.
                row.checkpoint = {**dict(row.checkpoint or {}), **checkpoint}
            row.status = "waiting_user" if waiting_user else "active"
            if not waiting_user:
                row.failure_count = 0
                row.last_error = None
            row.revision += 1
            self._event(db, owner, session_id, "goal_progress", row.id, row.revision, {"progress": progress, "checkpoint": checkpoint or {}, "status": row.status})
            db.flush()
            return _public_goal(row)

    def record_goal_failure(self, owner, session_id, error, checkpoint=None):
        """Persist bounded transport/model retry state for the server controller."""
        error = _clean_text(error, "goal error", 2000)
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError("Goal checkpoint must be an object")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled", "paused", "waiting_user"}:
                raise WorkNotFound("Active goal not found")
            row.failure_count = int(row.failure_count or 0) + 1 if row.last_error == error else 1
            row.last_error = error
            row.lease_token = None
            row.lease_expires_at = None
            row.progress = "Model attempt failed; the server will retry automatically."
            if checkpoint is not None:
                row.checkpoint = {**dict(row.checkpoint or {}), **checkpoint}
            if row.failure_count >= 3:
                row.status = "waiting_user"
                row.progress = "The model endpoint failed repeatedly; user attention is required."
            else:
                row.status = "active"
            row.revision += 1
            self._event(
                db, owner, session_id, "goal_attempt_failed", row.id, row.revision,
                {"error": error, "failure_count": row.failure_count, "status": row.status},
            )
            db.flush()
            return _public_goal(row)

    def complete_goal(self, owner, session_id, summary, evidence):
        summary = _clean_text(summary, "goal completion summary", 12000)
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("complete_goal requires at least one verification item")
        clean_evidence = [str(item).strip()[:2000] for item in evidence if str(item).strip()][:100]
        if not clean_evidence:
            raise ValueError("complete_goal requires verification evidence")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled"}:
                raise WorkNotFound("Active goal not found")
            row.progress = summary
            row.checkpoint = {**dict(row.checkpoint or {}), "evidence": clean_evidence}
            row.status = "completed"
            row.completed_at = utcnow_naive()
            row.revision += 1
            self._event(db, owner, session_id, "goal_completed", row.id, row.revision, {"summary": summary, "evidence": clean_evidence})
            db.flush()
            return _public_goal(row)

    def acquire_goal_lease(self, owner, session_id, ttl_seconds=90):
        """Fence duplicate continuation controllers after reconnect/restart."""
        now = utcnow_naive()
        token = uuid.uuid4().hex
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            expires = now + timedelta(seconds=max(15, min(int(ttl_seconds), 300)))
            # Conditional UPDATE makes lease acquisition a real CAS. Two web
            # workers recovering the same Goal cannot both observe an empty
            # lease and then dispatch duplicate autonomous attempts.
            changed = db.query(ChatGoal).filter(
                ChatGoal.owner == owner,
                ChatGoal.session_id == session_id,
                ChatGoal.status == "active",
                or_(ChatGoal.lease_token.is_(None), ChatGoal.lease_expires_at.is_(None), ChatGoal.lease_expires_at <= now),
            ).update({"lease_token": token, "lease_expires_at": expires}, synchronize_session=False)
            return token if changed == 1 else None

    def consume_goal_lease(self, owner, session_id, token):
        token = _clean_text(token, "goal continuation token", 200)
        now = utcnow_naive()
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=owner, session_id=session_id).first()
            if row is None or row.status != "active":
                raise WorkConflict("Goal is not ready to continue")
            if row.lease_token != token or not row.lease_expires_at or row.lease_expires_at <= now:
                raise WorkConflict("Goal continuation lease expired")
            row.lease_token = None
            row.lease_expires_at = None
            row.attempt += 1
            row.revision += 1
            self._event(db, owner, session_id, "goal_attempt_started", row.id, row.revision, {"attempt": row.attempt})
            db.flush()
            return _public_goal(row)


store = ChatWorkStore()
