"""Fail-closed transcript coverage for reusing a completed agent's working ledger.

The source is captured before generation. A terminal seal covers exactly that
prefix and the assistant row identified by message_saved, never a guessed latest
assistant. Any concurrent transcript write or legacy compaction invalidates it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import struct


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _legacy(session):
    checkpoint = session.context_checkpoint
    if hasattr(checkpoint, "to_dict"):
        checkpoint = checkpoint.to_dict()
    return _hash([checkpoint, int(session.context_checkpoint_count or 0)])


def _rows_hash(rows):
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps([row.id, row.role, row.content, row.meta_data, row.timestamp],
                             ensure_ascii=False, separators=(",", ":"), default=str).encode()
        # Length framing prevents ambiguous boundaries without materializing
        # another full-transcript list and serialized string on large chats.
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _read(db, session_id, owner):
    from core.database import ChatMessage, Session
    session = db.query(Session).filter_by(id=session_id).first()
    if session is None or (session.owner or None) != (owner or None):
        return None, []
    rows = db.query(ChatMessage).filter_by(session_id=session_id).order_by(
        ChatMessage.timestamp, ChatMessage.id).all()
    return session, rows


def _live_matches(session, rows):
    """DB IDs bind live multimodal messages to their media-stripped DB rows."""
    from src.attachment_refs import persistable_message_content
    history = session.history
    if len(history) != len(rows):
        return False
    return all(
        (message.metadata or {}).get("_db_id") == row.id
        and message.role == row.role
        and persistable_message_content(message.content, message.metadata) == row.content
        for message, row in zip(history, rows)
    )


def capture_source(session, owner):
    """Return a compact pre-generation proof, or None for ambiguous history."""
    from core.database import SessionLocal
    if (session.owner or None) != (owner or None):
        return None
    with SessionLocal() as db:
        stored, rows = _read(db, session.id, owner)
        if (stored is None or not rows or not _live_matches(session, rows)
                or _legacy(stored) != _legacy(session)):
            return None
        # Legacy summaries use system role, which the working-ledger serializer
        # intentionally omits. Do not claim that omitted evidence is covered.
        if session.context_checkpoint is not None and session.context_checkpoint_count:
            return None
        return {"version": 1, "owner": owner or None, "session_id": session.id,
                "source_count": len(rows), "source_hash": _rows_hash(rows),
                "legacy_hash": _legacy(stored)}


def seal_checkpoint(source, *, run_id, session_id, owner, saved_message_id, checkpoint):
    """Seal only a fresh completed ledger with one exact terminal DB anchor."""
    from core.database import ChatRunState, SessionLocal
    if (not isinstance(source, dict) or source.get("version") != 1
            or source.get("run_id") != run_id or source.get("session_id") != session_id
            or source.get("owner") != (owner or None) or not saved_message_id
            or not isinstance(checkpoint, dict) or not checkpoint.get("messages")):
        return None
    with SessionLocal() as db:
        stored, rows = _read(db, session_id, owner)
        run = db.query(ChatRunState).filter_by(run_id=run_id, session_id=session_id,
            owner=owner or "__odysseus_single_user__").first()
        count = source.get("source_count")
        if (stored is None or run is None or type(count) is not int or count < 1
                or len(rows) != count + 1 or rows[-1].id != saved_message_id
                or rows[-1].role != "assistant"
                or _rows_hash(rows[:count]) != source.get("source_hash")
                or _legacy(stored) != source.get("legacy_hash")):
            return None
        anchor_metadata = json.loads(rows[-1].meta_data or "{}")
        if (anchor_metadata.get("timeline_v2") or {}).get("run_id") != run_id:
            return None
        return {**source, "anchor_id": saved_message_id, "covered_count": len(rows),
                "prefix_hash": _rows_hash(rows),
                "checkpoint_hash": _hash(checkpoint["messages"])}


def restore_messages(session, owner, run_id, checkpoint):
    """Validate coverage and return ledger plus chronological live suffix.

    The current request's images remain in the live suffix; DB content is used
    only for identity/integrity checks. Slash chatter follows get_context_messages.
    """
    from core.database import SessionLocal
    if (session.owner or None) != (owner or None) or not isinstance(checkpoint, dict):
        return None
    seal = checkpoint.get("coverage")
    ledger = checkpoint.get("messages")
    if (not isinstance(seal, dict) or seal.get("version") != 1
            or seal.get("run_id") != run_id or seal.get("session_id") != session.id
            or seal.get("owner") != (owner or None) or not isinstance(ledger, list)
            or not ledger or _hash(ledger) != seal.get("checkpoint_hash")):
        return None
    with SessionLocal() as db:
        stored, rows = _read(db, session.id, owner)
        count = seal.get("covered_count")
        if (stored is None or type(count) is not int or count < 1 or len(rows) < count
                or rows[count - 1].id != seal.get("anchor_id")
                or rows[count - 1].role != "assistant"
                or _rows_hash(rows[:count]) != seal.get("prefix_hash")
                or _legacy(stored) != seal.get("legacy_hash")
                or _legacy(session) != seal.get("legacy_hash")
                or not _live_matches(session, rows)):
            return None
        suffix_ids = [row.id for row in rows[count:]
                      if (json.loads(row.meta_data or "{}") or {}).get("source") != "slash"]
        suffix_set = set(suffix_ids)
        suffix = [item for item in session.get_context_messages()
                  if (item.get("metadata") or {}).get("_db_id") in suffix_set]
        if [(item.get("metadata") or {}).get("_db_id") for item in suffix] != suffix_ids:
            return None
    # Project memory and skills are request-scoped, rebuilt by chat_routes.
    ledger = [item for item in ledger if isinstance(item, dict)
              and item.get("role") in {"user", "assistant", "tool"}
              and (item.get("metadata") or {}).get("source") != "project memory and skills"]
    return copy.deepcopy(ledger + suffix)


def restore_unsealed_terminal_goal(session, owner):
    """Bridge a fresh, completed Goal ledger into the next ordinary Agent run.

    Old Goal attempts can have a durable model-visible checkpoint but no
    ordinary transcript coverage seal. Never infer coverage merely from the
    existence of that checkpoint: require a checkpoint emitted by the exact
    terminal Goal run, its matching saved assistant anchor, and an unchanged
    owner-scoped transcript. All messages after that anchor are appended.
    """
    from core.database import ChatRunState, SessionLocal
    if (session.owner or None) != (owner or None):
        return None
    with SessionLocal() as db:
        run = db.query(ChatRunState).filter_by(session_id=session.id).order_by(
            ChatRunState.started_at.desc(), ChatRunState.run_id.desc(),
        ).first()
        if (run is None or run.status != "done"
                or run.owner != (owner or "__odysseus_single_user__")):
            return None
        continuation = run.continuation or {}
        checkpoint = continuation.get("working_checkpoint") or {}
        ledger = checkpoint.get("messages")
        if (continuation.get("goal") is not True
                or not isinstance(ledger, list) or not ledger
                or checkpoint.get("coverage")
                or not checkpoint.get("ledger_hash")
                or hashlib.sha256(json.dumps(
                    ledger, ensure_ascii=False, separators=(",", ":"), default=str,
                ).encode()).hexdigest() != checkpoint["ledger_hash"]):
            return None
        origin = checkpoint.get("checkpoint_run_id")
        if origin != run.run_id:
            if origin is not None:
                return None
            # Pre-marker Goal records can still be proved by their exact
            # owner/session-bound replay frame. The indexed lookup avoids
            # reading a multi-hour token stream into memory.
            try:
                from src import agent_runs
                from src.chat_replay_log import ReplayLog
                if not ReplayLog(agent_runs.replay_root(), run.run_id, session.id).has_context_checkpoint(
                    checkpoint["ledger_hash"], terminal_fresh=True,
                ):
                    return None
            except (OSError, ValueError, TypeError, struct.error):
                return None
        elif continuation.get("checkpoint_terminal_fresh") is not True:
            return None
        stored, rows = _read(db, session.id, owner)
        if (stored is None or not _live_matches(session, rows)
                or _legacy(stored) != _legacy(session)
                or (session.context_checkpoint is not None and session.context_checkpoint_count)):
            return None
        anchors = []
        for index, row in enumerate(rows):
            if row.role != "assistant":
                continue
            try:
                metadata = json.loads(row.meta_data or "{}")
            except (TypeError, ValueError):
                return None
            if not isinstance(metadata, dict):
                return None
            timeline = metadata.get("timeline_v2") or {}
            if timeline.get("run_id") == run.run_id and timeline.get("status") == "done":
                anchors.append((index, row.id))
        if len(anchors) != 1:
            return None
        anchor_index, anchor_id = anchors[0]
        tail_ids = []
        for row in rows[anchor_index:]:
            try:
                metadata = json.loads(row.meta_data or "{}")
            except (TypeError, ValueError):
                return None
            if not isinstance(metadata, dict):
                return None
            if metadata.get("source") != "slash":
                tail_ids.append(row.id)
        tail_set = set(tail_ids)
        tail = [item for item in session.get_context_messages()
                if (item.get("metadata") or {}).get("_db_id") in tail_set]
        if (not tail or (tail[0].get("metadata") or {}).get("_db_id") != anchor_id
                or [(item.get("metadata") or {}).get("_db_id") for item in tail] != tail_ids):
            return None
    ledger = [item for item in ledger if isinstance(item, dict)
              and item.get("role") in {"user", "assistant", "tool"}
              and (item.get("metadata") or {}).get("source") != "project memory and skills"]
    if ledger and ledger[-1].get("role") == "assistant" and ledger[-1].get("content") == tail[0].get("content"):
        tail = tail[1:]
    return copy.deepcopy(ledger + tail)
