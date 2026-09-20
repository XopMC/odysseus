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
            "instruction": "Re-read the active plan and goal checkpoint, rebuild current step state, then continue.",
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
        row.status = "settled"
        row.settled_at = utcnow_naive()
        if row.run_id:
            run = db.query(ChatRunState).filter(ChatRunState.run_id == row.run_id).first()
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
