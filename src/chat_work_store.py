"""Durable owner-scoped Plan and Goal state for ordinary chats.

The tables live beside chat sessions so backups and rollback-compatible builds
keep the transcript and its work state together.  Every mutation uses a
revision check when initiated by the UI; model tools operate on the exact
session/owner context supplied by the server-side dispatcher.
"""
from __future__ import annotations

from datetime import timedelta, timezone
import hashlib
import json
import re
import uuid
from sqlalchemy import or_

from core.database import (
    ChatGoal, ChatMessage, ChatPlan, ChatRunState, ChatWorkEvent, Session as DbSession, SessionLocal,
    reserve_sqlite_writer, utcnow_naive,
)
from src.run_wait_state import CONTEXT_FAILURE_CODES


PLAN_STATES = {"pending", "in_progress", "done", "blocked"}
PLAN_STATUS = {"draft", "approved", "executing", "done", "cancelled"}
GOAL_STATUS = {"active", "paused", "waiting_user", "review_required", "completed", "cancelled"}
_SINGLE_USER_OWNER_KEY = "__odysseus_single_user__"


def _storage_owner(owner):
    """Map the auth-disabled owner to a durable non-null scope key."""
    return str(owner or _SINGLE_USER_OWNER_KEY)


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


def _clean_string_list(value, name, *, count=100, item_limit=2000):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > count:
        raise ValueError(f"{name} must be a bounded list")
    return [_clean_text(item, name, item_limit) for item in value]


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
        "owner": None if row.owner == _SINGLE_USER_OWNER_KEY else row.owner,
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
    return steps


def _stable_step_id(text, ordinal=1):
    """Return an opaque, deterministic ID for legacy markdown steps.

    IDs must survive a reparse/reorder of the same plan.  The ordinal only
    disambiguates duplicate step text; authored IDs are still preferred.
    """
    digest = hashlib.sha256(str(text).strip().casefold().encode("utf-8")).hexdigest()[:20]
    return f"step-{digest}-{int(ordinal)}"


def _reconcile_plan_steps(normalized, old_steps, *, preserve_progress=False):
    """Keep opaque step identity and, for executing plans, durable progress.

    Legacy ``update_plan`` clients send a whole markdown checklist whose
    checkboxes may be stale or default to pending.  It is a plan-revision
    adapter, not an alternate progress API; only ``update_plan_step`` may
    change an executing step's status.
    """
    old_by_id = {
        str(step.get("id")): step for step in old_steps
        if isinstance(step, dict) and step.get("id")
    }
    old_by_text = {}
    for old in old_steps:
        if not isinstance(old, dict) or not old.get("id"):
            continue
        old_by_text.setdefault(
            str(old.get("text", "")).strip().casefold(), []
        ).append(old)
    used_ids = set()
    for step in normalized:
        old = old_by_id.get(step.get("id"))
        if old is None or str(old.get("id")) in used_ids:
            old = next((candidate for candidate in old_by_text.get(
                step["text"].strip().casefold(), []
            ) if str(candidate.get("id")) not in used_ids), None)
        if old is None:
            continue
        old_id = str(old["id"])
        used_ids.add(old_id)
        step["id"] = old_id
        if preserve_progress:
            step["status"] = old.get("status", "pending")
            step["required"] = old.get("required", True)
            for key in ("summary", "progress"):
                if key in old:
                    step[key] = old[key]


