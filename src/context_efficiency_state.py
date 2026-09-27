"""CAS-protected persistent Online Context Compact state."""

from __future__ import annotations

import uuid
import copy
from typing import Callable, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from src.database import ChatContextEfficiencyState, ChatSubagentEvent, SessionLocal


_CHILD_EFFICIENCY_KEY = "_child_context_efficiency"


def initial_state(ratio: float) -> dict:
    return {
        "version": 1, "epoch": 0, "plan": [], "pending_progress": [],
        "request_count": 0, "last_boundary_request_count": 0,
        "completed_boundary_request_counts": [], "last_context_tokens": None,
        "positive_context_delta_total": 0.0, "positive_context_delta_count": 0,
        "native_compaction_count": 0, "cache_debt_tokens": 0.0,
        "cache_debt_repayment_tokens": 0.0, "cache_write_read_ratio": float(ratio),
    }


def _valid(value: object) -> bool:
    if not isinstance(value, dict) or value.get("version") != 1:
        return False
    required = {"epoch", "plan", "pending_progress", "request_count",
                "last_boundary_request_count", "completed_boundary_request_counts",
                "last_context_tokens", "positive_context_delta_total",
                "positive_context_delta_count", "native_compaction_count",
                "cache_debt_tokens", "cache_debt_repayment_tokens", "cache_write_read_ratio"}
    return required <= set(value)


def _child_state(owner, session_id, ratio, child_id, fn=None):
    from src.context_compaction_ledger import _bounded_child_state, _locked_context_child
    from src.subagent_runtime import _execution_lease

    with SessionLocal() as db:
        child = _locked_context_child(db, owner, session_id, child_id, write=fn is not None)
        metrics = dict(child.metrics or {})
        current = copy.deepcopy(metrics.get(_CHILD_EFFICIENCY_KEY))
        valid = _valid(current)
        if valid and fn is None:
            return current
        if not valid:
            current = {**initial_state(ratio), "revision": 0}
        if fn is None and _execution_lease.get() is None:
            return current  # Read-only inspection never creates durable state.
        # Restore can initialize state only under a live, uncancelled lease.
        if child.cancel_requested:
            from src.subagent_runtime import ChildLeaseLost
            raise ChildLeaseLost("Child execution was stopped")
        revision = int(current.pop("revision", 0))
        updated = fn(current) if fn is not None else current
        if not _valid(updated):
            raise ValueError("Invalid context efficiency state")
        updated = _bounded_child_state({**updated, "revision": revision + 1})
        metrics[_CHILD_EFFICIENCY_KEY] = updated
        child.metrics, child.revision = metrics, int(child.revision or 0) + 1
        db.add(ChatSubagentEvent(child_id=child_id, owner=owner or "", parent_session_id=session_id,
                                kind="context_efficiency_updated", payload={
                                    key: updated[key] for key in ("revision", "epoch", "request_count", "native_compaction_count")
                                }))
        db.commit()
        return copy.deepcopy(updated)


def restore(owner: Optional[str], session_id: Optional[str], ratio: float, *, child_id: Optional[str] = None) -> dict:
    if child_id is not None:
        return _child_state(owner, session_id, ratio, child_id)
    if not session_id:
        return {**initial_state(ratio), "revision": 0}
    db = SessionLocal()
    try:
        row = db.query(ChatContextEfficiencyState).filter(
            ChatContextEfficiencyState.owner == (owner or ""),
            ChatContextEfficiencyState.session_id == session_id,
        ).first()
        if row and _valid(row.state):
            return {**dict(row.state), "revision": row.revision}
        if row:
            row.state = initial_state(ratio); row.revision += 1
        else:
            row = ChatContextEfficiencyState(
                id=uuid.uuid4().hex, owner=owner or "", session_id=session_id,
                state=initial_state(ratio), revision=1,
            )
            db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Two browser/controller workers may be the first writers for the
            # same chat.  The unique owner/session key chooses the winner; the
            # loser re-reads it instead of surfacing a transient 500.
            db.rollback()
            winner = db.query(ChatContextEfficiencyState).filter(
                ChatContextEfficiencyState.owner == (owner or ""),
                ChatContextEfficiencyState.session_id == session_id,
            ).first()
            if winner and _valid(winner.state):
                return {**dict(winner.state), "revision": winner.revision}
            raise
        return {**dict(row.state), "revision": row.revision}
    finally:
        db.close()


