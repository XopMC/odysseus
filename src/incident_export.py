"""Content-free, reproducible diagnostics for an owner-scoped chat session.

Never serialize conversation, replay frames, model prompts, paths, endpoint
details, tool arguments, receipts, or context snapshots into this archive.
"""
import io
import json
import math
import re
import zipfile

from core.database import ChatRunState, ChatToolIntent, SessionLocal
from core.constants import APP_VERSION
from src import agent_runs
from src.chat_replay_log import ReplayLog


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_RUN_STATUSES = frozenset({"running", "done", "error", "stopped", "interrupted"})
_EFFECT_STATUSES = frozenset({
    "intent", "unknown", "done", "verified", "verified_not_applied",
    "retry_authorized", "retry_consumed", "no_retry",
})
_TERMINAL_REASONS = frozenset({
    "process_restarted", "cancelled", "superseded_by_new_run", "user_stop",
})
_METRIC_LIMITS = {
    "ttft_last_ms": 3_600_000,
    "ttft_max_ms": 3_600_000,
    "prefill_tps_last": 1_000_000,
    "tool_latency_mean_ms": 3_600_000,
    "tool_latency_max_ms": 3_600_000,
    "compaction_last_ms": 3_600_000,
    "compaction_max_ms": 3_600_000,
    "sse_reconnects": 1_000_000,
    "compaction_failures": 1_000_000,
}


def _status(value, allowed):
    return value if value in allowed else "unrecognized"


def _safe_metric(value, maximum):
    if isinstance(value, str):
        # PostgreSQL JSON text extraction yields a string. Accept only a
        # bounded decimal scalar, never arbitrary content stored under a
        # telemetry key; SQLite already returns numeric JSON as int/float.
        if len(value) > 32 or not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,4})?", value):
            return None
        value = float(value)
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        return None
    return round(value, 2)


