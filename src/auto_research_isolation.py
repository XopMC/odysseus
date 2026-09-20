"""Hard-isolated held-out validation for Auto-Research.

The held-out manifest is read only by this server-side adapter.  It is never
placed in an LLM/subagent prompt, and only numeric metrics cross the boundary.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Optional

from src import engineering_hosts
from src.database import (
    AutoResearchCandidate, AutoResearchEnvironment, AutoResearchExperiment,
    AutoResearchLease, AutoResearchStageEvent, SessionLocal, utcnow_naive,
)


async def _rpc(host_call, host_id: str, op: str, args: dict, owner: Optional[str], scope: str) -> dict:
    response = await host_call(host_id, op, args, owner or "", scope)
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise RuntimeError("Isolated runner operation failed")
    return response["result"]


async def validate_git(owner: Optional[str], host_id: str, worktree: str, expected_sha: str,
                       *, scope: str, host_call=engineering_hosts.call) -> dict:
    state = await _rpc(host_call, host_id, "workspace.git-state", {"cwd": worktree}, owner, scope)
    if state.get("head") != str(expected_sha or "").lower() or state.get("clean") is not True:
        raise RuntimeError("Candidate worktree must be clean and checked out at the exact commit")
    return state


async def validate_environment(owner: Optional[str], host_id: str, root: str, *, scope: str,
                               host_call=engineering_hosts.call) -> str:
    digest = await _rpc(host_call, host_id, "workspace.digest", {"cwd": root}, owner, scope)
    value = digest.get("sha256")
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError("Environment digest unavailable")
    return value


async def validate_implementation(owner: Optional[str], experiment_id: str, *, event_id: str,
                                  lease_token: str, candidate_sha: str,
                                  host_call=engineering_hosts.call) -> str:
    state = _load(owner, experiment_id, event_id, lease_token, expected_stage="implementation")
    await validate_git(owner, state["runner_host_id"], state["worktree"], candidate_sha,
                       scope=f"auto-research:{experiment_id}", host_call=host_call)
    return str(candidate_sha).lower()


def experiment_host(owner: Optional[str], experiment_id: str) -> str:
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter_by(
            id=experiment_id, owner=owner or "", status="open",
        ).first()
        if not exp or not exp.runner_host_id:
            raise RuntimeError("Experiment execution host is unavailable")
        return exp.runner_host_id
    finally:
        db.close()


def leased_stage(owner: Optional[str], experiment_id: str, event_id: str, lease_token: str) -> str:
    token_hash = hashlib.sha256(str(lease_token or "").encode()).hexdigest()
    db = SessionLocal()
    try:
        event = db.query(AutoResearchStageEvent).filter_by(
            id=event_id, experiment_id=experiment_id, owner=owner or "", status="running",
        ).first()
        lease = db.query(AutoResearchLease).filter(
            AutoResearchLease.event_id == event_id,
            AutoResearchLease.token_hash == token_hash,
            AutoResearchLease.expires_at >= utcnow_naive(),
        ).first()
        if not event or not lease:
            raise RuntimeError("Active stage lease is unavailable")
        return event.stage
    finally:
        db.close()


def _load(owner: Optional[str], experiment_id: str, event_id: str, lease_token: str,
          expected_stage: str = "heldout") -> dict:
    token_hash = hashlib.sha256(str(lease_token or "").encode()).hexdigest()
    db = SessionLocal()
    try:
        exp = db.query(AutoResearchExperiment).filter_by(
            id=experiment_id, owner=owner or "", status="open",
        ).first()
        event = db.query(AutoResearchStageEvent).filter_by(
            id=event_id, experiment_id=experiment_id, owner=owner or "",
            stage=expected_stage, status="running",
        ).first()
        lease = db.query(AutoResearchLease).filter(
            AutoResearchLease.event_id == event_id,
            AutoResearchLease.token_hash == token_hash,
            AutoResearchLease.expires_at >= utcnow_naive(),
        ).first()
        environment = db.query(AutoResearchEnvironment).filter_by(
            experiment_id=experiment_id, split="heldout",
        ).first() if expected_stage == "heldout" else None
        candidate = db.query(AutoResearchCandidate).filter_by(
            id=event.candidate_id if event else "", experiment_id=experiment_id,
        ).first()
        if not exp or not event or not lease or not candidate or (expected_stage == "heldout" and not environment):
            raise RuntimeError("Active held-out lease is unavailable")
        manifest = json.loads(environment.sealed_manifest) if environment else {}
        if environment and not isinstance(manifest, dict):
            raise RuntimeError("Invalid held-out manifest")
        return {
            "worktree": exp.candidate_worktree, "candidate_sha": candidate.candidate_sha,
            "runner_host_id": exp.runner_host_id or manifest.get("runner_host_id"),
            "environment": environment.root if environment else None,
            "environment_hash": manifest.get("_content_sha256"), "manifest": manifest,
        }
    finally:
        db.close()


async def run_heldout(owner: Optional[str], experiment_id: str, *, event_id: str,
                      lease_token: str, host_call=engineering_hosts.call) -> dict:
    """Run one leased held-out stage in a fixed no-network container."""
    state = _load(owner, experiment_id, event_id, lease_token)
    manifest = state["manifest"]
    host_id = state["runner_host_id"]
    command = manifest["command"]
    timeout = manifest.get("timeout_seconds", 900)
    scope = f"auto-research:{experiment_id}"

    capabilities = await _rpc(host_call, host_id, "runner.capabilities", {}, owner, scope)
    required = {"workspace.digest", "workspace.git-state", "workspace.verification-copy", "sandbox.command.start"}
    if not required.issubset(set(capabilities.get("supported_ops") or [])):
        raise RuntimeError("Execution host lacks hard-isolation capabilities")

    await validate_git(owner, host_id, state["worktree"], state["candidate_sha"],
                       scope=scope, host_call=host_call)
    candidate_digest = await _rpc(host_call, host_id, "workspace.digest", {
        "cwd": state["worktree"],
    }, owner, scope)
    environment_digest = await _rpc(host_call, host_id, "workspace.digest", {
        "cwd": state["environment"],
    }, owner, scope)
    # The stored manifest hash protects the manifest itself; the runner digest
    # below freezes the actual held-out bytes immediately before dispatch.
    environment_hash = environment_digest.get("sha256")
    if not isinstance(environment_hash, str) or environment_hash != state["environment_hash"]:
        raise RuntimeError("Held-out environment changed after it was frozen")
    copied = await _rpc(host_call, host_id, "workspace.verification-copy", {
        "source": state["worktree"],
        "expected_source_sha256": candidate_digest["sha256"],
        "idempotency_key": f"auto-research-copy:{event_id}",
    }, owner, scope)
    job = await _rpc(host_call, host_id, "sandbox.command.start", {
        "cwd": copied["path"], "command": command, "timeout": timeout,
        "idempotency_key": f"auto-research-heldout:{event_id}",
        "expected_workspace_hash": candidate_digest["sha256"],
        "check_run_id": event_id,
        "sealed_environment": state["environment"],
        "expected_environment_hash": environment_hash,
    }, owner, scope)

    offset, chunks = 0, []
    while True:
        polled = await _rpc(host_call, host_id, "terminal.poll", {
            "id": job["id"], "offset": offset, "limit": 60000,
        }, owner, scope)
        evidence = polled.get("check_evidence") or {}
        if (polled.get("id") != job["id"] or evidence.get("run_id") != event_id
                or evidence.get("workspace_hash") != candidate_digest["sha256"]
                or evidence.get("environment_hash") != environment_hash
                or evidence.get("command_hash") != hashlib.sha256(command.encode()).hexdigest()):
            raise RuntimeError("Held-out runner evidence mismatch")
        encoded = polled.get("output_base64") or ""
        if encoded:
            chunks.append(base64.b64decode(encoded, validate=True))
            if sum(map(len, chunks)) > 256 * 1024:
                raise RuntimeError("Held-out output exceeds 256 KiB")
        offset = int(polled.get("next_offset") or offset)
        if polled.get("status") not in {"running", "starting"}:
            if (polled.get("exit_code") != 0
                    or evidence.get("workspace_hash_after") != candidate_digest["sha256"]
                    or evidence.get("environment_hash_after") != environment_hash):
                raise RuntimeError("Held-out validation failed or mutated its candidate")
            break
        await asyncio.sleep(.25)

    text = b"".join(chunks).decode("utf-8", "strict").strip()
    try:
        payload = json.loads(text.splitlines()[-1])
    except (IndexError, ValueError, UnicodeError):
        raise RuntimeError("Held-out runner did not return a JSON verdict") from None
    metrics = payload.get("metrics") if isinstance(payload, dict) else None
    if (not isinstance(metrics, dict) or not metrics
            or not all(isinstance(key, str) and type(value) in {int, float} for key, value in metrics.items())):
        raise RuntimeError("Held-out verdict requires numeric metrics")
    return {"metrics": metrics, "evidence": [], "isolation": {
        "hard": True, "network": "none", "candidate_hash": candidate_digest["sha256"],
        "environment_hash": environment_hash,
    }}
