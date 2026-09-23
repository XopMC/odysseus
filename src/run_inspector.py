"""Bounded, content-free run tree for one owner-scoped chat."""

import json
import math
import re

from sqlalchemy import and_, func, or_
from core.database import (ChatRunState, ChatSubagentRun, ChatToolIntent,
                           ChatSubagentEvidence, ChatSubagentEvent, SessionLocal)


def _stamp(value):
    return value.isoformat() + "Z" if value else None


def snapshot(owner: str | None, session_id: str, *, limit: int = 20,
             before_run_id: str | None = None) -> dict:
    """Project only safe scalar columns; never hydrate prompts or receipts.

    The route must verify chat ownership first. SQL applies the owner filter
    again so this function cannot accidentally join another owner's records.
    """
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Invalid run inspector limit")
    if before_run_id is not None and (
        not isinstance(before_run_id, str) or not re.fullmatch(r"[0-9a-f]{32}|[A-Za-z0-9_-]{1,64}", before_run_id)
    ):
        raise ValueError("Invalid run cursor")
    # Agent/effect rows use the single-user sentinel; child rows use an empty
    # owner in AUTH_ENABLED=false. Keep those historical durable scopes intact.
    scope = owner or "__odysseus_single_user__"
    child_scope = owner or ""
    with SessionLocal() as db:
        cursor_row = None
        if before_run_id is not None:
            cursor_row = db.query(ChatRunState.started_at, ChatRunState.run_id).filter(
                ChatRunState.owner == scope, ChatRunState.session_id == session_id,
                ChatRunState.run_id == before_run_id,
            ).first()
            if cursor_row is None:
                raise ValueError("Unknown run cursor")
        runs_query = db.query(
            ChatRunState.run_id, ChatRunState.status, ChatRunState.started_at,
            ChatRunState.terminal_at, ChatRunState.last_seq,
            ChatRunState.durable_seq, ChatRunState.context_revision,
        ).filter(
            ChatRunState.owner == scope, ChatRunState.session_id == session_id,
        )
        if cursor_row is not None:
            runs_query = runs_query.filter(or_(
                ChatRunState.started_at < cursor_row.started_at,
                and_(ChatRunState.started_at == cursor_row.started_at,
                     ChatRunState.run_id < cursor_row.run_id),
            ))
        runs = runs_query.order_by(ChatRunState.started_at.desc(), ChatRunState.run_id.desc()).limit(limit + 1).all()
        has_more = len(runs) > limit
        runs = runs[:limit]
        run_ids = [row.run_id for row in runs]
        if not run_ids:
            return {"runs": [], "limit": limit, "has_more": False, "next_cursor": None}
        children = db.query(
            ChatSubagentRun.id, ChatSubagentRun.parent_run_id,
            ChatSubagentRun.status, ChatSubagentRun.model,
            ChatSubagentRun.endpoint_id, ChatSubagentRun.started_at,
            ChatSubagentRun.finished_at, ChatSubagentRun.worker_id,
        ).filter(
            ChatSubagentRun.owner == child_scope,
            ChatSubagentRun.parent_session_id == session_id,
            ChatSubagentRun.parent_run_id.in_(run_ids),
        ).order_by(ChatSubagentRun.created_at.desc()).limit(200).all()
        intents = db.query(
            ChatToolIntent.id, ChatToolIntent.run_id,
            ChatToolIntent.tool_call_id, ChatToolIntent.tool_name,
            ChatToolIntent.status, ChatToolIntent.created_at,
            ChatToolIntent.updated_at,
        ).filter(
            ChatToolIntent.owner == scope,
            ChatToolIntent.session_id == session_id,
            ChatToolIntent.run_id.in_(run_ids),
        ).order_by(ChatToolIntent.created_at.desc()).limit(500).all()
        child_ids = [row.id for row in children]
        evidence = []
        child_cursors = []
        if child_ids:
            child_cursors = db.query(
                ChatSubagentEvent.child_id, func.max(ChatSubagentEvent.id),
            ).filter(
                ChatSubagentEvent.owner == child_scope,
                ChatSubagentEvent.parent_session_id == session_id,
                ChatSubagentEvent.child_id.in_(child_ids),
            ).group_by(ChatSubagentEvent.child_id).all()
            evidence = db.query(
                ChatSubagentEvidence.id, ChatSubagentEvidence.child_id,
                ChatSubagentEvidence.kind, ChatSubagentEvidence.created_at,
            ).filter(
                ChatSubagentEvidence.owner == child_scope,
                ChatSubagentEvidence.parent_session_id == session_id,
                ChatSubagentEvidence.child_id.in_(child_ids),
            ).order_by(ChatSubagentEvidence.created_at.desc()).limit(200).all()

    cursor_by_child = {child_id: int(cursor) for child_id, cursor in child_cursors}
    evidence_by_child = {}
    for row in evidence:
        evidence_by_child.setdefault(row.child_id, []).append({
            "id": row.id, "kind": row.kind, "status": "published",
            "created_at": _stamp(row.created_at),
        })
    children_by_run = {}
    for row in children:
        children_by_run.setdefault(row.parent_run_id, []).append({
            "child_run_id": row.id, "worker_id": row.worker_id,
            "status": row.status, "model": row.model,
            "endpoint_id": row.endpoint_id,
            "started_at": _stamp(row.started_at),
            "finished_at": _stamp(row.finished_at),
            "event_cursor": cursor_by_child.get(row.id),
            "artifacts": evidence_by_child.get(row.id, []),
        })
    intents_by_run = {}
    for row in intents:
        intents_by_run.setdefault(row.run_id, []).append({
            "intent_id": row.id, "tool_call_id": row.tool_call_id,
            "tool_name": row.tool_name, "status": row.status,
            "created_at": _stamp(row.created_at),
            "updated_at": _stamp(row.updated_at),
        })
    return {"runs": [{
        "run_id": row.run_id, "status": row.status,
        "started_at": _stamp(row.started_at),
        "terminal_at": _stamp(row.terminal_at),
        "last_seq": row.last_seq, "durable_seq": row.durable_seq,
        "context_revision": row.context_revision,
        "children": children_by_run.get(row.run_id, []),
            "tool_calls": intents_by_run.get(row.run_id, []),
    } for row in runs], "limit": limit, "has_more": has_more,
            "next_cursor": runs[-1].run_id if has_more else None,
            "truncated": {"children": len(children) == 200,
                          "tool_calls": len(intents) == 500,
                          "artifacts": len(evidence) == 200}}