def build_incident_archive(session_id: str, owner: str | None) -> bytes:
    """Return a ZIP with only allowlisted operational fields.

    The caller must verify session ownership before invoking this function.
    Database queries apply the owner constraint again as defense in depth.
    """
    db = SessionLocal()
    try:
        # SQLAlchemy compiles typed JSON path extraction for both SQLite and
        # PostgreSQL. Only scalar allowlist paths reach Python; the prompt,
        # receipt and full continuation JSON remain inside the database.
        safe_json_columns = [
            ChatRunState.continuation["terminal_reason"].as_string().label("terminal_reason_code"),
            *(ChatRunState.continuation["health_metrics"][key].as_string().label(key)
              for key in _METRIC_LIMITS),
        ]
        # Select only allowlisted columns. Loading ORM rows would hydrate
        # context snapshots and effect receipts containing sensitive data.
        runs_query = db.query(
            ChatRunState.run_id, ChatRunState.status, ChatRunState.started_at,
            ChatRunState.terminal_at, ChatRunState.last_seq,
            ChatRunState.durable_seq, ChatRunState.context_revision,
            *safe_json_columns,
        ).filter(ChatRunState.session_id == session_id)
        intents_query = db.query(
            ChatToolIntent.run_id, ChatToolIntent.status,
        ).filter(ChatToolIntent.session_id == session_id)
        if owner is not None:
            runs_query = runs_query.filter(ChatRunState.owner == owner)
            intents_query = intents_query.filter(ChatToolIntent.owner == owner)
        runs = runs_query.order_by(ChatRunState.started_at, ChatRunState.run_id).all()
        intents = intents_query.all()
        intent_counts = {}
        for intent in intents:
            by_status = intent_counts.setdefault(intent.run_id, {})
            status = _status(intent.status, _EFFECT_STATUSES)
            by_status[status] = by_status.get(status, 0) + 1
        records = []
        for run in runs:
            safe_run_id = run.run_id if isinstance(run.run_id, str) and re.fullmatch(r"[0-9a-f]{32}", run.run_id) else "invalid"
            replay_count = None
            replay_status = "unavailable"
            if safe_run_id != "invalid":
                try:
                    replay = ReplayLog(agent_runs.replay_root(), safe_run_id, session_id)
                    replay_count = len(replay)
                    replay_status = _status(replay.metadata.get("status"), _RUN_STATUSES)
                except (FileNotFoundError, OSError, ValueError):
                    pass
            records.append({
                "run_id": safe_run_id,
                "status": _status(run.status, _RUN_STATUSES),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "terminal_at": run.terminal_at.isoformat() if run.terminal_at else None,
                "last_seq": run.last_seq,
                "durable_seq": run.durable_seq,
                "context_revision": run.context_revision,
                "terminal_reason_code": _status(
                    getattr(run, "terminal_reason_code", None), _TERMINAL_REASONS,
                ),
                "health_metrics": {
                    key: _safe_metric(getattr(run, key, None), limit)
                    for key, limit in _METRIC_LIMITS.items()
                },
                "replay_event_count": replay_count,
                "replay_status": replay_status,
                "effect_status_counts": intent_counts.get(run.run_id, {}),
            })
    finally:
        db.close()

    manifest = {
        "schema": "odysseus.incident.v1",
        "app_version": APP_VERSION,
        "privacy": "allowlist-only; no chat content, prompts, paths, endpoint details or secrets",
        "run_count": len(records),
        "health_metrics_available": True,
        "files": ["manifest.json", "summary.json", "runs.json", "event-schema.json", "replay-fixture.json"],
    }
    status_counts = {}
    for record in records:
        status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
    summary = {
        "run_status_counts": status_counts,
        "error_run_count": status_counts.get("error", 0),
        "max_durable_lag_events": max(
            (max(0, int(record["last_seq"] or 0) - int(record["durable_seq"] or 0))
             for record in records), default=0,
        ),
        "unknown_effect_count": sum(
            record["effect_status_counts"].get("unknown", 0) for record in records
        ),
        "max_ttft_ms": max(
            (record["health_metrics"]["ttft_max_ms"] or 0 for record in records), default=0,
        ),
        "max_tool_latency_ms": max(
            (record["health_metrics"]["tool_latency_max_ms"] or 0 for record in records), default=0,
        ),
        "total_sse_reconnects": sum(
            record["health_metrics"]["sse_reconnects"] or 0 for record in records
        ),
        "total_compaction_failures": sum(
            record["health_metrics"]["compaction_failures"] or 0 for record in records
        ),
    }
    event_schema = {
        "schema": "odysseus.incident.events.v1",
        "identity": ["run_id", "seq", "segment_id", "tool_call_id"],
        "safe_status_fields": ["status", "last_seq", "durable_seq", "context_revision",
                               "terminal_reason_code", "health_metrics"],
        "excluded": ["content", "reasoning", "tool_arguments", "tool_result", "receipt", "context_snapshot"],
        "note": "This archive records counts and cursor state, not original replay frames.",
    }
    fixture = {
        "schema": "odysseus.replay-fixture.v1",
        "description": "Synthetic event shapes for reducer reproduction; not original messages.",
        "events": [
            {"run_id": "f" * 32, "seq": 0, "type": "agent_step", "round": 1, "segment_id": "fixture-segment-1"},
            {"run_id": "f" * 32, "seq": 1, "delta": "[synthetic thinking]", "thinking": True, "segment_id": "fixture-segment-1"},
            {"run_id": "f" * 32, "seq": 2, "type": "tool_start", "tool": "fixture_tool", "segment_id": "fixture-segment-1", "tool_call_id": "fixture-tool-1"},
            {"run_id": "f" * 32, "seq": 3, "type": "tool_progress", "segment_id": "fixture-segment-1", "tool_call_id": "fixture-tool-1"},
            {"run_id": "f" * 32, "seq": 4, "type": "tool_output", "tool": "fixture_tool", "segment_id": "fixture-segment-1", "tool_call_id": "fixture-tool-1", "exit_code": 0},
            {"run_id": "f" * 32, "seq": 5, "type": "agent_step", "round": 2, "segment_id": "fixture-segment-2"},
            {"run_id": "f" * 32, "seq": 6, "delta": "[synthetic answer]", "segment_id": "fixture-segment-2"},
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in (("manifest.json", manifest), ("summary.json", summary),
                            ("runs.json", records),
                            ("event-schema.json", event_schema), ("replay-fixture.json", fixture)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, _json(value))
    return output.getvalue()
