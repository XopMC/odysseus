"""Durable compaction settlement records used by resumed Agent/Goal runs."""

from __future__ import annotations

import uuid
import copy
import json
from typing import Optional

from src.database import ChatContextCompaction, ChatRunState, ChatSubagentEvent, SessionLocal, utcnow_naive


_CHILD_COMPACTION_KEY = "_child_context_compaction"


def _locked_context_child(db, owner, session_id, child_id, *, write=False):
    """Scope child context state and fence even attempts to target a sibling."""
    from src.subagent_runtime import ChildLeaseLost, _execution_lease, _locked_child

    if (not isinstance(child_id, str) or not 1 <= len(child_id) <= 200
            or not isinstance(session_id, str) or not session_id):
        raise ValueError("Exact child context scope required")
    lease = _execution_lease.get()
    if (lease is not None and lease[:2] != (child_id, owner or "")) or (write and lease is None):
        raise ChildLeaseLost("Current child execution lease required")
    child = _locked_child(db, child_id, owner)
    if child is None or child.parent_session_id != session_id or child.removed:
        raise ValueError("Child context scope unavailable")
    if write and child.cancel_requested:
        raise ChildLeaseLost("Child execution was stopped")
    return child


def _bounded_child_state(value):
    # Preserve the existing state shape and array semantics. Reject oversized
    # updates instead of putting unbounded transcripts into the child row.
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded) > 65536:
        raise ValueError("Child context state exceeds its durable limit")
    return json.loads(encoded)


def _marker(generation, *, child=False):
    return {
        "kind": "rebuild_plan_after_compaction", "generation": generation,
        "mandatory": True,
        "required_tools": ["create_plan", "update_plan", "update_plan_step"],
        "instruction": (
            "Online context compaction finished. Re-read the child objective and its saved checkpoint, "
            "then rebuild the child's private remaining-work plan before continuing. "
            "Use create_plan, update_plan, or update_plan_step for the child; do not modify the parent Goal or plan."
            if child else
            "Online context compaction finished and the parent task is still active. "
            "Before any other work, re-read the active goal/checkpoint and rebuild a fresh "
            "remaining-work plan by calling create_plan, update_plan, or update_plan_step. "
            "Do not continue execution or merely describe a plan until that tool call succeeds."
        ),
    }


def _child_record(owner, session_id, generation, *, child_id, ledger_hash, before_tokens, after_tokens, economics):
    with SessionLocal() as db:
        child = _locked_context_child(db, owner, session_id, child_id, write=True)
        metrics = dict(child.metrics or {})
        previous = metrics.get(_CHILD_COMPACTION_KEY) or {}
        generation = max(int(generation), int(previous.get("generation") or 0) + 1)
        state = _bounded_child_state({
            "id": uuid.uuid4().hex, "run_id": child_id, "generation": generation,
            "status": "pending_settlement", "ledger_hash": ledger_hash,
            "before_tokens": int(before_tokens), "after_tokens": int(after_tokens),
            "economics": dict(economics or {}), "rebuild_marker": _marker(generation, child=True),
        })
        metrics[_CHILD_COMPACTION_KEY] = state
        child.metrics, child.revision = metrics, int(child.revision or 0) + 1
        db.add(ChatSubagentEvent(child_id=child_id, owner=owner or "", parent_session_id=session_id,
                                kind="context_compaction_recorded", payload=copy.deepcopy(state)))
        db.commit()
        return {key: copy.deepcopy(state[key]) for key in ("id", "run_id", "status", "rebuild_marker")}


def record(owner: Optional[str], session_id: str, generation: int, *, ledger_hash: str,
           before_tokens: int, after_tokens: int, economics: dict, child_id: Optional[str] = None) -> dict:
    if child_id is not None:
        return _child_record(owner, session_id, generation, child_id=child_id, ledger_hash=ledger_hash,
                             before_tokens=before_tokens, after_tokens=after_tokens, economics=economics)
    db = SessionLocal()
    try:
        newest = db.query(ChatContextCompaction.generation).filter(
            ChatContextCompaction.owner == (owner or ""),
            ChatContextCompaction.session_id == session_id,
        ).order_by(ChatContextCompaction.generation.desc()).first()
        newest_generation = int(newest[0]) if newest else 0
        # A recovered working checkpoint can predate the durable ledger (or
        # omit its counter). Never reuse an old generation after restart.
        generation = max(int(generation), newest_generation + 1)
        run = db.query(ChatRunState).filter(
            ChatRunState.owner == (owner or ""), ChatRunState.session_id == session_id,
            ChatRunState.status == "running",
        ).order_by(ChatRunState.updated_at.desc()).first()
        marker = _marker(generation)
        row = ChatContextCompaction(
            id=uuid.uuid4().hex, owner=owner or "", session_id=session_id,
            run_id=run.run_id if run else None, generation=generation,
            ledger_hash=ledger_hash, before_tokens=int(before_tokens), after_tokens=int(after_tokens),
            economics=dict(economics or {}), rebuild_marker=marker,
        )
        db.add(row)
        if run:
            continuation = dict(run.continuation or {})
            continuation["compaction_settlement"] = {
                "id": row.id, "generation": row.generation, "status": row.status,
                "rebuild_marker": marker,
            }
            run.continuation = continuation
        db.commit()
        return {"id": row.id, "run_id": row.run_id, "status": row.status, "rebuild_marker": marker}
    finally:
        db.close()


