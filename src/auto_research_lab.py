"""Opt-in immutable experiment ledger for safe autonomous optimization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
import secrets
from datetime import timedelta
from typing import Optional

from src.database import (
    AutoResearchAuditEvent, AutoResearchCandidate, AutoResearchEnvironment,
    AutoResearchExperiment, AutoResearchLease, AutoResearchRun,
    AutoResearchStageEvent, SessionLocal, utcnow_naive,
)
from src.settings import get_setting
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_OPS = {"gte", "lte"}
_STAGE_ROLES = {
    "trajectory": "explorer", "map": "analyzer", "reduce": "reducer",
    "proposal": "proposer", "implementation": "implementer", "review": "reviewer",
    "train": "validator", "heldout": "heldout_validator",
}
_MAX_STAGE_PAYLOAD = 128 * 1024


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _audit(db, exp: AutoResearchExperiment, lineage_id: str, event_id: Optional[str],
           kind: str, payload: dict) -> None:
    clean = json.loads(json.dumps(payload or {}, ensure_ascii=False, default=str))
    db.add(AutoResearchAuditEvent(
        experiment_id=exp.id, owner=exp.owner, lineage_id=lineage_id,
        event_id=event_id, kind=kind, payload=clean, content_hash=_digest(clean),
    ))


def _queue(db, exp: AutoResearchExperiment, lineage_id: str, stage: str, *,
           iteration: int = 1, candidate_id: Optional[str] = None,
           payload: Optional[dict] = None) -> AutoResearchStageEvent:
    if stage not in _STAGE_ROLES:
        raise ValueError("Invalid auto-research stage")
    public = json.loads(json.dumps(payload or {}, ensure_ascii=False, default=str))
    row = AutoResearchStageEvent(
        id=uuid.uuid4().hex, experiment_id=exp.id, candidate_id=candidate_id,
        owner=exp.owner, lineage_id=lineage_id, stage=stage, iteration=max(1, int(iteration)),
        actor_role=_STAGE_ROLES[stage], status="queued", public_payload=public,
        input_hash=_digest({"stage": stage, "iteration": iteration, "payload": public}),
    )
    db.add(row); db.flush()
    _audit(db, exp, lineage_id, row.id, "stage_queued", {
        "stage": stage, "iteration": row.iteration, "candidate_id": candidate_id,
        "actor_role": row.actor_role, "input_hash": row.input_hash,
    })
    return row


def _public_event(row: AutoResearchStageEvent) -> dict:
    return {"event_id": row.id, "experiment_id": row.experiment_id,
            "candidate_id": row.candidate_id, "lineage_id": row.lineage_id,
            "stage": row.stage, "iteration": row.iteration,
            "actor_role": row.actor_role, "status": row.status,
            "input": dict(row.public_payload or {}), "input_hash": row.input_hash}


def _enabled() -> bool:
    return bool(get_setting("auto_research_lab_enabled", False))


def _freeze_worktree(path: str, baseline_sha: str) -> tuple[str, str]:
    """Resolve an existing Git worktree root and freeze an exact commit."""
    raw = os.path.abspath(str(path or ""))
    if not os.path.isdir(raw) or os.path.islink(raw):
        raise ValueError("Candidate worktree must be an existing non-symlink directory")
    resolved = os.path.realpath(raw)
    common = {"cwd": resolved, "text": True, "capture_output": True, "timeout": 10,
              "check": True}
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], **common).stdout.strip()
    if os.path.realpath(top) != resolved:
        raise ValueError("Candidate worktree must point to the Git root")
    frozen = subprocess.run(
        ["git", "rev-parse", "--verify", f"{baseline_sha}^{{commit}}"], **common,
    ).stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", frozen):
        raise ValueError("Baseline does not resolve to a commit")
    return resolved, frozen


def create(owner: Optional[str], session_id: Optional[str], *, baseline_sha: str,
           candidate_worktree: str, gates: list, objectives: list) -> dict:
    if not _enabled():
        return {"error": "Auto-Research Lab is disabled in settings", "exit_code": 1}
    baseline_sha = str(baseline_sha or "").lower()
    if not _SHA.fullmatch(baseline_sha) or not os.path.isabs(str(candidate_worktree or "")):
        return {"error": "A frozen git SHA and absolute candidate worktree are required", "exit_code": 1}
    try:
        worktree, baseline_sha = _freeze_worktree(candidate_worktree, baseline_sha)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"error": "Candidate worktree must be a Git root containing the frozen baseline", "exit_code": 1}
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


def configure_environment(owner: Optional[str], experiment_id: str, *, split: str,
                          root: str, manifest: dict) -> dict:
    split = str(split or "").lower()
    raw_root = os.path.abspath(str(root or ""))
    resolved = os.path.realpath(raw_root)
    if (split not in {"train", "heldout"} or not os.path.isabs(str(root or ""))
            or not os.path.isdir(raw_root) or os.path.islink(raw_root) or not isinstance(manifest, dict)):
        return {"error": "split, absolute root and manifest object are required", "exit_code": 1}
    encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode()) > _MAX_STAGE_PAYLOAD:
        return {"error": "Environment manifest exceeds 128 KiB", "exit_code": 1}
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        if db.query(AutoResearchEnvironment).filter(
            AutoResearchEnvironment.experiment_id == experiment_id,
            AutoResearchEnvironment.split == split,
        ).first():
            return {"error": "Environment split is already frozen", "exit_code": 1}
        other = db.query(AutoResearchEnvironment).filter(
            AutoResearchEnvironment.experiment_id == experiment_id,
            AutoResearchEnvironment.split != split,
        ).first()
        if other and os.path.realpath(other.root) == resolved:
            return {"error": "Train and held-out environments must use distinct roots", "exit_code": 1}
        row = AutoResearchEnvironment(
            id=uuid.uuid4().hex, experiment_id=experiment_id, owner=exp.owner,
            split=split, root=resolved, manifest_hash=hashlib.sha256(encoded.encode()).hexdigest(),
            sealed_manifest=encoded,
        )
        db.add(row); _audit(db, exp, "environment", None, "environment_frozen", {
            "split": split, "environment_id": row.id, "manifest_hash": row.manifest_hash,
        }); db.commit()
        return {"environment_id": row.id, "split": split, "manifest_hash": row.manifest_hash,
                "sealed": split == "heldout", "exit_code": 0}
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
        return {"qualified": not failures, "failed_gate_count": len(failures),
                "baseline_sha": exp.baseline_sha, "candidate_sha": candidate_sha,
                "heldout_feedback_released": False,
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
        heldout_env = db.query(AutoResearchEnvironment).filter(
            AutoResearchEnvironment.experiment_id == experiment_id,
            AutoResearchEnvironment.split == "heldout",
        ).first()
        if not heldout_env:
            return {"error": "A frozen held-out environment is required", "exit_code": 1}
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
                existing = db.query(AutoResearchStageEvent).filter(
                    AutoResearchStageEvent.experiment_id == experiment_id,
                    AutoResearchStageEvent.candidate_id == row.id,
                    AutoResearchStageEvent.stage == "heldout",
                ).first()
                if not existing:
                    prior = db.query(AutoResearchStageEvent).filter(
                        AutoResearchStageEvent.experiment_id == experiment_id,
                        AutoResearchStageEvent.candidate_id == row.id,
                    ).order_by(AutoResearchStageEvent.created_at.desc()).first()
                    _queue(db, exp, prior.lineage_id if prior else uuid.uuid4().hex, "heldout",
                           candidate_id=row.id, payload={
                               "candidate_sha": row.candidate_sha,
                               "frozen_gates_hash": _digest(exp.gates or []),
                               "heldout_feedback_policy": "verdict_only",
                           })
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
            "heldout_metrics_sealed": bool(row.heldout_metrics),
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
        if status != "open":
            leases = db.query(AutoResearchLease).filter(
                AutoResearchLease.experiment_id == experiment_id,
            ).all()
            for lease in leases:
                event = db.query(AutoResearchStageEvent).filter(
                    AutoResearchStageEvent.id == lease.event_id,
                    AutoResearchStageEvent.status == "running",
                ).first()
                if event:
                    event.status = "queued" if status == "paused" else "failed"
                    _audit(db, exp, event.lineage_id, event.id, "lease_revoked", {
                        "reason": status, "worker_id": lease.worker_id,
                    })
                db.delete(lease)
        db.commit()
        return {"experiment_id": experiment_id, "status": status, "exit_code": 0}
    finally:
        db.close()


def start_lineage(owner: Optional[str], experiment_id: str, *, hypothesis: str,
                  trajectory_count: int = 3) -> dict:
    """Start one disposable lineage with independent trajectory workers."""
    if not _enabled():
        return {"error": "Auto-Research Lab is disabled in settings", "exit_code": 1}
    try:
        count = max(2, min(32, int(trajectory_count)))
    except (TypeError, ValueError):
        return {"error": "trajectory_count must be an integer", "exit_code": 1}
    text = str(hypothesis or "").strip()
    if not text:
        return {"error": "A falsifiable hypothesis is required", "exit_code": 1}
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        lineage_id = uuid.uuid4().hex
        events = [_queue(db, exp, lineage_id, "trajectory", iteration=index + 1,
                         payload={"hypothesis": text[:12000], "trajectory_index": index + 1,
                                  "trajectory_count": count, "baseline_sha": exp.baseline_sha})
                  for index in range(count)]
        _audit(db, exp, lineage_id, None, "lineage_started", {
            "hypothesis": text[:12000], "trajectory_count": count,
            "gates_hash": _digest(exp.gates or []), "objectives": list(exp.objectives or []),
        })
        db.commit()
        return {"lineage_id": lineage_id, "status": "running",
                "queued": [_public_event(row) for row in events], "next_action": "claim_work",
                "exit_code": 0}
    finally:
        db.close()


def _recover_expired(db, exp: AutoResearchExperiment) -> None:
    now = utcnow_naive()
    leases = db.query(AutoResearchLease).filter(
        AutoResearchLease.experiment_id == exp.id,
        AutoResearchLease.expires_at < now,
    ).all()
    for lease in leases:
        event = db.query(AutoResearchStageEvent).filter(
            AutoResearchStageEvent.id == lease.event_id,
            AutoResearchStageEvent.status == "running",
        ).first()
        if event:
            event.status = "queued"
            _audit(db, exp, event.lineage_id, event.id, "lease_expired", {
                "worker_id": lease.worker_id, "stage": event.stage,
            })
        db.delete(lease)
    if leases:
        db.commit()


def recover_expired_work(owner: Optional[str], experiment_id: str) -> dict:
    """Requeue expired stage leases so a restarted driver can resume."""
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        before = db.query(AutoResearchLease).filter(
            AutoResearchLease.experiment_id == experiment_id,
            AutoResearchLease.expires_at < utcnow_naive(),
        ).count()
        _recover_expired(db, exp)
        return {"requeued": before, "exit_code": 0}
    finally:
        db.close()


def claim_work(owner: Optional[str], experiment_id: str, *, worker_id: str,
               actor_role: str = "", lease_seconds: int = 900,
               event_id: str = "") -> dict:
    """Atomically lease one role-compatible stage without exceeding concurrency."""
    worker = str(worker_id or "").strip()[:200]
    if not worker:
        return {"error": "worker_id is required", "exit_code": 1}
    try:
        ttl = max(30, min(7200, int(lease_seconds)))
    except (TypeError, ValueError):
        return {"error": "Invalid lease duration", "exit_code": 1}
    configured = get_setting("auto_research_max_parallel", 2)
    try:
        limit = max(1, min(16, int(configured if not isinstance(configured, bool) else 2)))
    except (TypeError, ValueError):
        limit = 2
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        if not exp:
            return {"error": "Open experiment not found", "exit_code": 1}
        _recover_expired(db, exp)
        query = db.query(AutoResearchStageEvent).filter(
            AutoResearchStageEvent.experiment_id == exp.id,
            AutoResearchStageEvent.status == "queued",
        )
        if actor_role:
            query = query.filter(AutoResearchStageEvent.actor_role == str(actor_role))
        if event_id:
            query = query.filter(AutoResearchStageEvent.id == str(event_id))
        candidates_rows = query.order_by(AutoResearchStageEvent.created_at, AutoResearchStageEvent.id).limit(64).all()
        if not candidates_rows:
            return {"work": None, "status": "idle", "exit_code": 0}
        for row in candidates_rows:
            for slot in range(1, limit + 1):
                token = secrets.token_urlsafe(32)
                lease = AutoResearchLease(
                    id=uuid.uuid4().hex, event_id=row.id, experiment_id=exp.id,
                    owner=exp.owner, worker_id=worker, actor_role=row.actor_role, slot=slot,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    expires_at=utcnow_naive() + timedelta(seconds=ttl),
                )
                try:
                    changed = db.execute(update(AutoResearchStageEvent).where(
                        AutoResearchStageEvent.id == row.id,
                        AutoResearchStageEvent.status == "queued",
                    ).values(status="running")).rowcount
                    if changed != 1:
                        db.rollback(); break
                    db.add(lease); db.flush()
                    _audit(db, exp, row.lineage_id, row.id, "stage_claimed", {
                        "stage": row.stage, "worker_id": worker, "actor_role": row.actor_role,
                        "slot": slot, "lease_seconds": ttl,
                    })
                    db.commit()
                    work = _public_event(row)
                    if row.stage in {"train", "heldout"}:
                        environment = db.query(AutoResearchEnvironment).filter(
                            AutoResearchEnvironment.experiment_id == exp.id,
                            AutoResearchEnvironment.split == row.stage,
                        ).first()
                        if environment:
                            work["sealed_assignment"] = {
                                "environment_id": environment.id, "root": environment.root,
                                "manifest_hash": environment.manifest_hash,
                                "manifest": json.loads(environment.sealed_manifest),
                            }
                    return {"work": work, "lease_token": token,
                            "lease_seconds": ttl, "slot": slot, "exit_code": 0}
                except IntegrityError:
                    db.rollback()
                    exp = db.query(AutoResearchExperiment).filter(
                        AutoResearchExperiment.id == experiment_id,
                        AutoResearchExperiment.owner == (owner or ""),
                        AutoResearchExperiment.status == "open",
                    ).first()
                    if not exp:
                        return {"error": "Experiment paused while claiming", "exit_code": 1}
                    continue
        return {"work": None, "status": "busy", "exit_code": 0}
    finally:
        db.close()


def release_work(owner: Optional[str], experiment_id: str, *, event_id: str,
                 lease_token: str, reason: str) -> dict:
    token_hash = hashlib.sha256(str(lease_token or "").encode()).hexdigest()
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
        ).first()
        lease = db.query(AutoResearchLease).filter(
            AutoResearchLease.event_id == event_id,
            AutoResearchLease.token_hash == token_hash,
        ).first()
        event = db.query(AutoResearchStageEvent).filter(
            AutoResearchStageEvent.id == event_id,
            AutoResearchStageEvent.experiment_id == experiment_id,
            AutoResearchStageEvent.status == "running",
        ).first()
        if not exp or not lease or not event:
            return {"error": "Active stage lease not found", "exit_code": 1}
        event.status = "queued"
        _audit(db, exp, event.lineage_id, event.id, "lease_released", {
            "reason": str(reason or "worker unavailable")[:1000], "worker_id": lease.worker_id,
        })
        db.delete(lease); db.commit()
        return {"event_id": event.id, "status": "queued", "exit_code": 0}
    finally:
        db.close()


def _maybe_queue_reduce(db, exp: AutoResearchExperiment, lineage_id: str) -> None:
    trajectories = db.query(AutoResearchStageEvent).filter(
        AutoResearchStageEvent.experiment_id == exp.id,
        AutoResearchStageEvent.lineage_id == lineage_id,
        AutoResearchStageEvent.stage == "trajectory",
    ).all()
    maps = db.query(AutoResearchStageEvent).filter(
        AutoResearchStageEvent.experiment_id == exp.id,
        AutoResearchStageEvent.lineage_id == lineage_id,
        AutoResearchStageEvent.stage == "map",
    ).all()
    if trajectories and len(maps) == len(trajectories) and all(row.status == "completed" for row in maps):
        exists = db.query(AutoResearchStageEvent).filter(
            AutoResearchStageEvent.experiment_id == exp.id,
            AutoResearchStageEvent.lineage_id == lineage_id,
            AutoResearchStageEvent.stage == "reduce",
        ).first()
        if not exists:
            _queue(db, exp, lineage_id, "reduce", payload={
                "map_event_ids": [row.id for row in maps],
                "map_output_hashes": [row.output_hash for row in maps],
                "mapped_evidence": [dict(row.public_payload or {}).get("result") for row in maps],
            })


def submit_work(owner: Optional[str], experiment_id: str, *, event_id: str,
                lease_token: str, output: dict, outcome: str = "completed") -> dict:
    """Commit one leased result and deterministically enqueue the next stage."""
    if outcome not in {"completed", "failed"} or not isinstance(output, dict):
        return {"error": "Invalid stage result", "exit_code": 1}
    encoded = json.dumps(output, sort_keys=True, ensure_ascii=False, default=str).encode()
    if len(encoded) > _MAX_STAGE_PAYLOAD:
        return {"error": "Stage result exceeds 128 KiB", "exit_code": 1}
    token_hash = hashlib.sha256(str(lease_token or "").encode()).hexdigest()
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
            AutoResearchExperiment.status == "open",
        ).first()
        row = db.query(AutoResearchStageEvent).filter(
            AutoResearchStageEvent.id == event_id,
            AutoResearchStageEvent.experiment_id == experiment_id,
            AutoResearchStageEvent.owner == (owner or ""),
            AutoResearchStageEvent.status == "running",
        ).first()
        lease = db.query(AutoResearchLease).filter(
            AutoResearchLease.event_id == event_id,
            AutoResearchLease.token_hash == token_hash,
            AutoResearchLease.expires_at >= utcnow_naive(),
        ).first()
        if not exp or not row or not lease:
            return {"error": "Active stage lease not found", "exit_code": 1}
        row.status = outcome
        row.completed_at = utcnow_naive()
        row.output_hash = hashlib.sha256(encoded).hexdigest()
        if row.stage == "heldout":
            row.sealed_payload = encoded.decode()
            row.public_payload = {**dict(row.public_payload or {}), "sealed": True}
        else:
            row.public_payload = {**dict(row.public_payload or {}), "result": output}
        _audit(db, exp, row.lineage_id, row.id, "stage_finished", {
            "stage": row.stage, "outcome": outcome, "output_hash": row.output_hash,
            "sealed": row.stage == "heldout", "worker_id": lease.worker_id,
        })
        db.delete(lease)
        if outcome == "completed":
            if row.stage == "trajectory":
                _queue(db, exp, row.lineage_id, "map", iteration=row.iteration,
                       payload={"trajectory_event_id": row.id, "trajectory_hash": row.output_hash,
                                "trajectory": output})
            elif row.stage == "map":
                _maybe_queue_reduce(db, exp, row.lineage_id)
            elif row.stage == "reduce":
                _queue(db, exp, row.lineage_id, "proposal",
                       payload={"reduced_evidence_hash": row.output_hash,
                                "reduced_evidence": output})
            elif row.stage == "proposal":
                configured_limit = get_setting("auto_research_max_candidates", 24)
                try:
                    candidate_limit = max(1, min(256, int(
                        24 if isinstance(configured_limit, bool) else configured_limit or 24
                    )))
                except (TypeError, ValueError):
                    candidate_limit = 24
                candidate_count = db.query(AutoResearchCandidate).filter(
                    AutoResearchCandidate.experiment_id == exp.id,
                ).count()
                if candidate_count >= candidate_limit:
                    db.rollback(); return {"error": "Candidate budget exhausted", "exit_code": 1}
                candidate_sha = str(output.get("candidate_sha") or "").lower()
                parent_sha = str(output.get("parent_sha") or exp.baseline_sha).lower()
                hypothesis = str(output.get("hypothesis") or "").strip()
                if not candidate_sha:
                    # A proposer does not mutate the worktree, so it normally
                    # cannot know the future commit.  Use a unique immutable
                    # proposal identity until the implementer returns a real
                    # commit from the frozen worktree.
                    candidate_sha = hashlib.sha256(f"proposal:{row.id}:{row.output_hash}".encode()).hexdigest()
                if (not _SHA.fullmatch(candidate_sha) or parent_sha != exp.baseline_sha
                        or not hypothesis):
                    db.rollback(); return {"error": "Proposal must reference the frozen parent and include a hypothesis", "exit_code": 1}
                candidate = AutoResearchCandidate(
                    id=uuid.uuid4().hex, experiment_id=exp.id, owner=exp.owner,
                    ordinal=candidate_count + 1,
                    candidate_sha=candidate_sha, parent_sha=parent_sha, hypothesis=hypothesis[:12000],
                    patch_ref=str(output.get("patch_ref") or "")[:4096], status="proposed",
                )
                db.add(candidate); db.flush(); row.candidate_id = candidate.id
                _queue(db, exp, row.lineage_id, "implementation", candidate_id=candidate.id,
                       payload={"proposal_event_id": row.id, "candidate_sha": candidate_sha,
                                "candidate_worktree": exp.candidate_worktree,
                                "proposal": output})
            elif row.stage == "implementation":
                candidate = db.query(AutoResearchCandidate).filter(
                    AutoResearchCandidate.id == row.candidate_id,
                ).first()
                implemented_sha = str(output.get("candidate_sha") or "").lower()
                if not candidate or not _SHA.fullmatch(implemented_sha):
                    db.rollback(); return {"error": "Implementation must return its committed candidate_sha", "exit_code": 1}
                try:
                    _root, implemented_sha = _freeze_worktree(exp.candidate_worktree, implemented_sha)
                except (OSError, subprocess.SubprocessError, ValueError):
                    db.rollback(); return {"error": "Implemented candidate_sha is not a commit in the frozen worktree", "exit_code": 1}
                candidate.candidate_sha = implemented_sha
                candidate.patch_ref = str(output.get("patch_ref") or candidate.patch_ref or "")[:4096]
                _queue(db, exp, row.lineage_id, "review", iteration=row.iteration,
                       candidate_id=row.candidate_id,
                       payload={"implementation_event_id": row.id, "implementation_hash": row.output_hash,
                                "implementation": output})
            elif row.stage == "review":
                verdict = str(output.get("verdict") or "").lower()
                if verdict == "accept":
                    _queue(db, exp, row.lineage_id, "train", candidate_id=row.candidate_id,
                           payload={"review_event_id": row.id, "review_hash": row.output_hash,
                                    "review": output, "candidate_worktree": exp.candidate_worktree})
                elif verdict == "revise" and row.iteration < 5:
                    _queue(db, exp, row.lineage_id, "implementation", iteration=row.iteration + 1,
                           candidate_id=row.candidate_id,
                           payload={"review_event_id": row.id, "review_hash": row.output_hash,
                                    "review": output, "candidate_worktree": exp.candidate_worktree})
                else:
                    candidate = db.query(AutoResearchCandidate).filter(AutoResearchCandidate.id == row.candidate_id).first()
                    if candidate: candidate.status = "rejected"
            elif row.stage == "train":
                metrics = output.get("metrics")
                if not isinstance(metrics, dict) or not all(isinstance(v, (int, float)) for v in metrics.values()):
                    db.rollback(); return {"error": "Train result requires numeric metrics", "exit_code": 1}
                candidate = db.query(AutoResearchCandidate).filter(AutoResearchCandidate.id == row.candidate_id).first()
                candidate.train_metrics = {str(k): float(v) for k, v in metrics.items()}; candidate.status = "trained"
                db.add(AutoResearchRun(id=uuid.uuid4().hex, experiment_id=exp.id, owner=exp.owner,
                    candidate_sha=candidate.candidate_sha, split="train", metrics=candidate.train_metrics,
                    evidence=[str(x) for x in output.get("evidence") or []], content_hash=row.output_hash))
            elif row.stage == "heldout":
                metrics = output.get("metrics")
                if not isinstance(metrics, dict) or not all(isinstance(v, (int, float)) for v in metrics.values()):
                    db.rollback(); return {"error": "Held-out result requires numeric metrics", "exit_code": 1}
                candidate = db.query(AutoResearchCandidate).filter(AutoResearchCandidate.id == row.candidate_id).first()
                candidate.heldout_metrics = {str(k): float(v) for k, v in metrics.items()}
                failures = [gate["metric"] for gate in exp.gates or [] if not (
                    isinstance(candidate.heldout_metrics.get(gate["metric"]), (int, float)) and
                    (candidate.heldout_metrics[gate["metric"]] >= gate["threshold"] if gate["op"] == "gte"
                     else candidate.heldout_metrics[gate["metric"]] <= gate["threshold"])
                )]
                candidate.status = "rejected" if failures else "qualified"
                db.add(AutoResearchRun(id=uuid.uuid4().hex, experiment_id=exp.id, owner=exp.owner,
                    candidate_sha=candidate.candidate_sha, split="heldout", metrics=candidate.heldout_metrics,
                    evidence=[], content_hash=row.output_hash))
                _audit(db, exp, row.lineage_id, row.id, "heldout_verdict", {
                    "candidate_id": candidate.id, "qualified": not failures,
                    "failed_gate_count": len(failures), "feedback_released": False,
                })
        db.commit()
        return {"event_id": row.id, "stage": row.stage, "status": row.status,
                "output_hash": row.output_hash, "sealed": row.stage == "heldout",
                "next_action": "claim_work", "exit_code": 0}
    except IntegrityError:
        db.rollback()
        return {"error": "Concurrent or duplicate workflow transition", "exit_code": 1}
    finally:
        db.close()


def workflow_snapshot(owner: Optional[str], experiment_id: str, *, cursor: int = 0) -> dict:
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter(
            AutoResearchExperiment.id == experiment_id,
            AutoResearchExperiment.owner == (owner or ""),
        ).first()
        if not exp:
            return {"error": "Experiment not found", "exit_code": 1}
        events = db.query(AutoResearchStageEvent).filter(
            AutoResearchStageEvent.experiment_id == experiment_id,
        ).order_by(AutoResearchStageEvent.created_at, AutoResearchStageEvent.id).all()
        audit = db.query(AutoResearchAuditEvent).filter(
            AutoResearchAuditEvent.experiment_id == experiment_id,
            AutoResearchAuditEvent.owner == (owner or ""),
            AutoResearchAuditEvent.id > max(0, int(cursor)),
        ).order_by(AutoResearchAuditEvent.id).limit(500).all()
        return {"experiment_id": experiment_id, "status": exp.status,
                "stages": [_public_event(row) for row in events],
                "audit": [{"cursor": row.id, "lineage_id": row.lineage_id,
                           "event_id": row.event_id, "kind": row.kind,
                           "payload": row.payload, "content_hash": row.content_hash}
                          for row in audit],
                "next_cursor": audit[-1].id if audit else max(0, int(cursor)), "exit_code": 0}
    finally:
        db.close()
