"""Content-free, reproducible diagnostics for an owner-scoped chat session.

Never serialize conversation, replay frames, model prompts, paths, endpoint
details, tool arguments, receipts, or context snapshots into this archive.
"""
import io
import json
import re
import zipfile

from core.database import ChatRunState, ChatToolIntent, SessionLocal
from core.constants import APP_VERSION
from src import agent_runs
from src.chat_replay_log import ReplayLog


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_RUN_STATUSES = frozenset({"running", "done", "error", "stopped", "interrupted"})
_EFFECT_STATUSES = frozenset({"intent", "unknown", "done", "no_retry"})


def _status(value, allowed):
    return value if value in allowed else "unrecognized"


def build_incident_archive(session_id: str, owner: str | None) -> bytes:
    """Return a ZIP with only allowlisted operational fields.

    The caller must verify session ownership before invoking this function.
    Database queries apply the owner constraint again as defense in depth.
    """
    db = SessionLocal()
    try:
        # Select only allowlisted columns. Loading ORM rows would hydrate
        # context snapshots and effect receipts containing sensitive data.
        runs_query = db.query(
            ChatRunState.run_id, ChatRunState.status, ChatRunState.started_at,
            ChatRunState.terminal_at, ChatRunState.last_seq,
            ChatRunState.durable_seq, ChatRunState.context_revision,
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
    }
    event_schema = {
        "schema": "odysseus.incident.events.v1",
        "identity": ["run_id", "seq", "segment_id", "tool_call_id"],
        "safe_status_fields": ["status", "last_seq", "durable_seq", "context_revision"],
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
