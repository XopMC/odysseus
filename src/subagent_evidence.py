"""Owner-scoped append-only evidence board for parallel child agents."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Optional

from src.database import (
    ChatSubagentCandidate, ChatSubagentEvidence, ChatSubagentRun,
    ChatSubagentVerification, SessionLocal,
)

KINDS = {"finding", "reproduction", "rejected", "verified"}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _child(db, owner, session_id, child_id):
    return db.query(ChatSubagentRun).filter(
        ChatSubagentRun.owner == (owner or ""),
        ChatSubagentRun.parent_session_id == session_id,
        ChatSubagentRun.id == child_id,
        ChatSubagentRun.removed.is_(False),
    ).first()


def publish(owner: Optional[str], session_id: str, child_id: str, *, kind: str,
            body: str, artifact_refs=None) -> dict:
    kind = str(kind or "").strip().lower()
    body = str(body or "").strip()
    refs = [str(x) for x in (artifact_refs or []) if str(x)][:32]
    if kind not in KINDS or not body or len(body) > 40_000:
        return {"error": "Invalid evidence kind/body", "exit_code": 1}
    digest = _hash({"kind": kind, "body": body, "artifact_refs": refs})
    db = SessionLocal()
    try:
        if not _child(db, owner, session_id, child_id):
            return {"error": "Subagent not found", "exit_code": 1}
        existing = db.query(ChatSubagentEvidence).filter(
            ChatSubagentEvidence.owner == (owner or ""),
            ChatSubagentEvidence.parent_session_id == session_id,
            ChatSubagentEvidence.child_id == child_id,
            ChatSubagentEvidence.content_hash == digest,
        ).first()
        if existing:
            return {"evidence_id": existing.id, "content_hash": digest, "duplicate": True, "exit_code": 0}
        row = ChatSubagentEvidence(
            id=uuid.uuid4().hex, owner=owner or "", parent_session_id=session_id,
            child_id=child_id, kind=kind, body=body, artifact_refs=refs,
            content_hash=digest,
        )
        db.add(row); db.commit()
        return {"evidence_id": row.id, "content_hash": digest, "exit_code": 0}
    finally:
        db.close()


def list_evidence(owner: Optional[str], session_id: str, *, child_id: str = "") -> dict:
    db = SessionLocal()
    try:
        query = db.query(ChatSubagentEvidence).filter(
            ChatSubagentEvidence.owner == (owner or ""),
            ChatSubagentEvidence.parent_session_id == session_id,
        )
        if child_id:
            query = query.filter(ChatSubagentEvidence.child_id == child_id)
        rows = query.order_by(ChatSubagentEvidence.created_at.asc()).limit(1000).all()
        return {"evidence": [{
            "evidence_id": row.id, "child_id": row.child_id, "kind": row.kind,
            "body": row.body, "artifact_refs": row.artifact_refs or [],
            "content_hash": row.content_hash,
        } for row in rows], "exit_code": 0}
    finally:
        db.close()


def submit_candidate(owner: Optional[str], session_id: str, child_id: str, *,
                     title: str, payload: dict, evidence_ids: list[str]) -> dict:
    title = str(title or "").strip()
    if not title or len(title) > 500 or not isinstance(payload, dict) or not evidence_ids:
        return {"error": "Candidate requires title, payload and evidence_ids", "exit_code": 1}
    ids = list(dict.fromkeys(str(x) for x in evidence_ids if str(x)))
    db = SessionLocal()
    try:
        if not _child(db, owner, session_id, child_id):
            return {"error": "Subagent not found", "exit_code": 1}
        owned = db.query(ChatSubagentEvidence.id).filter(
            ChatSubagentEvidence.owner == (owner or ""),
            ChatSubagentEvidence.parent_session_id == session_id,
            ChatSubagentEvidence.id.in_(ids),
        ).all()
        if {x[0] for x in owned} != set(ids):
            return {"error": "Candidate references unavailable evidence", "exit_code": 1}
        digest = _hash({"title": title, "payload": payload, "evidence_ids": ids})
        existing = db.query(ChatSubagentCandidate).filter(
            ChatSubagentCandidate.owner == (owner or ""),
            ChatSubagentCandidate.parent_session_id == session_id,
            ChatSubagentCandidate.content_hash == digest,
        ).first()
        if existing:
            return {"candidate_id": existing.id, "status": existing.status,
                    "content_hash": digest, "duplicate": True, "exit_code": 0}
        row = ChatSubagentCandidate(
            id=uuid.uuid4().hex, owner=owner or "", parent_session_id=session_id,
            submitted_by_child_id=child_id, title=title, payload=payload,
            evidence_ids=ids, content_hash=digest, status="proposed",
        )
        db.add(row); db.commit()
        return {"candidate_id": row.id, "status": row.status,
                "content_hash": digest, "exit_code": 0}
    finally:
        db.close()


def verify_candidate(owner: Optional[str], session_id: str, candidate_id: str,
                     verifier_child_id: str, *, verdict: str, notes: str = "") -> dict:
    verdict = str(verdict or "").strip().lower()
    notes = str(notes or "").strip()
    if verdict not in {"accepted", "rejected"} or len(notes) > 20_000:
        return {"error": "Verdict must be accepted or rejected", "exit_code": 1}
    db = SessionLocal()
    try:
        candidate = db.query(ChatSubagentCandidate).filter(
            ChatSubagentCandidate.owner == (owner or ""),
            ChatSubagentCandidate.parent_session_id == session_id,
            ChatSubagentCandidate.id == candidate_id,
        ).first()
        verifier = _child(db, owner, session_id, verifier_child_id)
        if not candidate or not verifier:
            return {"error": "Candidate or verifier not found", "exit_code": 1}
        if candidate.submitted_by_child_id == verifier_child_id:
            return {"error": "Candidate requires independent verification", "exit_code": 1}
        digest = _hash({"candidate_id": candidate_id, "verifier": verifier_child_id,
                        "verdict": verdict, "notes": notes})
        row = ChatSubagentVerification(
            id=uuid.uuid4().hex, owner=owner or "", parent_session_id=session_id,
            candidate_id=candidate_id, verifier_child_id=verifier_child_id,
            verdict=verdict, notes=notes, content_hash=digest,
        )
        db.add(row)
        candidate.status = verdict
        db.commit()
        return {"candidate_id": candidate_id, "status": verdict,
                "verification_hash": digest, "exit_code": 0}
    except Exception as exc:
        db.rollback()
        return {"error": f"Verification could not be recorded: {type(exc).__name__}", "exit_code": 1}
    finally:
        db.close()


def list_candidates(owner: Optional[str], session_id: str) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(ChatSubagentCandidate).filter(
            ChatSubagentCandidate.owner == (owner or ""),
            ChatSubagentCandidate.parent_session_id == session_id,
        ).order_by(ChatSubagentCandidate.created_at.asc()).limit(1000).all()
        return {"candidates": [{
            "candidate_id": row.id, "submitted_by_child_id": row.submitted_by_child_id,
            "title": row.title, "payload": row.payload, "evidence_ids": row.evidence_ids,
            "content_hash": row.content_hash, "status": row.status,
        } for row in rows], "exit_code": 0}
    finally:
        db.close()