def artifact_detail(owner: str | None, session_id: str, evidence_id: str) -> dict | None:
    """Fetch one explicitly selected child evidence body; never bulk-hydrate it."""
    if not isinstance(evidence_id, str) or not re.fullmatch(r"[0-9a-f]{32}", evidence_id):
        raise ValueError("Invalid artifact ID")
    scope = owner or ""
    with SessionLocal() as db:
        row = db.query(ChatSubagentEvidence).filter(
            ChatSubagentEvidence.owner == scope,
            ChatSubagentEvidence.parent_session_id == session_id,
            ChatSubagentEvidence.id == evidence_id,
        ).first()
        if row is None:
            return None
        return {"id": row.id, "child_id": row.child_id, "kind": row.kind,
                "status": "published", "created_at": _stamp(row.created_at),
                "body": row.body, "artifact_refs": list(row.artifact_refs or [])[:32]}


_EVENT_KINDS = frozenset({
    "agent_step", "tool_start", "tool_output", "agent_terminal",
    "ask_user", "goal_update", "plan_update", "context_checkpoint",
    "compacted", "context_compaction_failed", "generated_image",
})
_SAFE_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_.:]{0,99}\Z")
_SAFE_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


def event_refs(owner: str | None, session_id: str, run_id: str, *,
               before_seq: int | None = None, limit: int = 200) -> dict:
    """Read one indexed replay page and return only navigable event identities.

    No frame body, command, prompt, tool arguments or model output reaches the
    inspector. `previous_cursor` still advances across pages with no matches.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid run ID")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid inspector page size")
    if before_seq is not None and (type(before_seq) is not int or before_seq < 0):
        raise ValueError("Invalid replay cursor")
    scope = owner or "__odysseus_single_user__"
    with SessionLocal() as db:
        row = db.query(ChatRunState.run_id).filter(
            ChatRunState.owner == scope,
            ChatRunState.session_id == session_id,
            ChatRunState.run_id == run_id,
        ).first()
    if row is None:
        return None
    from src.agent_runs import replay_root
    from src.chat_replay_log import ReplayLog
    try:
        log = ReplayLog(replay_root(), run_id, session_id)
        cursor = len(log) if before_seq is None else before_seq
        page = log.page_before(cursor, limit)
    except (FileNotFoundError, OSError):
        return {"run_id": run_id, "events": [], "previous_cursor": 0,
                "has_more_before": False, "replay_available": False}
    events = []
    for item in page["events"]:
        raw = "\n".join(line[5:].lstrip() for line in item["event"].splitlines()
                        if line.startswith("data:"))
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind not in _EVENT_KINDS:
            if payload.get("delta") is None:
                continue
            kind = "thinking_delta" if payload.get("thinking") is True else "text_delta"
        replay = payload.get("_replay")
        replay = replay if isinstance(replay, dict) else {}
        created_at = replay.get("created_at")
        if (type(created_at) not in (int, float) or not math.isfinite(created_at)
                or not 0 <= created_at <= 4_102_444_800):
            created_at = None
        tool = payload.get("tool")
        tool_call_id = replay.get("tool_call_id") or payload.get("tool_call_id")
        segment_id = replay.get("segment_id")
        events.append({
            "seq": item["seq"], "kind": kind,
            "created_at": created_at,
            "segment_id": segment_id if isinstance(segment_id, str) and re.fullmatch(
                re.escape(run_id) + r":[0-9]{1,12}", segment_id) else None,
            "tool_call_id": tool_call_id if isinstance(tool_call_id, str) and _SAFE_CALL_ID.fullmatch(tool_call_id) else None,
            "tool_name": tool if isinstance(tool, str) and _SAFE_TOOL.fullmatch(tool) else None,
        })
    return {"run_id": run_id, "events": events,
            "previous_cursor": page["previous_cursor"],
            "has_more_before": page["has_more_before"], "replay_available": True}
