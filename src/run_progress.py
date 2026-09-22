"""Content-free progress evidence for long-running agent health checks."""

import hashlib
import json
import time
from typing import Optional


STALL_SECONDS = 600.0
_MAX_MARKERS = 512
_VERIFICATION_TOOLS = frozenset({
    "run_tests", "run_lint", "verify_hashes", "compare_files",
})


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def progress_marker(payload: dict) -> Optional[tuple[str, str]]:
    """Return proof identity only for a concrete state/evidence change.

    Streaming tokens, round markers, transport heartbeats, and generic tool
    success deliberately do not qualify. The digest avoids retaining content.
    """
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "plan_update":
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("id") or type(data.get("revision")) is not int:
            return None
        steps = data.get("steps")
        if not isinstance(steps, list) or not any(
            isinstance(step, dict) and step.get("status") in {"in_progress", "done"}
            for step in steps
        ):
            return None
        step_states = [
            (step.get("step_id"), step.get("status"))
            for step in steps if isinstance(step, dict)
        ]
        return "step", _digest((data["id"], data.get("current_step_id"), step_states))
    if kind == "goal_update":
        data = payload.get("data")
        if (
            isinstance(data, dict) and data.get("id")
            and data.get("status") in {"active", "completed"}
            and isinstance(data.get("progress"), str) and data["progress"].strip()
            and isinstance(data.get("checkpoint"), dict) and data["checkpoint"]
        ):
            return "evidence", _digest((data["id"], data["progress"], data["checkpoint"]))
    if kind == "tool_output" and type(payload.get("exit_code")) is int:
        if payload.get("tool") in _VERIFICATION_TOOLS and payload.get("output"):
            return "verification", _digest((payload["tool"], payload["exit_code"], payload["output"]))
        if payload["exit_code"] != 0:
            return None
        diff = payload.get("diff")
        added = diff.get("added") if isinstance(diff, dict) else None
        removed = diff.get("removed") if isinstance(diff, dict) else None
        if isinstance(diff, dict) and diff.get("text") and (
            (type(added) is int and added > 0) or (type(removed) is int and removed > 0)
        ):
            return "diff", _digest(diff)
        if payload.get("artifact_id"):
            return "artifact", _digest(payload["artifact_id"])
    if kind == "generated_image" and payload.get("image_id"):
        return "artifact", _digest(payload["image_id"])
    if kind == "doc_update" and payload.get("doc_id") and payload.get("version") is not None:
        return "artifact", _digest((payload["doc_id"], payload["version"]))
    return None


class ProgressTracker:
    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self.last_activity_at: Optional[float] = None
        self.last_heartbeat_at: Optional[float] = None
        self.last_progress_at: Optional[float] = None
        self.last_progress_kind: Optional[str] = None
        self.revision = 0
        self._seen: set[str] = set()
        self._order: list[str] = []

    def observe(self, payload: dict, *, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        self.last_activity_at = now
        marker = progress_marker(payload)
        if marker is None:
            return False
        kind, digest = marker
        if digest in self._seen:
            return False
        self._seen.add(digest)
        self._order.append(digest)
        if len(self._order) > _MAX_MARKERS:
            self._seen.discard(self._order.pop(0))
        self.revision += 1
        self.last_progress_at = now
        self.last_progress_kind = kind
        return True

    def heartbeat(self, *, now: Optional[float] = None) -> None:
        self.last_heartbeat_at = time.time() if now is None else now

    def snapshot(self, status: str, *, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        elapsed = max(0.0, now - (self.last_progress_at or self.started_at))
        return {
            "revision": self.revision,
            "last_activity_at": self.last_activity_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_progress_at": self.last_progress_at,
            "last_progress_kind": self.last_progress_kind,
            "seconds_without_progress": round(elapsed, 1),
            "stalled": status == "running" and elapsed >= STALL_SECONDS,
        }