def mutate(owner: Optional[str], session_id: Optional[str], ratio: float,
           fn: Callable[[dict], dict], *, child_id: Optional[str] = None) -> dict:
    if child_id is not None:
        return _child_state(owner, session_id, ratio, child_id, fn)
    if not session_id:
        result = fn(initial_state(ratio)); return {**result, "revision": 0}
    for _ in range(8):
        current = restore(owner, session_id, ratio)
        revision = int(current.pop("revision", 1))
        updated = fn(dict(current))
        if not _valid(updated):
            raise ValueError("Invalid context efficiency state")
        db = SessionLocal()
        try:
            changed = db.execute(update(ChatContextEfficiencyState).where(
                ChatContextEfficiencyState.owner == (owner or ""),
                ChatContextEfficiencyState.session_id == session_id,
                ChatContextEfficiencyState.revision == revision,
            ).values(state=updated, revision=revision + 1)).rowcount
            db.commit()
            if changed == 1:
                return {**dict(updated), "revision": revision + 1}
        finally:
            db.close()
    raise RuntimeError("Context efficiency state changed concurrently")


def record_provider_request(owner, session_id, ratio, context_tokens: int, *, child_id: Optional[str] = None) -> dict:
    def apply(state):
        last = state.get("last_context_tokens")
        delta = 0 if last is None else int(context_tokens) - int(last)
        debt = max(0.0, float(state["cache_debt_tokens"]) - float(state["cache_debt_repayment_tokens"]))
        state.update(request_count=int(state["request_count"]) + 1,
                     last_context_tokens=max(0, int(context_tokens)),
                     positive_context_delta_total=float(state["positive_context_delta_total"]) + max(0, delta),
                     positive_context_delta_count=int(state["positive_context_delta_count"]) + (1 if delta > 0 else 0),
                     cache_debt_tokens=debt,
                     cache_debt_repayment_tokens=0.0 if debt == 0 else float(state["cache_debt_repayment_tokens"]))
        return state
    return mutate(owner, session_id, ratio, apply, child_id=child_id)


def record_boundary(owner, session_id, ratio, plan: list, progress: Optional[dict], *, child_id: Optional[str] = None) -> dict:
    def apply(state):
        interval = max(0, int(state["request_count"]) - int(state["last_boundary_request_count"]))
        state["plan"] = list(plan or [])
        if progress:
            state["pending_progress"] = [*state["pending_progress"], dict(progress)]
        state["last_boundary_request_count"] = int(state["request_count"])
        state["completed_boundary_request_counts"] = [*state["completed_boundary_request_counts"], interval]
        return state
    return mutate(owner, session_id, ratio, apply, child_id=child_id)


def record_compaction(owner, session_id, ratio, debt_tokens: float, repayment_tokens: float, *, child_id: Optional[str] = None) -> dict:
    def apply(state):
        state.update(epoch=int(state["epoch"]) + 1, plan=[], pending_progress=[],
                     last_context_tokens=None, positive_context_delta_total=0.0,
                     positive_context_delta_count=0,
                     native_compaction_count=int(state["native_compaction_count"]) + 1,
                     cache_debt_tokens=max(0.0, float(debt_tokens)),
                     cache_debt_repayment_tokens=max(0.0, float(repayment_tokens)))
        return state
    return mutate(owner, session_id, ratio, apply, child_id=child_id)


def record_correction(owner, session_id, ratio, *, child_id: Optional[str] = None) -> dict:
    def apply(state):
        state.update(epoch=int(state["epoch"]) + 1, plan=[], pending_progress=[],
                     last_boundary_request_count=int(state["request_count"]),
                     completed_boundary_request_counts=[], last_context_tokens=None,
                     positive_context_delta_total=0.0, positive_context_delta_count=0,
                     cache_debt_tokens=0.0, cache_debt_repayment_tokens=0.0)
        return state
    return mutate(owner, session_id, ratio, apply, child_id=child_id)
