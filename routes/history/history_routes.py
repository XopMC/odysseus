"""History routes — session history, truncation, fork, conversation topics."""

import base64
import binascii
import json
import uuid
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy import and_, case, func, or_

from core.models import ChatMessage
from core.database import SessionLocal, ChatMessage as DbChatMessage, Session as DbSession
from src.auth_helpers import effective_user, require_chat_api_token_scope
from src.topic_analyzer import analyze_topics
from src.upload_handler import reserve_message_upload_references
from src.tool_approval_scopes import sanitize_client_message_metadata
from routes.session_routes import (
    _message_role,
    _message_text,
    _reject_compact_during_active_run,
    _verify_session_owner,
)

logger = logging.getLogger(__name__)

_HISTORY_INLINE_MEDIA_THRESHOLD = 200_000
_DATA_IMAGE_RE = re.compile(r"data:image/[^;,\"]+;base64,[A-Za-z0-9+/=\s]+")


def _history_cursor(message_id: str, before: int) -> str:
    payload = json.dumps({"id": message_id, "before": max(0, int(before))}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _history_cursor_id(value: str) -> tuple[str, int]:
    if not value or len(value) > 512:
        raise HTTPException(400, "Invalid history cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        payload = json.loads(raw)
        message_id = str(payload["id"])
        before = int(payload["before"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise HTTPException(400, "Invalid history cursor") from None
    if not message_id or len(message_id) > 128 or before < 0:
        raise HTTPException(400, "Invalid history cursor")
    return message_id, before


def _history_display_content(content: Any) -> Any:
    """Return a lightweight browser-display copy of stored message content.

    Older multimodal user messages may be persisted as a JSON *string*
    containing image_url blocks with inline base64 image bytes. Those bytes are
    needed for model calls when the turn is first sent, but they should not be
    sent back through /api/history every time the user opens the chat. The
    attachment metadata already carries file ids/names for the UI cards.
    """
    if isinstance(content, list):
        text_parts = []
        omitted_media = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif block.get("type") in {"image_url", "input_image", "audio", "input_audio"}:
                omitted_media += 1
        text = "\n".join(text_parts).strip()
        if omitted_media and not text:
            return f"[{omitted_media} media attachment{'s' if omitted_media != 1 else ''} omitted from history view]"
        return text

    if not isinstance(content, str):
        return content
    if len(content) < _HISTORY_INLINE_MEDIA_THRESHOLD and "data:image/" not in content:
        return content

    stripped = content.lstrip()
    if stripped.startswith("["):
        try:
            blocks = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            blocks = None
        if isinstance(blocks, list):
            text_parts = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
            if text_parts:
                return "\n".join(text_parts).strip()

    if "data:image/" in content:
        return _DATA_IMAGE_RE.sub("[inline image omitted from history view]", content)
    return content


def _merge_continue_rows_to_delete(db_messages, db1, db2):
    """DB rows to delete when merging the last two assistant messages.

    Always the second assistant message (db2), plus ONLY the single
    intervening "continue" user message (the one carrying "previous response
    was interrupted") — matching the in-memory merge. The previous code
    deleted the whole index range between the two assistant rows, destroying
    any tool/system/user messages in between and desyncing the DB from the
    in-memory history.
    """
    to_delete = [db2]
    i1 = next((i for i, m in enumerate(db_messages) if m is db1), None)
    i2 = next((i for i, m in enumerate(db_messages) if m is db2), None)
    if i1 is not None and i2 is not None and i2 - 1 > i1:
        between = db_messages[i2 - 1]
        if getattr(between, "role", "") == "user" and            "previous response was interrupted" in (getattr(between, "content", "") or ""):
            to_delete.append(between)
    return to_delete


def _context_route_matches(snapshot, session):
    from src.agent_context import context_endpoint_key
    return (snapshot['model'] == session.model and
            ('endpoint_key' not in snapshot or snapshot['endpoint_key'] == context_endpoint_key(session.endpoint_url)))


def _last_request_context(session):
    """Use only the final assistant's explicit snapshot, labeled as historical.

    An appended user/tool message or a later manual summary invalidates it.
    Older billing metadata (input_tokens/context_percent) is not occupancy.
    """
    from src.agent_runs import normalize_context_usage

    history = session.history
    if not history or _message_role(history[-1]) != "assistant":
        return None
    metadata = getattr(history[-1], "metadata", None) or {}
    snapshot = normalize_context_usage(metadata.get("working_context"))
    if snapshot is None or not _context_route_matches(snapshot, session):
        return None

    def timestamp(meta):
        try:
            value = datetime.fromisoformat(str(meta.get("timestamp", "")).replace("Z", "+00:00"))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        except (ValueError, TypeError):
            return None

    measured_at = timestamp(metadata)
    checkpoint = getattr(session, "context_checkpoint", None)
    checkpoint_meta = getattr(checkpoint, "metadata", None) or {}
    checkpoint_at = timestamp(checkpoint_meta)
    if checkpoint_at is not None and (measured_at is None or checkpoint_at >= measured_at):
        # Manual/automatic compaction created a newer working ledger. Do not
        # keep showing the pre-compaction request measurement at 100%.
        return None
    for message in history[:-1]:
        meta = getattr(message, "metadata", None) or {}
        if meta.get("compacted"):
            compacted_at = timestamp(meta)
            if measured_at is None or compacted_at is None or compacted_at >= measured_at:
                return None
    return snapshot


def setup_history_routes(session_manager, upload_handler=None) -> APIRouter:
    router = APIRouter(
        tags=["history"],
        dependencies=[Depends(require_chat_api_token_scope)],
    )
    # Long chats make the renderer-aware aggregate moderately expensive: it
    # evaluates JSON metadata for every stored row. Cache it against the small
    # denormalized Session revision tuple. Live run units are added separately
    # below, so the badge remains live without rescanning thousands of rows on
    # every three-second poll from every browser.
    _rendered_totals_cache: Dict[str, tuple[tuple[Any, ...], tuple[int, int, int]]] = {}

    def _session_count_signature(row: DbSession) -> tuple[Any, ...]:
        return (
            int(row.message_count or 0),
            row.updated_at.isoformat() if row.updated_at else None,
            row.last_message_at.isoformat() if row.last_message_at else None,
        )

    def _reserve_message_uploads(
        request: Request,
        content: Any,
        metadata: Any = None,
    ) -> None:
        try:
            missing_id = reserve_message_upload_references(
                upload_handler,
                effective_user(request),
                content,
                metadata,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Invalid message attachment metadata") from exc
        if missing_id:
            raise HTTPException(
                409,
                f"Referenced upload is no longer available: {missing_id}",
            )

    def _history_metadata_view(value: Dict[str, Any]) -> Dict[str, Any]:
        """Return the bounded browser view of durable message metadata.

        The complete replay stays in SQLite + the owner-scoped replay artifact.
        Shipping up to 5,000 duplicate SSE frames inside every assistant row
        made a 50-message page tens of megabytes and kept those objects alive in
        Safari. Canonical round arrays are enough for eager rendering; missing
        legacy reasoning is reconstructed once before the event list is dropped.
        """
        meta = dict(value or {})
        timeline = meta.get("timeline_v2")
        events = timeline.get("events") if isinstance(timeline, dict) else None
        if isinstance(events, list):
            rounds = list(meta.get("round_reasonings") or [])
            fill = {index + 1 for index, item in enumerate(rounds) if not str(item or "").strip()}
            for item in events:
                payload = item.get("data") if isinstance(item, dict) else None
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError):
                        payload = None
                if not isinstance(payload, dict) or not payload.get("delta"):
                    continue
                if payload.get("thinking") is not True and payload.get("channel") not in {"thinking", "thought"}:
                    continue
                try:
                    round_number = max(1, int(payload.get("round") or (payload.get("_replay") or {}).get("round") or 1))
                except (TypeError, ValueError):
                    round_number = 1
                while len(rounds) < round_number:
                    rounds.append("")
                    fill.add(len(rounds))
                if round_number in fill:
                    rounds[round_number - 1] += str(payload["delta"])
            if rounds:
                meta["round_reasonings"] = rounds
            meta["timeline_v2"] = {
                key: item for key, item in timeline.items() if key != "events"
            }
            meta["timeline_v2"]["event_count"] = len(events)

        bounded_tools = []
        for event in meta.get("tool_events") or []:
            if not isinstance(event, dict):
                continue
            clean = dict(event)
            output = str(clean.get("output") or "")
            replay = clean.get("_replay") if isinstance(clean.get("_replay"), dict) else {}
            if len(output) > 8192 and replay.get("run_id") and isinstance(replay.get("seq"), int):
                # Keep the lazy-render threshold crossed; the full value is
                # loaded from the artifact endpoint only when the user opens it.
                clean["output"] = output[:8193]
                clean["output_preview_truncated"] = True
            bounded_tools.append(clean)
        if bounded_tools:
            meta["tool_events"] = bounded_tools
        return meta

    def _db_history_entry(m: DbChatMessage) -> Dict[str, Any]:
        entry = {"role": m.role, "content": _history_display_content(m.content)}
        meta = {}
        if m.meta_data:
            try:
                meta = _history_metadata_view(json.loads(m.meta_data) or {})
            except (json.JSONDecodeError, ValueError):
                meta = {}
        # The DB row is canonical. A replay/timeline merge may carry an older
        # metadata timestamp; always refresh it from the persisted message time
        # so every device renders the same minute.
        if m.timestamp:
            meta["timestamp"] = m.timestamp.isoformat() + "Z"
        # Stable identity lets live/reconnect reconciliation append only new
        # canonical rows instead of replacing the already loaded older page.
        meta["_db_id"] = m.id
        if meta:
            entry["metadata"] = meta
        return entry

    def _history_rendered_units(entry: Dict[str, Any]) -> int:
        """Top-level message bubbles produced by the browser renderer."""
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        if metadata.get("hidden"):
            return 0
        role = str(entry.get("role") or "")
        content = str(entry.get("content") or "")
        if role == "user" and (
            content == "Continue where you left off"
            or content.startswith("Your message was cut off.")
            or content.startswith("Your previous response was interrupted.")
            or "[Instruction: Rewrite" in content
            or "[Instruction: Explain" in content
        ):
            return 0
        if role == "assistant":
            try:
                persisted = int(metadata.get("rendered_message_count") or 0)
            except (TypeError, ValueError):
                persisted = 0
            if persisted > 0:
                return persisted
            rounds = metadata.get("round_texts")
            if isinstance(rounds, list) and len(rounds) > 1:
                return len(rounds)
        return 1

    def _rendered_message_totals(db, session_id: str) -> tuple[int, int, int]:
        """Count canonical/rendered rows without hydrating message metadata.

        The sidebar/header poll used to call ``/api/history?...limit=1`` every
        three seconds.  A long Agent turn stores megabytes of timeline metadata
        on its final row, so even that one-row response repeatedly parsed and
        serialized the entire payload.  Keep the count path aggregate-only.
        """
        hidden = func.coalesce(
            func.json_extract(DbChatMessage.meta_data, "$.hidden"), 0,
        ) == 1
        synthetic_user = and_(
            DbChatMessage.role == "user",
            or_(
                DbChatMessage.content == "Continue where you left off",
                DbChatMessage.content.like("Your message was cut off.%"),
                DbChatMessage.content.like("Your previous response was interrupted.%"),
                DbChatMessage.content.like("%[Instruction: Rewrite%"),
                DbChatMessage.content.like("%[Instruction: Explain%"),
            ),
        )
        persisted_units = func.coalesce(
            func.json_extract(DbChatMessage.meta_data, "$.rendered_message_count"), 0,
        )
        legacy_rounds = func.coalesce(func.json_array_length(func.json_extract(
            DbChatMessage.meta_data, "$.round_texts",
        )), 0)
        assistant_units = case(
            (persisted_units > 0, persisted_units),
            (legacy_rounds > 1, legacy_rounds),
            else_=1,
        )
        rendered_units = case(
            (hidden, 0),
            (synthetic_user, 0),
            (DbChatMessage.role == "assistant", assistant_units),
            else_=1,
        )
        total, canonical_visible, rendered = db.query(
            func.count(DbChatMessage.id),
            func.coalesce(func.sum(case((~hidden, 1), else_=0)), 0),
            func.coalesce(func.sum(rendered_units), 0),
        ).filter(DbChatMessage.session_id == session_id).one()
        return int(total or 0), int(canonical_visible or 0), int(rendered or 0)

    @router.get("/api/session/{session_id}/message-count")
    async def get_session_message_count(request: Request, session_id: str) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                raise HTTPException(404, f"Session '{session_id}' not found")
            from src import agent_runs
            run = agent_runs.describe_run(session_id)
            signature = _session_count_signature(db_session)
            cached = _rendered_totals_cache.get(session_id)
            if cached is not None and cached[0] == signature:
                total, canonical_visible, rendered = cached[1]
            else:
                total, canonical_visible, rendered = _rendered_message_totals(db, session_id)
                if len(_rendered_totals_cache) >= 2048:
                    _rendered_totals_cache.clear()
                _rendered_totals_cache[session_id] = (
                    signature, (total, canonical_visible, rendered),
                )
            live_units = (
                int(run.get("live_rendered_units") or 0)
                if run and run.get("status") == "running" else 0
            )
            return {
                "total": total,
                "history_revision": db_session.updated_at.isoformat() if db_session.updated_at else None,
                "canonical_visible_total": canonical_visible,
                "canonical_rendered_total": rendered,
                "live_rendered_units": live_units,
                "rendered_total": rendered + live_units,
                "visible_total": rendered + live_units,
            }
        finally:
            db.close()

    @router.get("/api/history/{session_id}")
    async def get_session_history(
        request: Request,
        session_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        _verify_session_owner(request, session_id)
        if limit is not None:
            page_limit = max(1, min(int(limit), 100))
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session is None:
                    raise HTTPException(404, f"Session '{session_id}' not found")

                total, canonical_visible_total, rendered_total = _rendered_message_totals(db, session_id)
                # The header count is server-authoritative and counts exactly
                # what history can render. Hidden compaction/checkpoint rows
                # remain in raw ``total`` for legacy cursor compatibility but
                # never inflate the cross-device visible message count.
                # Explicit offsets remain available for older clients. New
                # clients use a stable opaque cursor so inserts at the tail do
                # not shift an in-progress upward pagination session.
                if offset is not None and cursor is None:
                    page_offset = max(0, min(int(offset), total))
                    rows = (
                        db.query(DbChatMessage)
                        .filter(DbChatMessage.session_id == session_id)
                        .order_by(DbChatMessage.timestamp, DbChatMessage.id)
                        .offset(page_offset)
                        .limit(page_limit)
                        .all()
                    )
                    history_dict = [
                        entry for entry in (_db_history_entry(m) for m in rows)
                        if not (entry.get("metadata") or {}).get("hidden")
                    ]
                    next_cursor = _history_cursor(rows[0].id, page_offset) if rows and page_offset > 0 else None
                    has_more_before = page_offset > 0
                    has_more_after = page_offset + len(rows) < total
                else:
                    epoch = datetime(1970, 1, 1)
                    order_ts = func.coalesce(DbChatMessage.timestamp, epoch)
                    query = db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id)
                    cursor_before = total
                    if cursor:
                        anchor_id, cursor_before = _history_cursor_id(cursor)
                        anchor = query.filter(DbChatMessage.id == anchor_id).first()
                        if anchor is None:
                            raise HTTPException(400, "Invalid history cursor")
                        anchor_ts = anchor.timestamp or epoch
                        query = query.filter(or_(
                            order_ts < anchor_ts,
                            and_(order_ts == anchor_ts, DbChatMessage.id < anchor.id),
                        ))

                    # Count *visible* messages, not raw rows: hidden compaction
                    # summaries never reduce the requested 50-message window.
                    visible_desc = []
                    rendered_page_units = 0
                    scanned_oldest = None
                    batch_anchor = None
                    raw_consumed = 0
                    has_more_before = False
                    batch_size = max(100, page_limit * 2)
                    while len(visible_desc) < page_limit:
                        batch_query = query
                        if batch_anchor is not None:
                            batch_ts = batch_anchor.timestamp or epoch
                            batch_query = batch_query.filter(or_(
                                order_ts < batch_ts,
                                and_(order_ts == batch_ts, DbChatMessage.id < batch_anchor.id),
                            ))
                        fetched = (
                            batch_query.order_by(order_ts.desc(), DbChatMessage.id.desc())
                            .limit(batch_size + 1).all()
                        )
                        if not fetched:
                            break
                        batch = fetched[:batch_size]
                        more_raw = len(fetched) > batch_size
                        reached_limit = False
                        for index, row in enumerate(batch):
                            entry = _db_history_entry(row)
                            units = _history_rendered_units(entry)
                            # Never split one durable assistant row: its
                            # timeline/reasoning metadata is one reconciliation
                            # unit. Stop before an older row if adding it would
                            # exceed the requested visible-bubble budget.
                            if units > 0 and visible_desc and rendered_page_units + units > page_limit:
                                has_more_before = True
                                reached_limit = True
                                break
                            scanned_oldest = row
                            raw_consumed += 1
                            if units > 0:
                                visible_desc.append(entry)
                                rendered_page_units += units
                                if rendered_page_units >= page_limit:
                                    has_more_before = index + 1 < len(batch) or more_raw
                                    reached_limit = True
                                    break
                        if reached_limit:
                            break
                        batch_anchor = scanned_oldest
                        if not more_raw:
                            break

                    history_dict = list(reversed(visible_desc))
                    if scanned_oldest is not None:
                        page_offset = max(0, cursor_before - raw_consumed)
                        next_cursor = _history_cursor(scanned_oldest.id, page_offset) if has_more_before else None
                    else:
                        has_more_before = False
                        page_offset = 0
                        next_cursor = None
                    has_more_after = cursor is not None
                return {
                    "history": history_dict,
                    "history_revision": db_session.updated_at.isoformat() if db_session.updated_at else None,
                    "model": db_session.model,
                    "endpoint_url": db_session.endpoint_url,
                    "name": db_session.name,
                    "offset": page_offset,
                    "limit": page_limit,
                    "total": total,
                    # Visible total follows the renderer, not the number of
                    # SQLite rows. Keep the canonical count additive for old
                    # management/library consumers that still need it.
                    "visible_total": rendered_total,
                    "canonical_visible_total": canonical_visible_total,
                    "rendered_total": rendered_total,
                    "cursor": next_cursor,
                    "next_cursor": next_cursor,
                    "has_more_before": has_more_before,
                    "has_more_after": has_more_after,
                }
            finally:
                db.close()

        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"Session '{session_id}' not found")

        history_dict = []
        for msg in session.history:
            if isinstance(msg, ChatMessage):
                # Skip hidden messages (e.g. compaction summaries for AI context)
                if msg.metadata and msg.metadata.get("hidden"):
                    continue
                entry = {"role": msg.role, "content": _history_display_content(msg.content)}
                if msg.metadata:
                    entry["metadata"] = msg.metadata
                history_dict.append(entry)
            elif isinstance(msg, dict):
                if msg.get("metadata", {}).get("hidden"):
                    continue
                entry = {
                    "role": msg.get("role", ""),
                    "content": _history_display_content(msg.get("content", "")),
                }
                if msg.get("metadata"):
                    entry["metadata"] = msg["metadata"]
                history_dict.append(entry)

        # Fallback: load from DB if in-memory renders empty. Display only —
        # get_session above is the hydration seam, so nothing here writes back
        # into session.history — rebuilding it from raw rows would overwrite
        # parsed multimodal content and the _db_id edit/delete keys it just set.
        if not history_dict:
            db = SessionLocal()
            try:
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id)
                    .order_by(DbChatMessage.timestamp)
                    .all()
                )
                # Response excludes hidden messages, matching the in-memory path.
                history_dict = [
                    entry for entry in (_db_history_entry(m) for m in db_messages)
                    if not (entry.get("metadata") or {}).get("hidden")
                ]
            except Exception as e:
                logger.error(f"DB fallback failed for {session_id}: {e}")
            finally:
                db.close()

        return {
            "history": history_dict,
            "model": session.model,
            "endpoint_url": session.endpoint_url,
            "name": session.name,
            "total": len(history_dict),
            "visible_total": len(history_dict),
        }

    @router.post("/api/session/{session_id}/truncate")
    async def truncate_session(request: Request, session_id: str):
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            keep_count = body.get("keep_count", 0)
            result = session_manager.truncate_messages(session_id, keep_count)
            return {"status": "ok", "kept": keep_count, "truncated": result}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Truncate error {session_id}: {e}")
            raise HTTPException(500, "Truncate failed")

    @router.post("/api/session/{session_id}/message")
    async def add_message(request: Request, session_id: str):
        """Add a message to a session (for slash command persistence)."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            role = body.get("role", "assistant")
            content = body.get("content", "")
            if not content:
                raise HTTPException(400, "content is required")
            metadata = sanitize_client_message_metadata(body.get("metadata"))
            _reserve_message_uploads(request, content, metadata)
            msg = ChatMessage(role=role, content=content, metadata=metadata)
            session_manager.add_message(session_id, msg)
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    @router.post("/api/session/{session_id}/delete-messages")
    async def delete_messages(request: Request, session_id: str):
        """Delete specific messages by DB ID (or legacy index)."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            msg_ids = body.get("msg_ids", [])
            indices = body.get("indices")  # legacy fallback

            session = session_manager.get_session(session_id)
            db = SessionLocal()
            try:
                if msg_ids:
                    # New ID-based delete
                    deleted = 0
                    for mid in msg_ids:
                        db_msg = db.query(DbChatMessage).filter(
                            DbChatMessage.id == mid,
                            DbChatMessage.session_id == session_id,
                        ).first()
                        if db_msg:
                            db.delete(db_msg)
                            deleted += 1

                    # Remove from in-memory history by matching _db_id
                    def _get_db_id(m):
                        meta = m.metadata if isinstance(m, ChatMessage) else (m.get('metadata') if isinstance(m, dict) else None)
                        return meta.get('_db_id') if isinstance(meta, dict) else None
                    session.history = [m for m in session.history if _get_db_id(m) not in msg_ids]
                elif indices:
                    # Legacy index-based delete
                    indices = sorted(indices, reverse=True)
                    db_messages = db.query(DbChatMessage).filter(
                        DbChatMessage.session_id == session_id
                    ).order_by(DbChatMessage.timestamp).all()

                    deleted = 0
                    for idx in indices:
                        if 0 <= idx < len(db_messages):
                            db.delete(db_messages[idx])
                            deleted += 1
                        if 0 <= idx < len(session.history):
                            session.history.pop(idx)
                else:
                    return {"status": "ok", "deleted": 0}

                session.message_count = len(session.history)
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session:
                    db_session.message_count = len(session.history)
                    from datetime import datetime, timezone
                    db_session.updated_at = datetime.now(timezone.utc)

                db.commit()
                return {"status": "ok", "deleted": deleted}
            finally:
                db.close()
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Delete messages error {session_id}: {e}")
            raise HTTPException(500, "Message deletion failed")

    @router.post("/api/session/{session_id}/edit-message")
    async def edit_message(request: Request, session_id: str):
        """Edit the content of a message by its database ID."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            msg_id = body.get("msg_id")
            content = body.get("content")
            if not msg_id or content is None:
                raise HTTPException(400, "msg_id and content are required")

            _reserve_message_uploads(request, content)

            session = session_manager.get_session(session_id)
            db = SessionLocal()
            try:
                db_msg = db.query(DbChatMessage).filter(
                    DbChatMessage.id == msg_id,
                    DbChatMessage.session_id == session_id,
                ).first()
                if not db_msg:
                    raise HTTPException(404, "Message not found")

                db_msg.content = content
                meta = {}
                if db_msg.meta_data:
                    try: meta = json.loads(db_msg.meta_data)
                    except (json.JSONDecodeError, ValueError): pass
                meta['edited'] = True
                db_msg.meta_data = json.dumps(meta)

                # Update in-memory history by matching _db_id
                for hmsg in session.history:
                    hmeta = hmsg.metadata if isinstance(hmsg, ChatMessage) else hmsg.get('metadata')
                    if isinstance(hmeta, dict) and hmeta.get('_db_id') == msg_id:
                        if isinstance(hmsg, ChatMessage):
                            hmsg.content = content
                            hmsg.metadata['edited'] = True
                        elif isinstance(hmsg, dict):
                            hmsg['content'] = content
                            hmsg['metadata']['edited'] = True
                        break

                db.commit()
                return {"status": "ok"}
            finally:
                db.close()
        except KeyError:
            raise HTTPException(404, "Session not found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Edit message error {session_id}: {e}")
            raise HTTPException(500, "Message edit failed")

    @router.post("/api/session/{session_id}/mark-stopped")
    async def mark_stopped(request: Request, session_id: str):
        """Mark the last assistant message as stopped by user."""
        _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
            # Find last assistant message and add stopped metadata
            for msg in reversed(session.history):
                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                    if isinstance(msg, ChatMessage):
                        if not msg.metadata:
                            msg.metadata = {}
                        msg.metadata['stopped'] = True
                        if not msg.metadata.get('model'):
                            msg.metadata['model'] = session.model
                    else:
                        if 'metadata' not in msg:
                            msg['metadata'] = {}
                        msg['metadata']['stopped'] = True
                        if not msg['metadata'].get('model'):
                            msg['metadata']['model'] = session.model
                    break
            # Also update in DB
            db = SessionLocal()
            try:
                import json as _json
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, DbChatMessage.role == 'assistant')
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                if db_messages:
                    meta = {}
                    if db_messages.meta_data:
                        try:
                            meta = _json.loads(db_messages.meta_data)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    meta['stopped'] = True
                    if not meta.get('model'):
                        meta['model'] = session.model
                    db_messages.meta_data = _json.dumps(meta)
                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Mark stopped error {session_id}: {e}")
            raise HTTPException(500, "Could not mark the message as stopped")

    @router.post("/api/session/{session_id}/update-last-meta")
    async def update_last_meta(request: Request, session_id: str):
        """Merge metadata into the last assistant message (e.g. save variants)."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            meta_update = body.get("metadata", {})
            session = session_manager.get_session(session_id)

            # Update in-memory
            for msg in reversed(session.history):
                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                    if isinstance(msg, ChatMessage):
                        if not msg.metadata:
                            msg.metadata = {}
                        msg.metadata.update(meta_update)
                    else:
                        if 'metadata' not in msg:
                            msg['metadata'] = {}
                        msg['metadata'].update(meta_update)
                    break

            # Update in DB
            db = SessionLocal()
            try:
                import json as _json
                db_msg = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, DbChatMessage.role == 'assistant')
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                if db_msg:
                    meta = {}
                    if db_msg.meta_data:
                        try: meta = _json.loads(db_msg.meta_data)
                        except (json.JSONDecodeError, ValueError): pass
                    meta.update(meta_update)
                    db_msg.meta_data = _json.dumps(meta)
                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Update last meta error {session_id}: {e}")
            raise HTTPException(500, "Could not update message metadata")

    @router.post("/api/session/{session_id}/merge-last-assistant")
    async def merge_last_assistant(request: Request, session_id: str):
        """Merge the last two assistant messages into one (for continue)."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            separator = body.get("separator", "\n\n")
            session = session_manager.get_session(session_id)

            # Find last two assistant messages in-memory
            ai_indices = []
            for i, msg in enumerate(session.history):
                role = msg.role if isinstance(msg, ChatMessage) else msg.get('role', '')
                if role == 'assistant':
                    ai_indices.append(i)

            if len(ai_indices) < 2:
                return {"status": "ok", "merged": False}

            idx1, idx2 = ai_indices[-2], ai_indices[-1]
            msg1, msg2 = session.history[idx1], session.history[idx2]

            content1 = msg1.content if isinstance(msg1, ChatMessage) else msg1.get('content', '')
            content2 = msg2.content if isinstance(msg2, ChatMessage) else msg2.get('content', '')
            merged_content = content1 + separator + content2

            # Merge metadata
            meta1 = (msg1.metadata if isinstance(msg1, ChatMessage) else msg1.get('metadata')) or {}
            meta2 = (msg2.metadata if isinstance(msg2, ChatMessage) else msg2.get('metadata')) or {}
            merged_meta = {**meta1, **meta2}
            merged_meta.pop('stopped', None)  # no longer stopped after continue

            # Update first message, remove second
            if isinstance(msg1, ChatMessage):
                msg1.content = merged_content
                msg1.metadata = merged_meta
            else:
                msg1['content'] = merged_content
                msg1['metadata'] = merged_meta

            # Also remove the hidden "continue" user message between them if present
            # It's the message at idx2-1 if it's a user message with continue text
            remove_indices = [idx2]
            if idx2 - 1 > idx1:
                between = session.history[idx2 - 1]
                between_role = between.role if isinstance(between, ChatMessage) else between.get('role', '')
                between_content = between.content if isinstance(between, ChatMessage) else between.get('content', '')
                if between_role == 'user' and 'previous response was interrupted' in between_content:
                    remove_indices.insert(0, idx2 - 1)

            for ri in sorted(remove_indices, reverse=True):
                session.history.pop(ri)

            # Update DB
            db = SessionLocal()
            try:
                import json as _json
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id)
                    .order_by(DbChatMessage.timestamp)
                    .all()
                )
                # Find last two assistant messages in DB
                ai_db = [(i, m) for i, m in enumerate(db_messages) if m.role == 'assistant']
                if len(ai_db) >= 2:
                    (_, db1), (_, db2) = ai_db[-2], ai_db[-1]
                    db1.content = merged_content
                    db1.meta_data = _json.dumps(merged_meta)

                    # Mirror the in-memory deletion: remove the second assistant
                    # message and ONLY the "continue" user message between them
                    # (not arbitrary tool/system/user rows). The old
                    # range-delete destroyed every row between the two assistant
                    # messages, desyncing the DB from the in-memory history.
                    for _row in _merge_continue_rows_to_delete(db_messages, db1, db2):
                        db.delete(_row)

                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok", "merged": True}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Merge assistant error {session_id}: {e}")
            raise HTTPException(500, "Could not merge assistant messages")

    @router.post("/api/session/{session_id}/fork")
    async def fork_session(request: Request, session_id: str):
        """Create a new session with messages copied up to keep_count."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            keep_count = body.get("keep_count", 0)

            # Get the source session. keep_count indexes into source.history,
            # so this must go through get_session — reading the cache directly
            # forks an empty transcript out of a metadata-only session after a
            # restart (display pagination no longer hydrates it).
            try:
                source = session_manager.get_session(session_id)
            except KeyError:
                raise HTTPException(404, "Session not found")
            if not source:
                raise HTTPException(404, "Session not found")

            # Create new session
            new_id = str(uuid.uuid4())
            fork_name = f"\u2ADD {source.name}"
            new_session = session_manager.create_session(
                session_id=new_id,
                name=fork_name,
                endpoint_url=source.endpoint_url,
                model=source.model,
                rag=False,
                owner=getattr(source, 'owner', None),
            )

            # Copy messages up to keep_count
            msgs_to_copy = source.history[:keep_count]
            for msg in msgs_to_copy:
                # Copy the metadata dict. Sharing it would let the fork's
                # persistence (add_message -> _persist_message stamps
                # _db_id/timestamp onto the dict) mutate the SOURCE session's
                # in-memory messages, corrupting their _db_id and breaking
                # edit/delete-by-id on the original conversation.
                meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else None
                new_session.add_message(ChatMessage(msg.role, msg.content, meta))
            try:
                from src.event_bus import fire_event
                fire_event("session_created", getattr(source, 'owner', None))
            except Exception:
                logger.debug("session_created event dispatch failed", exc_info=True)

            return {
                "status": "ok",
                "id": new_id,
                "name": fork_name,
                "kept": len(msgs_to_copy),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Fork error {session_id}: {e}")
            raise HTTPException(500, "Could not fork the session")

    @router.get("/api/conversations/topics")
    async def get_conversation_topics(request: Request) -> Dict[str, Any]:
        from src.auth_helpers import require_user
        user = require_user(request)
        try:
            return analyze_topics(session_manager, owner=user or None)
        except Exception as e:
            logger.error("Topic analysis failed", exc_info=True)
            raise HTTPException(500, "Topic analysis failed")

    @router.get("/api/session/{session_id}/context")
    async def get_session_context_usage(request: Request, session_id: str) -> Dict[str, Any]:
        """Report working request occupancy separately from saved-chat estimates."""
        _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")

        try:
            from src.model_context import estimate_tokens, get_context_length
            from src.agent_runs import get_context_usage, is_active
            from src.agent_context import context_endpoint_key

            messages = session.get_context_messages()
            working_used = int(estimate_tokens(messages))
            # The canonical transcript estimate is deliberately separate from
            # the compacted working ledger.  Calling the latter "stored chat"
            # made a successful compaction appear to shrink saved history.
            stored_messages = [
                {"role": _message_role(message), "content": _message_text(message)}
                for message in session.history
            ]
            stored_used = int(estimate_tokens(stored_messages))
            active = is_active(session_id)
            # Include the just-terminal detached run.  Its exact request
            # ledger is still authoritative during approval/Stop/error
            # persistence; falling back immediately to stored-chat tokens can
            # falsely jump from e.g. 40% to 7% without compaction.
            snapshot = get_context_usage(session_id, include_terminal=True)
            if snapshot and not _context_route_matches(snapshot, session):
                snapshot = None
            status = ("active_request" if active else "last_request") if snapshot else "stored_chat"
            if not active and snapshot is None:
                snapshot = _last_request_context(session)
                if snapshot:
                    status = "last_request"
            checkpoint = getattr(session, "context_checkpoint", None)
            checkpoint_meta = getattr(checkpoint, "metadata", None) or {}
            backend_snapshot = dict(snapshot) if snapshot else None

            def parsed_time(value):
                try:
                    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
                    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
                except (TypeError, ValueError):
                    return None

            checkpoint_at = parsed_time(checkpoint_meta.get("timestamp"))
            snapshot_at = parsed_time((snapshot or {}).get("_recorded_at"))
            checkpoint_revision = int(checkpoint_meta.get("context_revision") or 0)
            snapshot_revision = int((snapshot or {}).get("context_revision") or 0)
            checkpoint_is_newer = bool(
                not active and checkpoint is not None and (
                    snapshot is None
                    or checkpoint_revision > snapshot_revision
                    or (checkpoint_at is not None and snapshot_at is not None and checkpoint_at >= snapshot_at)
                )
            )
            if checkpoint_is_newer:
                snapshot = None
                status = "working_checkpoint"
            elif snapshot is None and checkpoint is not None:
                status = "working_checkpoint"
            used = snapshot["used_tokens"] if snapshot else working_used
            # A completed request's window is historical. The selected local
            # model may have been reloaded with a different serving window,
            # even while retaining the same model id.
            current_window = int(get_context_length(session.endpoint_url, session.model) or 0)
            ctx_len = snapshot["context_length"] if active and snapshot else current_window
            pct = round((used / ctx_len) * 100, 1) if ctx_len else 0.0
            pct = max(0.0, min(100.0, pct))
            visible_messages = sum(
                1 for m in session.history
                if not (getattr(m, "metadata", None) or {}).get("hidden")
            )
            compacted_messages = sum(
                1 for m in session.history
                if (getattr(m, "metadata", None) or {}).get("compacted")
            )
            can_compact = stored_used > 0 and not active
            # Keep the observed request separate from newly saved settings. A
            # settings edit cannot retroactively change an in-flight snapshot.
            from src.context_policy import ContextPolicy
            from src.context_policy_runtime import enabled as context_policy_enabled, owner_policy
            saved_policy = None
            effective_policy = None
            policy_error = False
            try:
                record = owner_policy(getattr(session, 'owner', None), session_id=session_id)
                if record:
                    effective_policy = record['effective']
                    saved_policy = {
                        'auto_compact': record['effective']['auto_compact'],
                        'trigger_percent': record['effective']['trigger_percent'],
                        'target_percent': record['effective']['target_percent'],
                        'revisions': record['revisions'], 'threshold_basis': 'input_budget',
                    }
                elif context_policy_enabled():
                    effective_policy = ContextPolicy().to_dict()
            except ValueError:
                policy_error = True
            observed_threshold = snapshot.get("auto_compact_threshold") if snapshot else None
            observed_enabled = snapshot.get("auto_compact_enabled") if snapshot else None
            if active:
                display_threshold = (
                    observed_threshold
                    if observed_threshold is not None
                    else effective_policy.get("trigger_percent") if effective_policy else None
                )
                display_enabled = (
                    observed_enabled
                    if observed_enabled is not None
                    else effective_policy.get("auto_compact") if effective_policy else None
                )
            else:
                display_threshold = effective_policy.get("trigger_percent") if effective_policy else observed_threshold
                display_enabled = effective_policy.get("auto_compact") if effective_policy else observed_enabled
            if display_threshold is None:
                display_threshold = ContextPolicy().trigger_percent
            if display_enabled is None:
                display_enabled = True
            effective_trigger_tokens = None
            effective_trigger_percent = None
            if effective_policy and ctx_len:
                try:
                    budget = ContextPolicy.from_dict(effective_policy).budget(ctx_len)
                    effective_trigger_tokens = budget.trigger_messages
                    effective_trigger_percent = round(100 * effective_trigger_tokens / ctx_len, 1)
                except ValueError:
                    policy_error = True
            if effective_trigger_tokens is None and ctx_len:
                effective_trigger_tokens = int(ctx_len * float(display_threshold) / 100)
                effective_trigger_percent = float(display_threshold)
            return {
                "session_id": session_id,
                "model": session.model,
                "endpoint_url": session.endpoint_url,
                "current_endpoint_key": context_endpoint_key(session.endpoint_url),
                "used_tokens": used,
                "context_length": ctx_len,
                "context_percent": pct,
                "source": snapshot["source"] if snapshot else "estimated",
                "context_status": status,
                "active_run": active,
                "stored_chat_tokens": stored_used,
                "prompt_tokens": snapshot.get("prompt_tokens") if snapshot else None,
                "round": snapshot.get("round") if snapshot else None,
                "compactions": snapshot.get("compactions", 0) if snapshot else int(
                    checkpoint_meta.get("context_generation")
                    or (int((backend_snapshot or {}).get("compactions", 0) or 0) + (1 if checkpoint_is_newer else 0))
                ),
                "context_revision": snapshot.get("context_revision") if snapshot else (
                    checkpoint_revision or (snapshot_revision + 1 if checkpoint_is_newer else None)
                ),
                "context_reason": snapshot.get("context_reason") if snapshot else checkpoint_meta.get("context_reason"),
                "compaction_revision": checkpoint_meta.get("compaction_revision"),
                "ledger_hash": snapshot.get("ledger_hash") if snapshot else checkpoint_meta.get("ledger_hash"),
                "working_checkpoint": {
                    "used_tokens": working_used,
                    "context_length": ctx_len,
                    "context_percent": round((working_used / ctx_len) * 100, 1) if ctx_len else 0.0,
                    "compaction_revision": checkpoint_meta.get("compaction_revision"),
                    "context_revision": checkpoint_revision or None,
                    "ledger_hash": checkpoint_meta.get("ledger_hash"),
                    "reason": checkpoint_meta.get("context_reason"),
                    "created_at": checkpoint_meta.get("timestamp"),
                } if checkpoint is not None else None,
                "backend_measurement": {
                    "used_tokens": backend_snapshot.get("used_tokens"),
                    "context_length": backend_snapshot.get("context_length"),
                    "context_percent": backend_snapshot.get("context_percent"),
                    "source": backend_snapshot.get("source"),
                    "context_revision": backend_snapshot.get("context_revision"),
                    "reason": backend_snapshot.get("context_reason"),
                    "recorded_at": backend_snapshot.get("_recorded_at"),
                } if backend_snapshot else None,
                "messages": visible_messages,
                "context_messages": len(messages),
                "compacted_messages": compacted_messages,
                "can_compact": can_compact,
                "should_compact": bool(display_enabled and effective_trigger_tokens is not None
                                       and used >= effective_trigger_tokens),
                # While idle, show the policy that will shape the *next*
                # request. Preserve the last observed request separately so a
                # settings edit never rewrites historical telemetry.
                "auto_compact_threshold": display_threshold,
                "configured_auto_compact_threshold": (
                    effective_policy.get("trigger_percent") if effective_policy else display_threshold
                ),
                "effective_auto_compact_threshold": (
                    effective_trigger_percent if effective_trigger_percent is not None else display_threshold
                ),
                "effective_auto_compact_trigger_tokens": effective_trigger_tokens,
                "threshold_basis": "usable_input" if effective_policy else "model_window",
                "auto_compact_enabled": display_enabled,
                "observed_auto_compact_threshold": observed_threshold,
                "observed_auto_compact_enabled": observed_enabled,
                "effective_context_policy": effective_policy,
                "saved_context_policy": saved_policy,
                "context_policy_error": policy_error,
            }
        except Exception as e:
            logger.error(f"Context usage error {session_id}: {e}")
            raise HTTPException(500, "Context usage unavailable")

    return router
