"""Durable compaction settlement records used by resumed Agent/Goal runs."""

from __future__ import annotations

import uuid
from typing import Optional

from src.database import ChatContextCompaction, ChatRunState, SessionLocal, utcnow_naive


def record(owner: Optional[str], session_id: str, generation: int, *, ledger_hash: str,
           before_tokens: int, after_tokens: int, economics: dict) -> dict:
    db = SessionLocal()
    try:
        run = db.query(ChatRunState).filter(
            ChatRunState.owner == (owner or ""), ChatRunState.session_id == session_id,
            ChatRunState.status == "running",
        ).order_by(ChatRunState.updated_at.desc()).first()
        marker = {
            "kind": "rebuild_plan_after_compaction", "generation": int(generation),
            "mandatory": True,
            "required_tools": ["create_plan", "update_plan", "update_plan_step"],
            "instruction": (
                "Online context compaction finished and the parent task is still active. "
                "Before any other work, re-read the active goal/checkpoint and rebuild a fresh "
                "remaining-work plan by calling create_plan, update_plan, or update_plan_step. "
                "Do not continue execution or merely describe a plan until that tool call succeeds."
            ),
        }
        row = ChatContextCompaction(
            id=uuid.uuid4().hex, owner=owner or "", session_id=session_id,
            run_id=run.run_id if run else None, generation=int(generation),
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


def settle(owner: Optional[str], session_id: str, generation: int) -> bool:
    db = SessionLocal()
    try:
        row = db.query(ChatContextCompaction).filter(
            ChatContextCompaction.owner == (owner or ""),
            ChatContextCompaction.session_id == session_id,
            ChatContextCompaction.generation == int(generation),
        ).order_by(ChatContextCompaction.created_at.desc()).first()
        if not row or row.status != "pending_settlement":
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


def pending(owner: Optional[str], session_id: str) -> Optional[dict]:
    """Return the newest unsettled handshake so a resumed run cannot bypass it."""
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
