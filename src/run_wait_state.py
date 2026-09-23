"""Redacted server-side phase of one detached Agent/Chat run."""

import time
from typing import Optional
from urllib.parse import urlsplit
import re


CONTEXT_FAILURE_CODES = frozenset({
    "summarizer_timeout", "summarizer_rate_limited", "summarizer_model_unavailable",
    "summarizer_provider_error", "summarizer_request_rejected",
    "summarizer_no_answer", "summarizer_transport_unavailable", "summarizer_error",
    "context_policy_changed", "auto_compact_disabled", "context_no_reduction",
    "context_input_budget_exceeded", "context_uncompactable", "context_policy_error",
    "context_window_unavailable",
})


def _label(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r", " ").replace("\n", " ")[:limit]


def selected_endpoint_host(url: str) -> Optional[str]:
    """Expose only a safe host:port, never URL credentials, path or query."""
    try:
        parts = urlsplit(str(url or ""))
        host = parts.hostname
        port = parts.port
    except (TypeError, ValueError):
        return None
    if parts.scheme not in {"http", "https"} or not host or len(host) > 253:
        return None
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        return None
    display = f"[{host}]" if ":" in host else host
    return f"{display}:{port}" if port is not None else display


class RunWaitTracker:
    def __init__(self, started_at: float) -> None:
        self.phase = "model"
        self.phase_since = started_at
        self.model = ""
        self.endpoint_id = ""
        self.endpoint_label = ""
        self.tool = ""
        self.tool_call_id = ""

    def _phase(self, value: str, now: float) -> None:
        if self.phase != value:
            self.phase = value
            self.phase_since = now
        if value != "tool":
            self.tool = ""
            self.tool_call_id = ""

    def observe(self, payload: dict, *, now: Optional[float] = None) -> None:
        if not isinstance(payload, dict):
            return
        now = time.time() if now is None else now
        kind = payload.get("type")
        if kind == "model_actual":
            self.model = _label(payload.get("model"), 200) or self.model
            actual_id = _label(payload.get("endpoint_id"), 200)
            actual_label = _label(payload.get("endpoint_label"), 120)
            if actual_id and actual_id != self.endpoint_id:
                self.endpoint_id = actual_id
                # A fallback with no useful label must show its actual ID,
                # never the previous route's host as if it answered.
                self.endpoint_label = ""
            if actual_label and actual_label != "Selected route":
                self.endpoint_label = actual_label
        elif kind == "context_usage":
            data = payload.get("data")
            if isinstance(data, dict):
                self.model = _label(data.get("model"), 200) or self.model
        elif kind == "tool_start":
            self._phase("tool", now)
            self.tool = _label(payload.get("tool"), 100)
            self.tool_call_id = _label(payload.get("tool_call_id"), 200)
        elif kind == "tool_progress":
            self._phase("tool", now)
        elif kind == "tool_output":
            self._phase("model", now)
        elif kind == "ask_user":
            data = payload.get("data")
            self._phase(
                "approval" if isinstance(data, dict) and data.get("kind") == "tool_approval" else "user",
                now,
            )
        elif kind == "agent_step" or payload.get("delta") is not None:
            self._phase("model", now)

    def snapshot(
        self, status: str, *, now: Optional[float] = None,
        durable_seq: int = -1, context_revision: int = 0,
        ledger_hash: Optional[str] = None, stalled: bool = False,
    ) -> dict:
        now = time.time() if now is None else now
        phase = self.phase
        if status == "interrupted":
            phase = "reconnect"
        recovery = (
            "answer" if phase in {"approval", "user"}
            else "inspect" if status == "running" and stalled
            else "reconnect" if phase == "reconnect"
            else "wait" if status == "running" else "none"
        )
        return {
            "phase": phase,
            "phase_since": self.phase_since,
            "phase_seconds": round(max(0.0, now - self.phase_since), 1),
            "model": self.model or None,
            "endpoint_id": self.endpoint_id or None,
            "endpoint_label": self.endpoint_label or None,
            "tool": self.tool or None,
            "tool_call_id": self.tool_call_id or None,
            "checkpoint": {
                "durable_seq": int(durable_seq),
                "context_revision": int(context_revision),
                "ledger_hash": ledger_hash if isinstance(ledger_hash, str) and len(ledger_hash) == 64 else None,
            },
            "recovery_action": recovery,
        }


def compose_wait_panel(
    *, run: Optional[dict], goal: Optional[dict],
    children: Optional[list[dict]] = None, now: Optional[float] = None,
    unknown_effects: Optional[int] = None,
    selected_endpoint_label: Optional[str] = None,
) -> dict:
    """Merge owner-gated records into one content-free waiting diagnosis."""
    now = time.time() if now is None else now
    run = run if isinstance(run, dict) else {}
    goal = goal if isinstance(goal, dict) else {}
    state = run.get("wait_state") if isinstance(run.get("wait_state"), dict) else {}
    health = run.get("progress_health") if isinstance(run.get("progress_health"), dict) else {}
    run_id = _label(run.get("run_id"), 200)
    goal_status = goal.get("status")
    wait_reason = goal.get("wait_reason") if goal.get("wait_reason") in {
        "repeated_premature_stop", "ask_user", "other", "provider_failure", "context_compaction", "unknown_side_effect", "dispatch_failure", "resource_budget",
    } else None
    run_status = run.get("status")
    if goal_status == "waiting_user":
        phase = "approval" if state.get("phase") == "approval" else "user"
    elif goal_status == "paused":
        phase = "paused"
    elif run_status == "running":
        phase = state.get("phase") if state.get("phase") in {"model", "tool", "approval", "user"} else "model"
    elif run_status == "interrupted":
        phase = "reconnect"
    elif goal.get("lease_held") is True or goal_status == "active":
        phase = "queue"
    else:
        phase = "idle"

    candidates = [
        child for child in (children or [])
        if isinstance(child, dict) and child.get("status") in {"running", "queued", "waiting_user", "stopping"}
        and (not run_id or child.get("parent_run_id") == run_id)
    ]
    priority = {"running": 0, "waiting_user": 1, "stopping": 2, "queued": 3}
    candidates.sort(key=lambda child: priority.get(child.get("status"), 9))
    child = candidates[0] if candidates else None
    current_child = ({
        "child_id": _label(child.get("child_id"), 200),
        "status": child.get("status"),
        "model": _label(child.get("model"), 200) or None,
        "endpoint_id": _label(child.get("endpoint_id"), 200) or None,
        "parent_run_id": _label(child.get("parent_run_id"), 200) or None,
    } if child else None)

    phase_since = (
        goal.get("status_since")
        if phase == "paused" or (phase == "user" and state.get("phase") not in {"user", "approval"})
        else state.get("phase_since")
    )
    if not isinstance(phase_since, (int, float)) or isinstance(phase_since, bool):
        phase_since = run.get("started_at")
    if not isinstance(phase_since, (int, float)) or isinstance(phase_since, bool):
        phase_since = now
    digest = run.get("ledger_hash")
    durable_seq = run.get("durable_seq")
    context_revision = run.get("context_revision")
    checkpoint = {
        "durable_seq": durable_seq if type(durable_seq) is int and durable_seq >= -1 else -1,
        "context_revision": context_revision if type(context_revision) is int and context_revision >= 0 else 0,
        "ledger_hash": digest if isinstance(digest, str) and len(digest) == 64 else None,
    }
    recovery = (
        "resume_goal" if goal_status == "waiting_user" and wait_reason == "unknown_side_effect" and unknown_effects == 0
        else "inspect_effect" if goal_status == "waiting_user" and wait_reason == "unknown_side_effect"
        else "inspect_context" if goal_status == "waiting_user" and wait_reason == "context_compaction"
        else "resume_goal" if goal_status == "waiting_user" and wait_reason in {"repeated_premature_stop", "provider_failure", "dispatch_failure", "resource_budget"}
        else "answer" if phase in {"approval", "user"}
        else "resume_goal" if phase == "paused"
        else "reconnect" if phase == "reconnect"
        else "inspect" if phase in {"model", "tool"} and health.get("stalled") is True
        else "wait" if phase in {"model", "tool", "queue"} else "none"
    )
    return {
        "run_id": run_id or None,
        "run_status": run_status if isinstance(run_status, str) else None,
        "phase": phase,
        "phase_since": phase_since,
        "phase_seconds": round(max(0.0, now - phase_since), 1),
        "model": _label(state.get("model"), 200) or None,
        "endpoint_id": _label(state.get("endpoint_id"), 200) or None,
        "endpoint_label": _label(state.get("endpoint_label"), 120) or None,
        "selected_endpoint_label": _label(selected_endpoint_label, 120) or None,
        "tool": _label(state.get("tool"), 100) or None,
        "tool_call_id": _label(state.get("tool_call_id"), 200) or None,
        "current_child": current_child,
        "lease": {
            "held": goal.get("lease_held") is True,
            "expires_at": _label(goal.get("lease_expires_at"), 40) or None,
        },
        "goal_status": goal_status if isinstance(goal_status, str) else None,
        "wait_reason": wait_reason,
        "failure_code": (
            goal.get("failure_code")
            if wait_reason == "context_compaction"
            and goal.get("failure_code") in CONTEXT_FAILURE_CODES else None
        ),
        "budget": (
            dict(goal["budget"])
            if wait_reason == "resource_budget" and isinstance(goal.get("budget"), dict)
            and goal["budget"].get("resource") in {"tool_calls", "model_rounds", "model_tokens", "model_requests", "wall_seconds", "children"}
            and type(goal["budget"].get("used")) is int
            and type(goal["budget"].get("limit")) is int
            and isinstance(goal["budget"].get("run_id"), str)
            else None
        ),
        "unknown_effect_count": unknown_effects if type(unknown_effects) is int and unknown_effects >= 0 else None,
        "attempt": goal.get("attempt") if type(goal.get("attempt")) is int else None,
        "checkpoint": checkpoint,
        "progress_revision": health.get("revision") if type(health.get("revision")) is int else None,
        "stalled": health.get("stalled") is True,
        "recovery_action": recovery,
    }
