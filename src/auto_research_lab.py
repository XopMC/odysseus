"""Opt-in immutable experiment ledger for safe autonomous optimization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Optional

from src.database import (
    AutoResearchCandidate, AutoResearchExperiment, AutoResearchRun, SessionLocal,
    utcnow_naive,
)
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
        candidate = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.experiment_id == experiment_id,
            AutoResearchCandidate.owner == (owner or ""),
            AutoResearchCandidate.candidate_sha == candidate_sha,
        ).first()
        if candidate is None:
            ordinal = db.query(AutoResearchCandidate).filter(
                AutoResearchCandidate.experiment_id == experiment_id,
            ).count() + 1
            candidate = AutoResearchCandidate(
                id=uuid.uuid4().hex, experiment_id=experiment_id, owner=owner or "",
                ordinal=ordinal, candidate_sha=candidate_sha, parent_sha=exp.baseline_sha,
                hypothesis="legacy recorded candidate", status="proposed",
            )
            db.add(candidate)
            db.flush()
        if split == "heldout" and candidate.status not in {"selected", "heldout_running", "heldout", "qualified", "rejected"}:
            # Compatibility for the original one-candidate ledger: with a
            # single trained candidate the Pareto frontier is unambiguous.
            trained_count = db.query(AutoResearchCandidate).filter(
                AutoResearchCandidate.experiment_id == experiment_id,
                AutoResearchCandidate.status.in_(["trained", "selected"]),
            ).count()
            if candidate.status == "trained" and trained_count == 1:
                candidate.status, candidate.selected_at = "selected", utcnow_naive()
            else:
                return {"error": "Candidate must be selected from train results before held-out evaluation", "exit_code": 1}
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
        candidate.status = "trained" if split == "train" else "heldout"
        if split == "train":
            candidate.train_metrics = values
        else:
            candidate.heldout_metrics = values
        db.commit()
        return {"run_id": row.id, "candidate_id": candidate.id,
                "candidate_status": candidate.status, "content_hash": digest, "exit_code": 0}
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
        candidate = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.experiment_id == experiment_id,
            AutoResearchCandidate.owner == (owner or ""),
            AutoResearchCandidate.candidate_sha == candidate_sha,
        ).first()
        if candidate:
            candidate.status = "rejected" if failures else "qualified"
            db.commit()
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


def propose(owner: Optional[str], experiment_id: str, *, candidate_sha: str,
            parent_sha: str, hypothesis: str, patch_ref: str = "") -> dict:
    """Register a generated candidate without exposing any held-out signal."""
    if not _enabled():
        return {"error": "Auto-Research Lab is disabled in settings", "exit_code": 1}
    candidate_sha, parent_sha = str(candidate_sha or "").lower(), str(parent_sha or "").lower()
    if not _SHA.fullmatch(candidate_sha) or not _SHA.fullmatch(parent_sha) or not str(hypothesis or "").strip():
        return {"error": "Candidate SHA, parent SHA and hypothesis are required", "exit_code": 1}
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        count = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.experiment_id == experiment_id,
        ).count()
        configured_limit = get_setting("auto_research_max_candidates", 24)
        if isinstance(configured_limit, bool):
            configured_limit = 24
        try:
            limit = max(1, min(256, int(configured_limit or 24)))
        except (TypeError, ValueError):
            limit = 24
        if count >= limit:
            return {"error": "Candidate budget exhausted", "exit_code": 1}
        row = AutoResearchCandidate(
            id=uuid.uuid4().hex, experiment_id=experiment_id, owner=owner or "",
            ordinal=count + 1, candidate_sha=candidate_sha, parent_sha=parent_sha,
            hypothesis=str(hypothesis).strip()[:12000], patch_ref=str(patch_ref or "")[:4096],
        )
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
            return {"error": "Candidate already exists", "exit_code": 1}
        return {"candidate_id": row.id, "ordinal": row.ordinal, "status": row.status,
                "next_action": "run_train", "exit_code": 0}
    finally:
        db.close()


def _dominates(left: dict, right: dict, objectives: list[str]) -> bool:
    values = [(left.get(key), right.get(key)) for key in objectives]
    if not values or any(not isinstance(a, (int, float)) or not isinstance(b, (int, float)) for a, b in values):
        return False
    return all(a >= b for a, b in values) and any(a > b for a, b in values)


def select_pareto(owner: Optional[str], experiment_id: str) -> dict:
    """Select the train Pareto frontier; only it may enter held-out testing."""
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        rows = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.experiment_id == experiment_id,
            AutoResearchCandidate.owner == (owner or ""),
            AutoResearchCandidate.status.in_(["trained", "selected"]),
        ).order_by(AutoResearchCandidate.ordinal).all()
        objectives = [str(x) for x in (exp.objectives or [])]
        frontier = [row for row in rows if not any(
            other.id != row.id and _dominates(other.train_metrics or {}, row.train_metrics or {}, objectives)
            for other in rows
        )]
        now = utcnow_naive()
        frontier_ids = {row.id for row in frontier}
        for row in rows:
            if row.id in frontier_ids:
                row.status, row.selected_at = "selected", now
            elif row.status == "selected":
                row.status, row.selected_at = "trained", None
        db.commit()
        return {"selected": [{"candidate_id": row.id, "candidate_sha": row.candidate_sha,
                               "train_metrics": row.train_metrics} for row in frontier],
                "next_action": "run_heldout", "exit_code": 0}
    finally:
        db.close()


def candidates(owner: Optional[str], experiment_id: str) -> dict:
    db = SessionLocal()
    try:
        rows = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.experiment_id == experiment_id,
            AutoResearchCandidate.owner == (owner or ""),
        ).order_by(AutoResearchCandidate.ordinal).all()
        return {"candidates": [{
            "candidate_id": row.id, "ordinal": row.ordinal, "candidate_sha": row.candidate_sha,
            "parent_sha": row.parent_sha, "hypothesis": row.hypothesis, "status": row.status,
            "train_metrics": row.train_metrics,
            # Held-out values are sealed from the generation/selection phase.
            **({"heldout_metrics": row.heldout_metrics} if row.status in {"heldout", "qualified", "rejected"} else {}),
        } for row in rows], "exit_code": 0}
    finally:
        db.close()


def claim(owner: Optional[str], experiment_id: str, candidate_id: str, split: str) -> dict:
    """Lease one bounded train/held-out job; execution remains in its worktree."""
    split = str(split or "").lower()
    if split not in {"train", "heldout"}:
        return {"error": "split must be train or heldout", "exit_code": 1}
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        row = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.id == candidate_id,
            AutoResearchCandidate.experiment_id == experiment_id,
            AutoResearchCandidate.owner == (owner or ""),
        ).first()
        if not exp or not row:
            return {"error": "Open experiment or candidate not found", "exit_code": 1}
        expected = "proposed" if split == "train" else "selected"
        if row.status != expected:
            return {"error": f"Candidate must be {expected} before {split}", "exit_code": 1}
        configured = get_setting("auto_research_max_parallel", 2)
        if isinstance(configured, bool):
            configured = 2
        try:
            limit = max(1, min(16, int(configured or 2)))
        except (TypeError, ValueError):
            limit = 2
        running = db.query(AutoResearchCandidate).filter(
            AutoResearchCandidate.experiment_id == experiment_id,
            AutoResearchCandidate.status.in_(["training", "heldout_running"]),
        ).count()
        if running >= limit:
            return {"error": "Parallel experiment limit reached", "exit_code": 1}
        row.status = "training" if split == "train" else "heldout_running"
        db.commit()
        return {"candidate_id": row.id, "candidate_sha": row.candidate_sha,
                "candidate_worktree": exp.candidate_worktree, "patch_ref": row.patch_ref,
                "hypothesis": row.hypothesis, "split": split, "status": row.status,
                "heldout_metrics_visible": False, "exit_code": 0}
    finally:
        db.close()


def set_status(owner: Optional[str], experiment_id: str, status: str) -> dict:
    status = str(status or "").lower()
    if status not in {"open", "paused", "closed"}:
        return {"error": "Invalid experiment status", "exit_code": 1}
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
        ).first()
        if not exp:
            return {"error": "Experiment not found", "exit_code": 1}
        if exp.status == "closed" and status != "closed":
            return {"error": "Closed experiments cannot be reopened", "exit_code": 1}
        exp.status = status
        db.commit()
        return {"experiment_id": experiment_id, "status": status, "exit_code": 0}
    finally:
        db.close()
