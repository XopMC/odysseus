"""Create an owner-scoped, content-free long-chat UI fixture.

Only role, sizes and a small allowlist of rendering shapes cross from the
source chat. No source text, objective, tool arguments, attachment or workspace
binding is copied. This is for long-history/replay performance tests, not for
resuming the source task.
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import timedelta
from typing import Any

from core.database import ChatMessage, Session, SessionLocal, utcnow_naive


def _filler(value: Any, label: str, limit: int = 8192) -> str:
    length = min(len(str(value or "")), limit)
    if length == 0:
        return ""
    prefix = f"[synthetic {label}] "
    return (prefix + "x" * max(0, length - len(prefix)))[:length]


def _small_int(value: Any, default: int = 0, maximum: int = 100000) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {"type": "synthetic_event"}
    event_type = str(event.get("type") or "synthetic_event")
    if not re.fullmatch(r"[a-z_]{1,48}", event_type):
        event_type = "synthetic_event"
    safe: dict[str, Any] = {"type": event_type}
    for key in ("seq", "round", "duration_ms", "token_count"):
        if key in event:
            safe[key] = _small_int(event[key])
    for key in ("delta", "text", "reasoning", "output", "content"):
        if key in event:
            safe[key] = _filler(event[key], key, 512)
    if event_type.startswith("tool_"):
        safe["tool"] = "synthetic_check"
        safe["command"] = "synthetic read-only check"
    return safe


def sanitize_metadata(raw: Any) -> dict[str, Any]:
    """Keep rendering density, never source strings or authority fields."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    safe: dict[str, Any] = {"synthetic_soak_fixture": True}
    if raw.get("hidden") is True:
        safe["hidden"] = True
    if "rendered_message_count" in raw:
        safe["rendered_message_count"] = _small_int(raw["rendered_message_count"], maximum=1000)
    for key in ("round_texts", "round_reasonings"):
        values = raw.get(key)
        if isinstance(values, list):
            safe[key] = [_filler(value, key, 4096) for value in values[:1000]]
    tools = raw.get("tool_events")
    if isinstance(tools, list):
        safe["tool_events"] = [
            {
                "tool": "synthetic_check",
                "command": "synthetic read-only check",
                "output": _filler(event.get("output"), "tool result", 1024),
                "exit_code": 0 if event.get("exit_code") == 0 else 1,
                "round": _small_int(event.get("round"), 0, 1000),
                "tool_call_id": uuid.uuid4().hex,
            }
            for event in tools[:1000] if isinstance(event, dict)
        ]
    timeline = raw.get("timeline_v2")
    if isinstance(timeline, dict) and isinstance(timeline.get("events"), list):
        safe["timeline_v2"] = {
            "run_id": uuid.uuid4().hex,
            "events": [_safe_event(event) for event in timeline["events"][:10000]],
        }
    return safe


def create_clone(*, source_session_id: str, owner: str, model: str,
                 endpoint_url: str, name: str = "SAFE structural long-chat soak",
                 destination_session_id: str | None = None,
                 dry_run: bool = False, max_source_rows: int = 10000,
                 target_rows: int | None = None) -> dict[str, Any]:
    if not source_session_id or not owner or not model or not endpoint_url:
        raise ValueError("source session, owner, model and endpoint are required")
    new_id = destination_session_id or str(uuid.uuid4())
    if new_id == source_session_id:
        raise ValueError("destination must differ from source")
    total = 0
    visible = 0
    original_chars = 0
    synthetic_chars = 0
    now = utcnow_naive()
    with SessionLocal.begin() as db:
        source = db.query(Session).filter_by(id=source_session_id, owner=owner).first()
        if source is None:
            raise ValueError("source chat not found for owner")
        total = db.query(ChatMessage).filter_by(session_id=source_session_id).count()
        if total > max_source_rows or total == 0:
            raise ValueError("source chat exceeds the explicitly bounded fixture size")
        generated = target_rows or total
        if generated < total or generated > 10000:
            raise ValueError("target rows must be between source size and 10000")
        if dry_run:
            clone = None
        elif destination_session_id:
            clone = db.query(Session).filter_by(id=new_id, owner=owner).first()
            if clone is None:
                raise ValueError("destination chat must exist and belong to owner")
            existing = db.query(ChatMessage).filter_by(session_id=new_id).all()
            existing.sort(key=lambda row: (row.timestamp, row.id))
            if not (1 <= len(existing) <= 2 and existing[0].role == "user"
                    and existing[0].content == "SAFE STRUCTURAL SOAK FIXTURE SEED"
                    and (len(existing) == 1 or (existing[1].role == "assistant" and not existing[1].content))):
                raise ValueError("destination must contain exactly the safe fixture seed and optional empty stopped reply")
            if clone.project_id is not None:
                raise ValueError("destination chat must not bind a project workspace")
        else:
            clone = Session(id=new_id, owner=owner)
            db.add(clone)
        if clone is not None:
            clone.name = name
            clone.model = model
            clone.endpoint_url = endpoint_url
            clone.mode = "agent"
            clone.rag = False
            clone.archived = False
            clone.folder = "Safe soak"
            clone.project_id = None
            clone.headers = {}
            clone.message_count = generated + (len(existing) if destination_session_id else 0)
            clone.context_checkpoint = None
            clone.last_message_at = now
            clone.updated_at = now
            db.flush()
        rows = (
            db.query(ChatMessage)
            .filter_by(session_id=source_session_id)
            .order_by(ChatMessage.timestamp, ChatMessage.id)
            .yield_per(100)
        )
        templates = []
        for row in rows:
            metadata = sanitize_metadata(row.meta_data)
            content = _filler(row.content, row.role, 8192)
            templates.append((row.role if row.role in {"user", "assistant", "system", "tool"} else "assistant", content, metadata, not metadata.get("hidden")))
            original_chars += len(row.content or "")
        for index in range(generated):
            role, content, metadata, is_visible = templates[index % total]
            synthetic_chars += len(content)
            if is_visible:
                visible += 1
            if not dry_run:
                metadata_json = json.dumps(metadata if index < total else sanitize_metadata(metadata))
                db.add(ChatMessage(
                    id=uuid.uuid4().hex, session_id=new_id,
                    role=role, content=content, meta_data=metadata_json,
                    timestamp=now - timedelta(seconds=generated - index),
                ))
            if not dry_run and index % 100 == 99:
                db.flush()
        if clone is not None:
            clone.message_count = generated + (len(existing) if destination_session_id else 0)
    return {
        "clone_id": None if dry_run else new_id,
        "dry_run": dry_run,
        "source_rows": total,
        "synthetic_rows": generated,
        "visible_rows": visible,
        "source_chars": original_chars,
        "synthetic_chars": synthetic_chars,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint-url", required=True)
    parser.add_argument("--name", default="SAFE structural long-chat soak")
    parser.add_argument("--destination-session-id", help="Existing empty safe chat; keeps the UI session registered")
    parser.add_argument("--dry-run", action="store_true", help="Only report safe counts and generated sizes; make no writes")
    parser.add_argument("--max-source-rows", type=int, default=10000)
    parser.add_argument("--target-rows", type=int, help="Expand sanitized shapes to a bounded larger synthetic history")
    args = parser.parse_args()
    print(json.dumps(create_clone(
        source_session_id=args.source_session_id, owner=args.owner,
        model=args.model, endpoint_url=args.endpoint_url, name=args.name,
        destination_session_id=args.destination_session_id,
        dry_run=args.dry_run, max_source_rows=args.max_source_rows,
        target_rows=args.target_rows,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
