"""Opt-in immutable experiment ledger for safe autonomous optimization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Optional

from src.database import AutoResearchExperiment, AutoResearchRun, SessionLocal
from src.settings import get_setting

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_OPS = {"gte", "lte"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _enabled() -> bool:
    return bool(get_setting("auto_research_lab_enabled", False))


def create(owner: Optional[str], session_id: Optional[str], *, baseline_sha: str,
           candidate_worktree: str, gates: list, objectives: list) -> dict:
    if not _enabled():
        return {"error": "Auto-Research Lab is disabled in settings", "exit_code": 1}
    baseline_sha = str(baseline_sha or "").lower()
    worktree = os.path.realpath(str(candidate_worktree or ""))
    if not _SHA.fullmatch(baseline_sha) or not os.path.isabs(worktree):
        return {"error": "A frozen git SHA and absolute candidate worktree are required", "exit_code": 1}
    if not isinstance(gates, list) or not gates or not isinstance(objectives, list):
        return {"error": "At least one fixed gate and objective are required", "exit_code": 1}
    normalized = []
    for gate in gates:
        if (not isinstance(gate, dict) or set(gate) != {"metric", "op", "threshold"}
                or gate["op"] not in _OPS or not isinstance(gate["threshold"], (int, float))):
            return {"error": "Invalid fixed gate", "exit_code": 1}
        normalized.append({"metric": str(gate["metric"]), "op": gate["op"],
                           "threshold": float(gate["threshold"])})
    db = SessionLocal()
    try:
        row = AutoResearchExperiment(
            id=uuid.uuid4().hex, owner=owner or "", session_id=session_id,
            baseline_sha=baseline_sha, candidate_worktree=worktree,
            gates=normalized, objectives=[str(x) for x in objectives if str(x)], status="open",
        )
        db.add(row); db.commit()
        return {"experiment_id": row.id, "baseline_sha": baseline_sha,
                "gates_hash": _digest(normalized), "status": "open", "exit_code": 0}
    finally:
        db.close()


def record(owner: Optional[str], experiment_id: str, *, candidate_sha: str,
           split: str, metrics: dict, evidence: list) -> dict:
    if not _enabled():
        return {"error": "Auto-Research Lab is disabled in settings", "exit_code": 1}
    candidate_sha = str(candidate_sha or "").lower()
    split = str(split or "").lower()
    if not _SHA.fullmatch(candidate_sha) or split not in {"train", "heldout"} or not isinstance(metrics, dict):
        return {"error": "Invalid candidate measurement", "exit_code": 1}
    values = {}
    for key, value in metrics.items():
        if not isinstance(value, (int, float)):
            return {"error": "Metrics must be numeric", "exit_code": 1}
        values[str(key)] = float(value)
    payload = {"candidate_sha": candidate_sha, "split": split, "metrics": values,
               "evidence": [str(x) for x in (evidence or [])]}
    digest = _digest(payload)
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        row = AutoResearchRun(
            id=uuid.uuid4().hex, experiment_id=experiment_id, owner=owner or "",
            candidate_sha=candidate_sha, split=split, metrics=values,
            evidence=payload["evidence"], content_hash=digest,
        )
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
            return {"error": "Identical immutable measurement already exists", "exit_code": 1}
        return {"run_id": row.id, "content_hash": digest, "exit_code": 0}
    finally:
        db.close()


def evaluate(owner: Optional[str], experiment_id: str, candidate_sha: str) -> dict:
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
        ).first()
        if not exp:
            return {"error": "Experiment not found", "exit_code": 1}
        rows = db.query(AutoResearchRun).filter(
            AutoResearchRun.experiment_id == experiment_id,
            AutoResearchRun.owner == (owner or ""),
            AutoResearchRun.candidate_sha == candidate_sha,
        ).all()
        by_split = {row.split: row for row in rows}
        if set(by_split) != {"train", "heldout"}:
            return {"qualified": False, "reason": "train_and_heldout_required", "exit_code": 0}
        failures = []
        heldout = by_split["heldout"].metrics or {}
        for gate in exp.gates or []:
            value = heldout.get(gate["metric"])
            ok = isinstance(value, (int, float)) and (
                value >= gate["threshold"] if gate["op"] == "gte" else value <= gate["threshold"]
            )
            if not ok:
                failures.append(gate["metric"])
        return {"qualified": not failures, "failed_gates": failures,
                "baseline_sha": exp.baseline_sha, "candidate_sha": candidate_sha,
                "deployment_authorized": False, "exit_code": 0}
    finally:
        db.close()


def list_experiments(owner: Optional[str]) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.owner == (owner or "")
        ).order_by(AutoResearchExperiment.created_at.desc()).limit(100).all()
        return {"experiments": [{"experiment_id": row.id, "baseline_sha": row.baseline_sha,
                                 "candidate_worktree": row.candidate_worktree,
                                 "gates": row.gates, "objectives": row.objectives,
                                 "status": row.status} for row in rows], "exit_code": 0}
    finally:
        db.close()