def settle(owner: Optional[str], session_id: str, generation: int, *, child_id: Optional[str] = None) -> bool:
    if child_id is not None:
        with SessionLocal() as db:
            child = _locked_context_child(db, owner, session_id, child_id, write=True)
            metrics = dict(child.metrics or {})
            state = copy.deepcopy(metrics.get(_CHILD_COMPACTION_KEY) or {})
            if state.get("generation") != int(generation) or state.get("status") != "pending_settlement":
                return False
            state.update(status="settled", settled_at=utcnow_naive().isoformat())
            metrics[_CHILD_COMPACTION_KEY] = state
            child.metrics, child.revision = metrics, int(child.revision or 0) + 1
            db.add(ChatSubagentEvent(child_id=child_id, owner=owner or "", parent_session_id=session_id,
                                    kind="context_compaction_settled", payload={
                                        "id": state["id"], "generation": state["generation"], "status": "settled",
                                    }))
            db.commit()
            return True
    db = SessionLocal()
    try:
        row = db.query(ChatContextCompaction).filter(
            ChatContextCompaction.owner == (owner or ""),
            ChatContextCompaction.session_id == session_id,
            ChatContextCompaction.generation == int(generation),
        ).order_by(ChatContextCompaction.created_at.desc()).first()
        if not row:
            return False
        # Recovery and a model-requested plan tool may both acknowledge the
        # same durable checkpoint. A previous successful commit is already
        # sufficient evidence; turning this duplicate acknowledgement into
        # failure would stop an otherwise recovered long-running Goal.
        if row.status == "settled":
            return True
        if row.status != "pending_settlement":
            return False
        settled_at = utcnow_naive()
        # A plan rebuilt after generation N necessarily covers the checkpoint
        # produced by every earlier compaction. Close those legacy markers in
        # the same transaction; otherwise Resume sees N-1 as a fresh pending
        # handshake and can loop through old generations forever.
        rows = db.query(ChatContextCompaction).filter(
            ChatContextCompaction.owner == (owner or ""),
            ChatContextCompaction.session_id == session_id,
            ChatContextCompaction.status == "pending_settlement",
            ChatContextCompaction.generation <= int(generation),
        ).all()
        for pending_row in rows:
            pending_row.status = "settled"
            pending_row.settled_at = settled_at
        run_ids = {pending_row.run_id for pending_row in rows if pending_row.run_id}
        for run_id in run_ids:
            run = db.query(ChatRunState).filter(ChatRunState.run_id == run_id).first()
            if run:
                continuation = dict(run.continuation or {})
                state = dict(continuation.get("compaction_settlement") or {})
                state["status"] = "settled"
                continuation["compaction_settlement"] = state
                run.continuation = continuation
        db.commit()
        return True
    finally:
        db.close()


def pending(owner: Optional[str], session_id: str, *, child_id: Optional[str] = None) -> Optional[dict]:
    """Return the newest unsettled handshake so a resumed run cannot bypass it."""
    if child_id is not None:
        with SessionLocal() as db:
            child = _locked_context_child(db, owner, session_id, child_id)
            state = (child.metrics or {}).get(_CHILD_COMPACTION_KEY) or {}
            if state.get("status") != "pending_settlement":
                return None
            return {key: copy.deepcopy(state[key]) for key in
                    ("id", "run_id", "generation", "status", "rebuild_marker")}
    db = SessionLocal()
    try:
        row = db.query(ChatContextCompaction).filter(
            ChatContextCompaction.owner == (owner or ""),
            ChatContextCompaction.session_id == session_id,
            ChatContextCompaction.status == "pending_settlement",
        ).order_by(ChatContextCompaction.created_at.desc()).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "run_id": row.run_id,
            "generation": int(row.generation),
            "status": row.status,
            "rebuild_marker": dict(row.rebuild_marker or {}),
        }
    finally:
        db.close()