class ChatWorkStore:
    def _event(self, db, owner, session_id, kind, entity_id, revision, payload):
        db.add(ChatWorkEvent(
            session_id=session_id, owner=_storage_owner(owner), kind=kind,
            entity_id=entity_id, revision=revision, payload=payload or {},
        ))

    def get(self, owner, session_id):
        with SessionLocal() as db:
            _session(db, owner, session_id)
            stored_owner = _storage_owner(owner)
            plan = db.query(ChatPlan).filter_by(owner=stored_owner, session_id=session_id).first()
            goal = db.query(ChatGoal).filter_by(owner=stored_owner, session_id=session_id).first()
            cursor = db.query(ChatWorkEvent.id).filter_by(owner=stored_owner, session_id=session_id).order_by(ChatWorkEvent.id.desc()).limit(1).scalar()
            return {"plan": _public_plan(plan), "goal": _public_goal(goal), "cursor": cursor or 0}

    def wait_metadata(self, owner, session_id):
        """Return owner-scoped Goal/lease state without objective or lease token."""
        with SessionLocal() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
            ).first()
            if row is None:
                return {"status": None, "attempt": None, "revision": None,
                        "lease_held": False, "lease_expires_at": None,
                        "status_since": None}
            lease_held = bool(
                row.lease_token and row.lease_expires_at
                and row.lease_expires_at > utcnow_naive()
            )
            return {
                "status": row.status,
                "attempt": row.attempt,
                "revision": row.revision,
                "wait_reason": (
                    dict(row.checkpoint or {}).get("_wait_reason")
                    if row.status == "waiting_user" or (
                        row.status == "review_required"
                        and dict(row.checkpoint or {}).get("_wait_reason") in {
                            "repeated_premature_stop", "repeated_action_observation",
                        }
                    ) else None
                ),
                "failure_code": (
                    dict(row.checkpoint or {}).get("failure_code")
                    if row.status == "waiting_user"
                    and dict(row.checkpoint or {}).get("_wait_reason") == "context_compaction"
                    and dict(row.checkpoint or {}).get("failure_code") in CONTEXT_FAILURE_CODES
                    else None
                ),
                "budget": (
                    dict(row.checkpoint or {}).get("budget")
                    if row.status == "waiting_user"
                    and dict(row.checkpoint or {}).get("_wait_reason") == "resource_budget"
                    else None
                ),
                "status_since": (
                    row.updated_at.replace(tzinfo=timezone.utc).timestamp()
                    if row.updated_at else None
                ),
                "lease_held": lease_held,
                "lease_expires_at": (
                    row.lease_expires_at.isoformat() + "Z" if lease_held else None
                ),
            }

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
            stored_owner = _storage_owner(owner)
            rows = db.query(ChatWorkEvent).filter(
                ChatWorkEvent.owner == stored_owner, ChatWorkEvent.session_id == session_id,
                ChatWorkEvent.id > after,
            ).order_by(ChatWorkEvent.id).limit(limit).all()
            return [{
                "seq": row.id, "type": row.kind, "entity_id": row.entity_id,
                "revision": row.revision, "data": dict(row.payload or {}),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            } for row in rows]

    def save_plan(self, owner, session_id, title, steps, *, expected_revision=None,
                  replace_terminal=False, replace_previous_goal_plan_run_id=None,
                  replace_after_run_id=None):
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
            reserve_sqlite_writer(db)
            _session(db, owner, session_id)
            stored_owner = _storage_owner(owner)
            row = db.query(ChatPlan).filter_by(owner=stored_owner, session_id=session_id).first()
            if row is None:
                if expected_revision not in (None, 0):
                    raise WorkConflict("Plan changed; reload")
                row = ChatPlan(id=uuid.uuid4().hex, owner=stored_owner, session_id=session_id)
                db.add(row)
                db.flush()
            else:
                if replace_after_run_id and row.status in {"cancelled", "done"}:
                    origin_run = db.query(ChatRunState).filter_by(
                        owner=stored_owner, session_id=session_id,
                        run_id=replace_after_run_id, status="running",
                    ).first()
                    active_goal = db.query(ChatGoal).filter_by(
                        owner=stored_owner, session_id=session_id,
                    ).first()
                    # A terminal Plan belongs to its earlier run. A later
                    # owner-scoped run may start a fresh one, but a stale call
                    # from the finishing run or the same unfinished Goal must
                    # never reopen it. A newly created Goal has its own
                    # replacement path below.
                    if (origin_run is not None and origin_run.started_at
                            and row.updated_at and origin_run.started_at > row.updated_at
                            and (active_goal is None or active_goal.status in {"completed", "cancelled"})):
                        replace_terminal = True
                if replace_previous_goal_plan_run_id and row.status in {"cancelled", "done"}:
                    goal = db.query(ChatGoal).filter_by(owner=stored_owner, session_id=session_id).first()
                    origin_run = db.query(ChatRunState).filter_by(
                        owner=stored_owner, session_id=session_id, run_id=replace_previous_goal_plan_run_id,
                        status="running",
                    ).first()
                    # A new explicit Goal may replace its predecessor's plan;
                    # a finished plan from this same Goal remains immutable.
                    # Evaluate under the same writer lock as the Plan CAS.
                    if (goal is not None and goal.status == "active" and goal.created_at
                            and origin_run is not None and (origin_run.continuation or {}).get("goal") is True
                            and origin_run.started_at and origin_run.started_at >= goal.created_at
                            and row.updated_at and goal.created_at > row.updated_at):
                        replace_terminal = True
                if row.status in {"cancelled", "done"} and not replace_terminal:
                    raise WorkConflict("Plan is no longer mutable")
                if expected_revision is not None and row.revision != expected_revision:
                    raise WorkConflict("Plan changed; reload")
                row.revision += 1
            # Legacy update_plan is an adapter. Once execution has started it
            # must not silently reset or advance durable step progress. Reconcile
            # IDs by explicit ID first, then matching text for old clients.
            replaces_finished_plan = bool(
                replace_terminal and row is not None and row.status in {"cancelled", "done"}
            )
            if row is not None and row.steps and not replaces_finished_plan:
                _reconcile_plan_steps(
                    normalized, row.steps,
                    # Recovery may replace an old terminal Plan, but a fresh
                    # post-compaction projection of the currently executing
                    # Plan must retain verified progress for matching steps.
                    preserve_progress=row.status == "executing",
                )
            step_ids = [step["id"] for step in normalized]
            if len(step_ids) != len(set(step_ids)):
                raise ValueError("Plan step IDs must be unique")
            if replaces_finished_plan:
                # Preserve the old generation in ChatWorkEvent and give this
                # Plan its own identity. Keep revision monotonic so stale UI
                # actions still fail their CAS check.
                row.id = uuid.uuid4().hex
                row.created_at = utcnow_naive()
            prior_status = "draft" if replaces_finished_plan else (row.status if row is not None else "draft")
            if prior_status in {"draft", "approved"} and any(
                step["status"] != "pending" for step in normalized
            ):
                raise WorkConflict("Execute the plan before updating step progress")
            status = prior_status if prior_status in {"executing", "done"} else "draft"
            if status == "executing" and not any(
                step.get("required", True) and step.get("status") != "done"
                for step in normalized
            ):
                status = "done"
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
            row = db.query(ChatPlan).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
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

    def ensure_plan_executing(self, owner, session_id):
        """Start a draft Plan or resume its current step after checkpoint recovery."""
        plan = self.get(owner, session_id).get("plan")
        if not isinstance(plan, dict):
            raise WorkNotFound("Plan not found")
        status = plan.get("status")
        if status in {"draft", "approved"}:
            return self.plan_action(owner, session_id, "execute", plan["revision"])
        if status != "executing":
            raise WorkConflict("Plan cannot resume in its current state")
        steps = [step for step in plan.get("steps", []) if isinstance(step, dict)]
        unfinished = [step for step in steps
                      if step.get("required", True) and step.get("status") != "done"]
        if not unfinished:
            return plan
        current = next((step for step in unfinished if step.get("status") == "in_progress"), None)
        if current:
            return plan
        return self.update_plan_step(
            owner, session_id, str(unfinished[0].get("id") or ""), "in_progress",
            expected_revision=plan["revision"],
        )

    def revise_goal(self, owner, session_id, objective, expected_revision):
        objective = _clean_text(objective, "goal", 12000)
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
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

    def update_plan_step(self, owner, session_id, step_id, status, *, summary="", progress=None,
                         expected_revision=None):
        if status not in PLAN_STATES:
            raise ValueError("Invalid plan step status")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatPlan).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
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
            if progress is not None:
                if not isinstance(progress, dict) or set(progress) - {
                    "files_changed", "verification", "decisions", "next_work",
                }:
                    raise ValueError("Invalid plan progress snapshot")
                target["progress"] = {
                    "files_changed": _clean_string_list(progress.get("files_changed"), "files_changed"),
                    "verification": _clean_string_list(progress.get("verification"), "verification"),
                    "decisions": _clean_string_list(progress.get("decisions"), "decisions"),
                    "next_work": _clean_string_list(progress.get("next_work"), "next_work"),
                }
            unfinished = [s for s in steps if s.get("required", True) and s.get("status") != "done"]
            row.steps = steps
            row.status = "done" if not unfinished else "executing"
            row.current_step_id = next((s["id"] for s in steps if s.get("status") == "in_progress"), None) or (unfinished[0]["id"] if unfinished else None)
            row.revision += 1
            self._event(db, owner, session_id, "plan_step_updated", row.id, row.revision, {
                "step_id": step_id, "status": status, "summary": summary,
                "progress": dict(target.get("progress") or {}),
            })
            db.flush()
            return _public_plan(row)

    def ensure_goal(self, owner, session_id, objective):
        objective = _clean_text(objective, "goal", 12000)
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            stored_owner = _storage_owner(owner)
            row = db.query(ChatGoal).filter_by(owner=stored_owner, session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled"}:
                if row is not None:
                    db.delete(row)
                    db.flush()
                row = ChatGoal(id=uuid.uuid4().hex, owner=stored_owner, session_id=session_id, objective=objective)
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
            row = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
            if row is None:
                raise WorkNotFound("Goal not found")
            if row.revision != expected_revision:
                raise WorkConflict("Goal changed; reload")
            if row.status in {"completed", "cancelled"}:
                raise WorkConflict("Goal is already terminal")
            row.status = statuses[action]
            if action == "resume":
                row.checkpoint = {
                    key: value for key, value in dict(row.checkpoint or {}).items()
                    if key not in {"_wait_reason", "auto_decide_question_id"}
                }
            row.revision += 1
            event_kind = {"pause": "goal_paused", "resume": "goal_resumed", "cancel": "goal_cancelled"}[action]
            self._event(db, owner, session_id, event_kind, row.id, row.revision, _public_goal(row))
            db.flush()
            return _public_goal(row)

    def expired_goal_questions(self, timeout_seconds=60, limit=100):
        """Return only identifiers for ordinary unanswered Goal questions."""
        cutoff = utcnow_naive() - timedelta(seconds=max(1, int(timeout_seconds)))
        with SessionLocal() as db:
            rows = db.query(ChatGoal).filter(
                ChatGoal.status == "waiting_user", ChatGoal.updated_at <= cutoff,
            ).order_by(ChatGoal.updated_at.desc()).limit(max(1, min(int(limit), 100))).all()
            return [
                {"owner": None if row.owner == _SINGLE_USER_OWNER_KEY else row.owner,
                 "session_id": row.session_id, "goal_id": row.id,
                 "revision": row.revision, "question_id": checkpoint.get("question_id")}
                for row in rows
                for checkpoint in [dict(row.checkpoint or {})]
                if checkpoint.get("_wait_reason") == "ask_user"
                and isinstance(checkpoint.get("question_id"), str)
            ]

    def resume_expired_goal_question(self, owner, session_id, goal_id, expected_revision,
                                     *, timeout_seconds=60):
        """CAS timeout of ask_user only; never infer approval for an effect."""
        cutoff = utcnow_naive() - timedelta(seconds=max(1, int(timeout_seconds)))
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(
                owner=_storage_owner(owner), session_id=session_id, id=goal_id,
                status="waiting_user", revision=expected_revision,
            ).first()
            if row is None or not row.updated_at or row.updated_at > cutoff:
                return None
            checkpoint = dict(row.checkpoint or {})
            question_id = checkpoint.get("question_id")
            if checkpoint.get("_wait_reason") != "ask_user" or not isinstance(question_id, str):
                return None
            row.status = "active"
            row.progress = "No answer arrived in one minute; continuing with a safe model decision."
            row.checkpoint = {
                **{key: value for key, value in checkpoint.items() if key != "_wait_reason"},
                "auto_decide_question_id": question_id,
            }
            row.revision += 1
            self._event(db, owner, session_id, "goal_question_timed_out", row.id, row.revision,
                        {"question_id": question_id, "timeout_seconds": timeout_seconds})
            db.flush()
            return _public_goal(row)

    def add_goal_guidance(self, owner, session_id, message):
        """Durably append human guidance without pausing the active Goal."""
        message = _clean_text(message, "goal guidance", 12000)
        now = utcnow_naive()
        with SessionLocal.begin() as db:
            session = _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
            ).first()
            if row is None or row.status != "active":
                raise WorkConflict("Goal is not active")
            guidance = dict(row.checkpoint or {}).get("guidance") or []
            guidance = list(guidance) if isinstance(guidance, list) else []
            item = {"id": uuid.uuid4().hex, "text": message, "created_at": now.isoformat()}
            guidance.append(item)
            guidance = guidance[-100:]
            row.checkpoint = {
                **{key: value for key, value in dict(row.checkpoint or {}).items()
                   if key != "auto_decide_question_id"},
                "guidance": guidance,
            }
            row.progress = "Additional user guidance received; continuing the active Goal."
            row.revision += 1
            db.add(ChatMessage(
                id=uuid.uuid4().hex, session_id=session_id, role="user", content=message,
                meta_data=json.dumps({"goal_guidance": True, "guidance_id": item["id"]}),
                timestamp=now,
            ))
            session.message_count = int(session.message_count or 0) + 1
            session.last_message_at = now
            self._event(
                db, owner, session_id, "goal_guidance", row.id, row.revision,
                {"guidance": item, "goal": _public_goal(row)},
            )
            db.flush()
            return {"goal": _public_goal(row), "guidance": item}

    def add_goal_background_context(self, owner, session_id, context_message, job_id):
        """Queue a completed background result under the active Goal lease.

        The content stays explicitly untrusted and hidden from the visible
        transcript.  The live loop consumes the guidance at its next round;
        a later Goal attempt sees the same hidden durable message in history.
        """
        if not isinstance(context_message, dict) or context_message.get("role") != "user":
            raise ValueError("Background context must be a user-role message object")
        metadata = dict(context_message.get("metadata") or {})
        if metadata.get("trusted") is not False:
            raise ValueError("Background context must remain untrusted")
        content = str(context_message.get("content") or "").strip()
        if not content or "\0" in content:
            raise ValueError("Background context must contain text")
        if len(content) > 12000:
            content = (
                content[:6000]
                + "\n[Background result excerpt; middle omitted. Full result remains in the job log.]\n"
                + content[-6000:]
            )
        job_id = _clean_text(str(job_id or ""), "background job id", 200)
        now = utcnow_naive()
        with SessionLocal.begin() as db:
            session = _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
            ).first()
            if row is None or row.status not in {"active", "paused", "waiting_user", "review_required"}:
                raise WorkConflict("Goal is not available for background context")
            guidance = dict(row.checkpoint or {}).get("guidance") or []
            guidance = list(guidance) if isinstance(guidance, list) else []
            item = {
                "id": uuid.uuid4().hex,
                "context_message": {"role": "user", "content": content, "metadata": metadata},
                "source": "background_job",
                "job_id": job_id,
                "created_at": now.isoformat(),
            }
            guidance.append(item)
            row.checkpoint = {**dict(row.checkpoint or {}), "guidance": guidance[-100:]}
            row.revision += 1
            hidden_metadata = {
                **metadata,
                "hidden": 1,
                "hidden_from_user_view": True,
                "bg_job_id": job_id,
                "goal_background_context": True,
                "guidance_id": item["id"],
            }
            db.add(ChatMessage(
                id=uuid.uuid4().hex, session_id=session_id, role="user", content=content,
                meta_data=json.dumps(hidden_metadata), timestamp=now,
            ))
            session.message_count = int(session.message_count or 0) + 1
            session.last_message_at = now
            self._event(
                db, owner, session_id, "background_context", row.id, row.revision,
                {"guidance_id": item["id"], "job_id": job_id},
            )
            db.flush()
            return {"goal": _public_goal(row), "guidance": item}

    def update_goal(self, owner, session_id, progress, checkpoint=None, *,
                    waiting_user=False, review_required=False, reset_failures=True,
                    expected_goal_id=None, expected_attempt=None):
        progress = _clean_text(progress, "goal progress", 12000)
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError("Goal checkpoint must be an object")
        if (expected_goal_id is None) != (expected_attempt is None):
            raise ValueError("Goal ID and attempt must be supplied together")
        if expected_goal_id is not None and (
            not isinstance(expected_goal_id, str) or type(expected_attempt) is not int
            or expected_attempt < 1
        ):
            raise ValueError("Invalid expected goal attempt")
        if waiting_user and review_required:
            raise ValueError("Goal cannot wait for an answer and require review simultaneously")
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
            if row is None:
                raise WorkNotFound("Active goal not found")
            if expected_goal_id is not None and (
                row.id != expected_goal_id or row.attempt != expected_attempt
                or row.status != "active"
            ):
                raise WorkConflict("Goal attempt changed before progress settlement")
            if row.status == "waiting_user" and waiting_user:
                previous = dict(row.checkpoint or {})
                question_id = (checkpoint or {}).get("question_id")
                if (
                    question_id
                    and previous.get("_wait_reason") == "ask_user"
                    and previous.get("question_id") == question_id
                ):
                    # A detached stream may race another completion path that
                    # already checkpointed this exact ask_user. Treat that
                    # duplicate as success without revising the Goal or adding
                    # a second durable event.
                    return _public_goal(row)
                raise WorkConflict("Goal is already waiting for a different user decision")
            if row.status in {"completed", "cancelled", "paused", "waiting_user", "review_required"}:
                raise WorkNotFound("Active goal not found")
            row.progress = progress
            if checkpoint is not None:
                # Progress/tool checkpoints are partial updates. Never erase
                # the durable model ledger, prior tool results, or approval
                # provenance when a later event only carries one field.
                row.checkpoint = {**dict(row.checkpoint or {}), **checkpoint}
            if waiting_user or review_required:
                reason = (checkpoint or {}).get("reason")
                row.checkpoint = {
                    **dict(row.checkpoint or {}),
                    "_wait_reason": (
                        "repeated_premature_stop" if reason == "repeated_premature_stop"
                        else "repeated_action_observation" if reason == "repeated_action_observation"
                        else "unknown_side_effect" if reason == "unknown_side_effect"
                        else "ask_user" if (checkpoint or {}).get("question_id")
                        else "other"
                    ),
                }
            else:
                row.checkpoint = {
                    key: value for key, value in dict(row.checkpoint or {}).items()
                    if key != "_wait_reason"
                }
            row.status = "review_required" if review_required else "waiting_user" if waiting_user else "active"
            if review_required:
                row.lease_token = None
                row.lease_expires_at = None
            if not waiting_user and reset_failures:
                row.failure_count = 0
                row.last_error = None
                row.failure_key = None
            row.revision += 1
            self._event(db, owner, session_id, "goal_progress", row.id, row.revision, {"progress": progress, "checkpoint": checkpoint or {}, "status": row.status})
            db.flush()
            return _public_goal(row)

    def record_goal_failure(self, owner, session_id, error, checkpoint=None, *,
                            force_wait_user=False, expected_goal_id=None,
                            expected_attempt=None, failure_attempts=1):
        """Persist bounded transport/model/checkpoint retry state."""
        error = _clean_text(error, "goal error", 2000)
        if checkpoint is not None and not isinstance(checkpoint, dict):
            raise ValueError("Goal checkpoint must be an object")
        if type(failure_attempts) is not int or not 1 <= failure_attempts <= 10:
            raise ValueError("Goal failure attempts must be between 1 and 10")
        if (expected_goal_id is None) != (expected_attempt is None):
            raise ValueError("Goal ID and attempt must be supplied together")
        with SessionLocal.begin() as db:
            reserve_sqlite_writer(db)
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
            if row is None or row.status in {"completed", "cancelled", "paused", "waiting_user", "review_required"}:
                raise WorkNotFound("Active goal not found")
            if expected_goal_id is not None and (
                row.id != expected_goal_id or row.attempt != expected_attempt
            ):
                raise WorkConflict("Goal attempt changed before failure settlement")
            # Count failed attempts, not identical error strings. Providers
            # can alternate timeout/503/schema errors without any successful
            # work; changing wording must not reset the retry budget.
            provider_http = (
                (checkpoint or {}).get("reason") == "provider_http"
                or (checkpoint or {}).get("failure_class") == "provider_http"
            )
            failure_key = "provider_http" if provider_http else "other"
            if row.failure_key and row.failure_key != failure_key:
                row.failure_count = 0
            row.failure_key = failure_key
            row.failure_count = int(row.failure_count or 0) + failure_attempts
            row.last_error = error
            row.lease_token = None
            row.lease_expires_at = None
            row.progress = "Model attempt failed; the server will retry automatically."
            if checkpoint is not None:
                row.checkpoint = {**dict(row.checkpoint or {}), **checkpoint}
            failure_limit = 10 if provider_http else 3
            if force_wait_user or row.failure_count >= failure_limit:
                row.status = "paused" if provider_http and not force_wait_user else "waiting_user"
                checkpoint_failed = (checkpoint or {}).get("reason") == "context_compaction"
                dispatch_failed = (checkpoint or {}).get("reason") == "continuation_dispatch_failed"
                dispatch_code = (checkpoint or {}).get("failure_code")
                dispatch_retry_exhausted = dispatch_failed and (checkpoint or {}).get("retry_exhausted") is True
                row.progress = (
                    "Goal continuation did not start. Choose a model for this chat, then retry explicitly."
                    if dispatch_failed and dispatch_code == "model_unselected" else
                    "Goal continuation did not start. Choose an available model endpoint, then retry explicitly."
                    if dispatch_failed and dispatch_code == "model_endpoint_unavailable" else
                    "Goal continuation dispatcher failed ten attempts; Goal paused."
                    if dispatch_retry_exhausted else
                    "Goal continuation did not start; inspect the endpoint and retry explicitly."
                    if dispatch_failed else
                    "Context checkpoint failed repeatedly; check the summarizer or context policy before resuming."
                    if checkpoint_failed else
                    "The model endpoint failed ten consecutive requests; Goal paused."
                    if provider_http else
                    "The model endpoint failed repeatedly; user attention is required."
                )
                row.checkpoint = {
                    **dict(row.checkpoint or {}),
                    "_wait_reason": (
                        "dispatch_failure" if dispatch_failed else
                        "context_compaction" if checkpoint_failed else "provider_failure"
                    ),
                }
            else:
                row.status = "active"
                row.checkpoint = {
                    key: value for key, value in dict(row.checkpoint or {}).items()
                    if key != "_wait_reason"
                }
            row.revision += 1
            self._event(
                db, owner, session_id, "goal_attempt_failed", row.id, row.revision,
                {"error": error, "failure_count": row.failure_count, "status": row.status},
            )
            db.flush()
            return _public_goal(row)

    def wait_on_goal_budget(self, owner, session_id, *, resource, used, limit, run_id,
                            expected_goal_id, expected_attempt, usage_source=None):
        """Hard budget is a user decision point, never a silent Goal retry."""
        if resource not in {"tool_calls", "model_rounds", "model_tokens", "model_requests", "wall_seconds", "children"} or type(used) is not int or type(limit) is not int:
            raise ValueError("Valid run budget required")
        if not 1 <= limit <= 10_000_000 or used < limit or used > 100_000_000:
            raise ValueError("Budget usage is invalid")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Exact run ID required")
        if not isinstance(expected_goal_id, str) or type(expected_attempt) is not int:
            raise ValueError("Exact goal attempt required")
        if usage_source is not None and (resource != "model_tokens" or usage_source not in {"real", "estimated", "mixed"}):
            raise ValueError("Invalid budget usage source")
        budget = {"resource": resource, "used": used, "limit": limit, "run_id": run_id}
        if usage_source is not None:
            budget["usage_source"] = usage_source
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
            ).first()
            if (row is None or row.status != "active" or row.id != expected_goal_id
                    or row.attempt != expected_attempt):
                raise WorkConflict("Active goal changed before budget settlement")
            row.status = "waiting_user"
            label = {"tool_calls": "Tool-call", "model_rounds": "Model-round",
                     "model_tokens": "Model-token", "model_requests": "Model-request",
                     "wall_seconds": "Wall-time",
                     "children": "Child-agent"}[resource]
            row.progress = f"{label} limit reached ({used}/{limit}); review the budget before resuming."
            row.checkpoint = {**dict(row.checkpoint or {}), "budget": budget,
                              "_wait_reason": "resource_budget"}
            row.lease_token = None
            row.lease_expires_at = None
            row.revision += 1
            self._event(db, owner, session_id, "goal_budget_exceeded", row.id, row.revision, budget)
            db.flush()
            return _public_goal(row)

    def clear_goal_failure(self, owner, session_id, *, reason="recovered"):
        """Clear retry backoff after verified runtime recovery without
        overwriting the Goal's user-visible progress or durable checkpoint.
        """
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
            ).first()
            if row is None or row.status != "active":
                raise WorkNotFound("Active goal not found")
            if not row.failure_count and not row.last_error:
                return _public_goal(row)
            row.failure_count = 0
            row.last_error = None
            row.failure_key = None
            row.revision += 1
            self._event(
                db, owner, session_id, "goal_retry_recovered", row.id, row.revision,
                {"reason": _clean_text(reason, "recovery reason", 200)},
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
            row = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
            # An ordinary Agent run may still be started while the Goal is
            # paused. Its tool calls must not silently finish that Goal:
            # only an explicit resume returns it to the active state.
            if row is None or row.status != "active":
                raise WorkNotFound("Active goal not found")
            plan = db.query(ChatPlan).filter_by(
                owner=_storage_owner(owner), session_id=session_id,
            ).first()
            if (
                plan is not None
                and plan.status in {"approved", "executing"}
                and any(step.get("required", True) and step.get("status") != "done"
                        for step in (plan.steps or []))
            ):
                raise WorkConflict(
                    "Complete the approved plan's required steps before completing the Goal"
                )
            row.progress = summary
            row.checkpoint = {**dict(row.checkpoint or {}), "evidence": clean_evidence}
            row.status = "completed"
            row.completed_at = utcnow_naive()
            row.revision += 1
            self._event(db, owner, session_id, "goal_completed", row.id, row.revision, {"summary": summary, "evidence": clean_evidence})
            db.flush()
            return _public_goal(row)

    def acquire_goal_lease(self, owner, session_id, ttl_seconds=300, *,
                           expected_goal_id=None, expected_attempt=None):
        """Fence duplicate continuation controllers after reconnect/restart."""
        if (expected_goal_id is None) != (expected_attempt is None):
            raise ValueError("Goal ID and attempt must be supplied together")
        if expected_goal_id is not None and (
            not isinstance(expected_goal_id, str) or type(expected_attempt) is not int
            or expected_attempt < 1
        ):
            raise ValueError("Invalid expected goal attempt")
        now = utcnow_naive()
        token = uuid.uuid4().hex
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            # Large sessions can spend well over a minute hydrating history
            # and building the first request before /api/chat_stream consumes
            # this lease. Keep the default at the bounded dispatch maximum so
            # a valid autonomous continuation is not rejected as stale.
            expires = now + timedelta(seconds=max(15, min(int(ttl_seconds), 300)))
            # Conditional UPDATE makes lease acquisition a real CAS. Two web
            # workers recovering the same Goal cannot both observe an empty
            # lease and then dispatch duplicate autonomous attempts.
            query = db.query(ChatGoal).filter(
                ChatGoal.owner == _storage_owner(owner),
                ChatGoal.session_id == session_id,
                ChatGoal.status == "active",
                or_(ChatGoal.lease_token.is_(None), ChatGoal.lease_expires_at.is_(None), ChatGoal.lease_expires_at <= now),
            )
            if expected_goal_id is not None:
                query = query.filter(
                    ChatGoal.id == expected_goal_id,
                    ChatGoal.attempt == expected_attempt,
                )
            changed = query.update({"lease_token": token, "lease_expires_at": expires}, synchronize_session=False)
            return token if changed == 1 else None

    def consume_goal_lease(self, owner, session_id, token):
        token = _clean_text(token, "goal continuation token", 200)
        now = utcnow_naive()
        with SessionLocal.begin() as db:
            _session(db, owner, session_id)
            row = db.query(ChatGoal).filter_by(owner=_storage_owner(owner), session_id=session_id).first()
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
