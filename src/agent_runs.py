"""Detached agent-run manager.

Keeps an agent/chat stream running server-side after the SSE client disconnects
(tab close, navigate away, refresh). The streaming generator is drained by a
background asyncio task into a per-session replay buffer; SSE clients SUBSCRIBE
to that buffer (replay everything so far, then live). Closing the SSE only drops
the subscriber — the drain task keeps going.

The wrapped generator already persists the assistant message to the session on
completion, so reopening the session shows the finished result even if nobody
was connected when it finished. Reconnecting mid-run replays the buffer + streams
live (pick up where it is).

With ODYSSEUS_DURABLE_CHAT_REPLAY=1, events are disk-backed with bounded storage.
Replay survives process restart, execution does not: unfinished runs are exposed
as interrupted and their commands are never replayed automatically.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
import uuid
from typing import AsyncGenerator, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def replay_root():
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, 'chat-replay')


def delete_replays_for_session(session_id: str) -> int:
    """Delete only replay artifacts whose sidecar hashes this session."""
    try:
        from src.chat_replay_log import _key
        root = replay_root()
        removed = 0
        for sidecar in Path(root).glob("*.json"):
            try:
                meta = json.loads(sidecar.read_text())
                if meta.get("session_hash") != _key(str(session_id)):
                    continue
                run_id = sidecar.stem
                if not re.fullmatch(r"[0-9a-f]{32}", run_id):
                    continue
                for suffix in (".events", ".index", ".json"):
                    sidecar.with_suffix(suffix).unlink(missing_ok=True)
                    removed += 1
            except (OSError, TypeError, ValueError):
                continue
        return removed
    except Exception:
        logger.warning("[agent-run] replay cleanup failed for deleted session", exc_info=True)
        return 0


class _Run:
    __slots__ = (
        "buffer", "subscribers", "status", "task", "evict_task", "run_id",
        "context_usage", "started_at", "round", "segment", "tool_counter",
        "active_tool_call_id", "on_terminal", "context_revision", "terminal_status",
        "owner", "session_id", "continuation", "durable_seq", "ledger_hash", "terminal_at",
        "compaction_pending",
    )

    def __init__(self) -> None:
        self.buffer: list = []          # ordered SSE event strings (replay log)
        self.subscribers: set = set()   # one asyncio.Queue per connected client
        self.status: str = "running"    # running | done | error | stopped
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None
        # Stable across every subscription/replay of this exact detached run.
        # The browser uses it to make local cost accounting replay-idempotent.
        self.run_id: str = uuid.uuid4().hex
        self.context_usage: Optional[dict] = None
        self.started_at: float = time.time()
        self.round: int = 0
        self.segment: int = 0
        self.tool_counter: int = 0
        self.active_tool_call_id: Optional[str] = None
        self.on_terminal: Optional[Callable[[str], Awaitable[None]]] = None
        # Monotonic request-context ledger.  Reconnect/approval telemetry must
        # not replace a newer measurement with a smaller stored-chat estimate.
        self.context_revision: int = 0
        self.terminal_status: Optional[str] = None
        self.owner: Optional[str] = None
        self.session_id: Optional[str] = None
        self.continuation: dict = {}
        self.durable_seq: int = -1
        self.ledger_hash: Optional[str] = None
        self.terminal_at: Optional[float] = None
        self.compaction_pending: bool = False


_RUNS: Dict[str, _Run] = {}

# Auth-disabled/single-user deployments still need a durable context ledger so
# Stop, approval and Goal continuation cannot fall back to a tiny stored-chat
# estimate. Keep the DB owner column non-null without inventing an account that
# could collide with an authenticated owner.
_SINGLE_USER_OWNER_KEY = "__odysseus_single_user__"


def _storage_owner(run: _Run) -> str:
    return str(run.owner or _SINGLE_USER_OWNER_KEY)

# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180


def _ledger_hash(run: _Run) -> str:
    """Hash the latest model-visible context snapshot without storing secrets."""
    payload = {
        "run_id": run.run_id,
        "context_revision": run.context_revision,
        "context_usage": run.context_usage or {},
        "last_seq": len(run.buffer) - 1,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _persist_run_state(run: _Run, *, status: Optional[str] = None, durable: bool = False) -> None:
    """Persist a small owner-scoped run checkpoint; never persist secrets."""
    try:
        from core.database import ChatRunState, SessionLocal, utcnow_naive
        effective_status = status or run.terminal_status or run.status
        run.ledger_hash = _ledger_hash(run)
        run.durable_seq = len(run.buffer) - 1
        with SessionLocal.begin() as db:
            row = db.query(ChatRunState).filter(ChatRunState.run_id == run.run_id).first()
            if row is None:
                row = ChatRunState(
                    run_id=run.run_id,
                    session_id=getattr(run, "session_id", "") or "",
                    owner=_storage_owner(run),
                    status=effective_status,
                    started_at=datetime.utcfromtimestamp(run.started_at),
                    last_seq=len(run.buffer) - 1,
                    durable_seq=run.durable_seq,
                )
                db.add(row)
            row.status = effective_status
            row.last_seq = len(run.buffer) - 1
            row.durable_seq = run.durable_seq if durable else max(row.durable_seq or -1, run.durable_seq)
            row.context_revision = run.context_revision
            row.ledger_hash = run.ledger_hash
            row.context_snapshot = dict(run.context_usage) if run.context_usage else None
            row.continuation = dict(run.continuation or {}) or None
            if effective_status != "running":
                row.terminal_at = utcnow_naive()
                run.terminal_at = row.terminal_at.timestamp()
    except Exception:
        logger.warning("[agent-run] durable run-state checkpoint failed", exc_info=True)


def _seed_context_ledger(run: _Run) -> None:
    """Carry the last model-visible ledger into a replacement attempt.

    Goal continuation, approval continuation, and a normal retry create a new
    run ID.  Seeding from the latest durable row keeps the occupancy high-water
    mark and revision session-scoped, so a transport/Stop boundary cannot look
    like an unexplained compaction.  The next explicit ``compacted`` event is
    allowed to lower it again.
    """
    if not run.session_id:
        return
    try:
        from core.database import ChatRunState, SessionLocal
        with SessionLocal() as db:
            row = db.query(ChatRunState).filter(
                ChatRunState.session_id == run.session_id,
                ChatRunState.owner == _storage_owner(run),
            ).order_by(ChatRunState.updated_at.desc()).first()
            if row is None or not row.context_snapshot:
                return
            run.context_usage = dict(row.context_snapshot)
            run.context_revision = int(row.context_revision or 0)
            run.ledger_hash = row.ledger_hash
    except Exception:
        # The additive table may not exist in a legacy test/development DB;
        # live streaming remains fully functional without the seed.
        logger.debug("[agent-run] context ledger seed unavailable", exc_info=True)


def _annotate_event(run: _Run, ev: str, seq: int) -> str:
    """Add a stable replay identity to JSON SSE frames without changing their type.

    Legacy clients ignore the additive ``_replay`` object.  Plain-text test and
    heartbeat frames remain byte-for-byte compatible.
    """
    lines = ev.splitlines()
    data_indexes = [idx for idx, line in enumerate(lines) if line.startswith("data:")]
    if not data_indexes:
        return ev
    raw = "\n".join(lines[idx][5:].lstrip() for idx in data_indexes)
    if raw == "[DONE]":
        return f"id: {seq}\n" + ev
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ev
    if not isinstance(payload, dict):
        return ev

    event_type = payload.get("type")
    if event_type == "agent_step":
        try:
            run.round = max(0, int(payload.get("round") or run.round + 1))
        except (TypeError, ValueError):
            run.round += 1
        run.segment += 1
        run.active_tool_call_id = None
    elif event_type == "tool_start":
        run.tool_counter += 1
        run.segment += 1
        run.active_tool_call_id = str(payload.get("tool_call_id") or f"tool-{run.tool_counter}")
    elif event_type == "tool_output":
        # Keep the current id for this result; it is cleared after annotation.
        pass
    elif payload.get("delta") and not payload.get("thinking"):
        if run.segment == 0:
            run.segment = 1

    created_at = time.time()
    replay = {
        "run_id": run.run_id,
        "seq": seq,
        "created_at": created_at,
        "started_at": run.started_at,
        "round": run.round,
        "segment_id": f"{run.run_id}:{run.segment}",
    }
    if event_type in ("tool_start", "tool_progress", "tool_output"):
        tool_call_id = str(payload.get("tool_call_id") or run.active_tool_call_id or "")
        if tool_call_id:
            payload["tool_call_id"] = tool_call_id
            replay["tool_call_id"] = tool_call_id
    payload["_replay"] = replay
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prefix = [line for line in lines if not line.startswith("data:") and not line.startswith("id:")]
    rendered = [f"id: {seq}", *prefix, f"data: {encoded}"]
    if event_type == "tool_output":
        run.active_tool_call_id = None
    return "\n".join(rendered) + "\n\n"


def _publish(run: _Run, ev: str) -> None:
    """Append one SSE event and fan it out to every live subscriber."""
    seq = len(run.buffer)
    ev = _annotate_event(run, ev, seq)
    event_type = None
    # Bind measurements to this exact run, not a session lookup: a cancelled
    # predecessor can still publish while its replacement is being started.
    try:
        payload = json.loads("\n".join(
            line[5:].lstrip() for line in ev.splitlines() if line.startswith("data:")
        ))
        if isinstance(payload, dict) and payload.get("type") == "context_usage":
            event_type = "context_usage"
            snapshot = normalize_context_usage(payload.get("data"))
            if snapshot is not None:
                previous = run.context_usage
                previous_compactions = int((previous or {}).get("compactions", 0) or 0)
                current_compactions = int(snapshot.get("compactions", 0) or 0)
                # Keep stale events in the replay log for audit, but do not
                # lower the live high-water mark without a real compaction.
                stale = bool(
                    previous and not run.compaction_pending and (
                        current_compactions < previous_compactions
                        or (
                            current_compactions == previous_compactions
                            and snapshot.get("used_tokens", 0) < previous.get("used_tokens", 0)
                        )
                    )
                )
                if not stale:
                    run.context_revision += 1
                    snapshot["context_revision"] = run.context_revision
                    snapshot["context_reason"] = (
                        "compaction" if run.compaction_pending or current_compactions > previous_compactions else "measurement"
                    )
                    run.context_usage = snapshot
                    run.compaction_pending = False
                else:
                    snapshot = {
                        **snapshot,
                        "context_revision": run.context_revision,
                        "context_reason": "measurement",
                        "stale": True,
                    }
                # Carry the server's accepted revision in replay metadata.  Do
                # not change the legacy ``data`` shape: older clients compare
                #/persist that payload verbatim.  New clients merge these
                # fields before applying the measurement so a stale lower
                # value can never replace the live high-water mark.
                replay_meta = payload.setdefault("_replay", {})
                if isinstance(replay_meta, dict):
                    replay_meta["context_revision"] = run.context_revision
                    replay_meta["context_reason"] = snapshot.get("context_reason", "measurement")
                    if snapshot.get("stale"):
                        replay_meta["stale"] = True
                try:
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    lines = ev.splitlines()
                    data_idx = next(i for i, line in enumerate(lines) if line.startswith("data:"))
                    lines[data_idx] = f"data: {encoded}"
                    ev = "\n".join(lines) + "\n\n"
                except (StopIteration, TypeError, ValueError):
                    pass
        elif isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type == "compacted":
                # The following context_usage frame is the first measurement
                # after an explicit successful compaction and may legitimately
                # be lower than the prior run's high-water mark.
                run.compaction_pending = True
                checkpoint = payload.get("checkpoint")
                if isinstance(checkpoint, dict) and checkpoint.get("summary"):
                    run.continuation["working_checkpoint"] = {
                        "summary": str(checkpoint["summary"])[:120000],
                        "compactions": int(checkpoint.get("compactions") or 0),
                        "ledger_hash": str(checkpoint.get("ledger_hash") or "")[:128],
                    }
            elif event_type == "context_checkpoint" and isinstance(payload.get("messages"), list):
                messages = payload["messages"]
                run.continuation["working_checkpoint"] = {
                    "messages": messages,
                    "compactions": int(payload.get("compactions") or 0),
                    "ledger_hash": str(payload.get("ledger_hash") or "")[:128],
                }
                # The ledger belongs in the protected run-state artifact, not
                # every browser replay page. Keep only audit metadata in SSE.
                payload = {
                    "type": "context_checkpoint",
                    "message_count": len(messages),
                    "ledger_hash": run.continuation["working_checkpoint"]["ledger_hash"],
                    "compactions": run.continuation["working_checkpoint"]["compactions"],
                    "_replay": payload.get("_replay", {}),
                }
                lines = ev.splitlines()
                try:
                    data_idx = next(i for i, line in enumerate(lines) if line.startswith("data:"))
                    lines[data_idx] = "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    ev = "\n".join(lines) + "\n\n"
                except StopIteration:
                    pass
    except (TypeError, ValueError):
        pass  # Other SSE events, comments and [DONE] are not measurements.
    run.buffer.append(ev)
    if event_type in {"context_usage", "context_checkpoint", "compacted", "tool_output", "agent_step", "ask_user", "goal_update", "plan_update"}:
        _persist_run_state(run)
    for q in list(run.subscribers):
        try:
            q.put_nowait(True)
        except Exception:
            pass


def _wake_run_subscribers(run: _Run) -> None:
    """Close subscribers even when the drain task never reached its body."""
    for q in list(run.subscribers):
        try:
            q.put_nowait(True)
        except Exception:
            pass


def _persist_timeline_v2(session_id: str, run: _Run, *, status: Optional[str] = None) -> None:
    """Attach the bounded canonical replay timeline to the saved assistant turn.

    The agent generator persists the assistant message before it returns.  The
    detached runner is therefore the first layer that has both that durable row
    and the fully annotated SSE sequence.  Keep the older round/tool metadata
    untouched so a rollback-compatible build can still render the same turn.
    """
    try:
        from core.database import (
            ChatMessage as DbChatMessage,
            Session as DbSession,
            SessionLocal,
        )

        events = []
        encoded_bytes = 0
        truncated = False
        saved_message_id = None
        # Metadata is bounded, but current work is more important than the
        # oldest deltas. Build from the tail so Stop/error never preserves round
        # one while dropping the tool and reasoning that were active at Stop.
        selected_events = []
        for index in range(len(run.buffer) - 1, max(-1, len(run.buffer) - 5001), -1):
            frame = run.buffer[index]
            raw = "\n".join(
                line[5:].lstrip() for line in frame.splitlines()
                if line.startswith("data:")
            )
            if not raw or raw == "[DONE]":
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "message_saved" and payload.get("id"):
                saved_message_id = str(payload["id"])
            item = {"seq": index, "data": payload}
            size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if encoded_bytes + size > 2 * 1024 * 1024:
                truncated = True
                break
            encoded_bytes += size
            selected_events.append(item)
        events = list(reversed(selected_events))
        if len(run.buffer) > 5000:
            truncated = True
        if not events:
            return

        # A normal completion already carries legacy round/tool metadata. A
        # user Stop can happen before final metrics, so reconstruct enough of
        # that view from the replay log to preserve separate bubbles, thinking
        # and tool cards after reload.
        round_parts = {}
        tool_order = []
        tools = {}
        current_round = 1
        actual_model = ""
        actual_endpoint_id = None
        actual_endpoint_label = None
        round_timestamps = {}
        substantive = False
        for item in events:
            payload = item["data"]
            replay = payload.get("_replay") if isinstance(payload.get("_replay"), dict) else {}
            raw_round = payload.get("round", replay.get("round", current_round))
            try:
                event_round = max(1, int(raw_round or current_round))
            except (TypeError, ValueError):
                event_round = current_round
            event_type = payload.get("type")
            if event_type == "agent_step":
                current_round = event_round
                continue
            current_round = max(current_round, event_round)
            replay_created_at = replay.get("created_at") if isinstance(replay, dict) else None
            if replay_created_at is not None and event_round not in round_timestamps:
                round_timestamps[event_round] = replay_created_at
            if event_type == "model_actual":
                actual_model = str(payload.get("model") or actual_model)
                actual_endpoint_id = payload.get("endpoint_id", actual_endpoint_id)
                actual_endpoint_label = payload.get("endpoint_label", actual_endpoint_label)
            elif event_type == "context_usage" and isinstance(payload.get("data"), dict):
                actual_model = str(payload["data"].get("model") or actual_model)
            if payload.get("delta"):
                substantive = True
                parts = round_parts.setdefault(event_round, {"thinking": [], "text": []})
                channel = payload.get("channel")
                target = "thinking" if payload.get("thinking") is True or channel in {"thinking", "thought"} else "text"
                parts[target].append(str(payload["delta"]))
                continue
            if event_type not in {"tool_start", "tool_progress", "tool_output"}:
                continue
            substantive = True
            tool_id = str(payload.get("tool_call_id") or replay.get("tool_call_id") or f"tool-{len(tool_order) + 1}")
            event = tools.get(tool_id)
            if event is None:
                event = {
                    "round": event_round,
                    "tool": str(payload.get("tool") or "Tool"),
                    "command": str(payload.get("command") or ""),
                    "output": "",
                    "exit_code": None,
                    "tool_call_id": tool_id,
                    "_replay": {"run_id": run.run_id, "seq": item["seq"], "tool_call_id": tool_id},
                }
                tools[tool_id] = event
                tool_order.append(tool_id)
            if payload.get("tool"):
                event["tool"] = str(payload["tool"])
            if payload.get("command"):
                event["command"] = str(payload["command"])
            if event_type == "tool_progress":
                tail = payload.get("tail") or payload.get("message")
                if tail:
                    event["_progress_tail"] = str(tail)[-65536:]
            elif event_type == "tool_output":
                event["output"] = str(payload.get("output") or "")
                event["exit_code"] = payload.get("exit_code")
                for key in (
                    "ask_user", "diff", "image_url", "image_prompt", "image_model",
                    "image_size", "image_quality", "doc_id", "doc_title",
                ):
                    if payload.get(key) is not None:
                        event[key] = payload[key]

        max_round = max([*round_parts.keys(), *(tools[key]["round"] for key in tool_order), 0])
        round_texts = []
        round_reasonings = []
        for round_number in range(1, max_round + 1):
            parts = round_parts.get(round_number, {"thinking": [], "text": []})
            thinking = "".join(parts["thinking"]).strip()
            text = "".join(parts["text"]).strip()
            round_texts.append((f"<think>\n{thinking}\n</think>\n\n" if thinking else "") + text)
            round_reasonings.append(thinking)
        tool_events = []
        for tool_id in tool_order:
            event = tools[tool_id]
            progress_tail = event.pop("_progress_tail", "")
            if (status or run.status) == "stopped" and event.get("exit_code") is None:
                event["exit_code"] = 130
                event["output"] = ((progress_tail + "\n") if progress_tail else "") + "Interrupted by user."
            tool_events.append(event)

        legacy_metadata = {}
        terminal_status = status or run.status
        if terminal_status in {"stopped", "error"}:
            legacy_metadata["stopped"] = terminal_status == "stopped"
            legacy_metadata["cancelled"] = terminal_status == "stopped" and not substantive
            if tool_events:
                legacy_metadata["tool_events"] = tool_events
            if actual_model:
                legacy_metadata["model"] = actual_model
            if actual_endpoint_id:
                legacy_metadata["endpoint_id"] = actual_endpoint_id
            if actual_endpoint_label:
                legacy_metadata["endpoint_label"] = actual_endpoint_label
        # Keep the canonical per-round thinking alongside the legacy text/tool
        # arrays for every terminal status. Older clients only know
        # ``round_texts``; newer renderers use this parallel array to restore
        # non-empty collapsible thinking blocks after reload.
        if round_texts:
            legacy_metadata["round_texts"] = round_texts
            legacy_metadata["round_reasonings"] = round_reasonings
            legacy_metadata["round_timestamps"] = [
                round_timestamps.get(number) for number in range(1, max_round + 1)
            ]

        synced_message_id = None
        synced_metadata = None
        with SessionLocal.begin() as db:
            row = None
            if saved_message_id:
                row = db.query(DbChatMessage).filter(
                    DbChatMessage.id == saved_message_id,
                    DbChatMessage.session_id == session_id,
                    DbChatMessage.role == "assistant",
                ).first()
            if row is None:
                # Cancellation saves cannot yield message_saved. Bind only to
                # an assistant row after this run's latest user turn, never to
                # the previous assistant response.
                run_start = datetime.utcfromtimestamp(run.started_at)
                latest_user = db.query(DbChatMessage).filter(
                    DbChatMessage.session_id == session_id,
                    DbChatMessage.role == "user",
                    DbChatMessage.timestamp <= run_start + timedelta(seconds=5),
                ).order_by(DbChatMessage.timestamp.desc(), DbChatMessage.id.desc()).first()
                if latest_user is not None:
                    row = db.query(DbChatMessage).filter(
                        DbChatMessage.session_id == session_id,
                        DbChatMessage.role == "assistant",
                        DbChatMessage.timestamp >= latest_user.timestamp,
                    ).order_by(DbChatMessage.timestamp.desc(), DbChatMessage.id.desc()).first()
                else:
                    # Compatibility for system-created/test sessions which can
                    # legitimately produce an assistant turn without a user row.
                    row = db.query(DbChatMessage).filter(
                        DbChatMessage.session_id == session_id,
                        DbChatMessage.role == "assistant",
                        DbChatMessage.timestamp >= run_start - timedelta(seconds=5),
                    ).order_by(DbChatMessage.timestamp.desc(), DbChatMessage.id.desc()).first()
            terminal_status_for_row = status or run.status
            if row is None and (
                terminal_status_for_row in {"stopped", "error"}
                or (terminal_status_for_row == "done" and substantive)
            ):
                # Tool-only/reasoning-only stops previously had no assistant DB
                # row. Persist one canonical placeholder for history replay.
                visible = "\n\n".join(
                    "".join(round_parts.get(number, {}).get("text", [])).strip()
                    for number in range(1, max_round + 1)
                    if "".join(round_parts.get(number, {}).get("text", [])).strip()
                )
                row = DbChatMessage(
                    id=str(uuid.uuid4()), session_id=session_id, role="assistant",
                    content=visible, meta_data="{}", timestamp=datetime.utcnow(),
                )
                db.add(row)
                session_row = db.query(DbSession).filter(DbSession.id == session_id).first()
                if session_row is not None:
                    session_row.message_count = int(session_row.message_count or 0) + 1
                    session_row.last_message_at = datetime.utcnow()
            if row is None:
                return
            try:
                metadata = json.loads(row.meta_data or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update(legacy_metadata)
            # Stop, Pause and transport errors are not compaction. Preserve the
            # exact last model-visible occupancy so the idle context endpoint
            # cannot fall back to a smaller transcript estimate after the run
            # becomes terminal. This snapshot is also restored after restart.
            if run.context_usage:
                metadata["working_context"] = {
                    **run.context_usage,
                    "context_revision": run.context_revision,
                    "context_reason": run.context_usage.get("context_reason", "measurement"),
                }
            metadata["timeline_v2"] = {
                "version": 2,
                "run_id": run.run_id,
                "started_at": run.started_at,
                "status": terminal_status,
                "events": events,
                "truncated": truncated,
            }
            row.meta_data = json.dumps(metadata, ensure_ascii=False)
            synced_message_id = str(row.id)
            synced_metadata = dict(metadata)

        # The context API reads the process Session cache. A direct DB metadata
        # update would otherwise remain invisible until a restart/re-hydration,
        # causing a transient percentage drop immediately after Pause.
        if synced_message_id and synced_metadata is not None:
            try:
                from core.models import get_session_manager_instance
                manager = get_session_manager_instance()
                session = manager.get_session(session_id) if manager else None
                if session is not None:
                    for message in reversed(session.history or []):
                        meta = getattr(message, "metadata", None) or {}
                        if str(meta.get("_db_id") or "") != synced_message_id:
                            continue
                        updated = dict(synced_metadata)
                        updated["_db_id"] = synced_message_id
                        message.metadata = updated
                        break
            except Exception:
                logger.warning(
                    "[agent-run] in-memory context snapshot sync failed for %s",
                    session_id,
                    exc_info=True,
                )
    except Exception as exc:
        # Replay remains available from the durable run log and legacy
        # round/tool metadata.  A metadata write failure must not corrupt the
        # already-saved conversation or delay subscriber shutdown.
        logger.warning("[agent-run] timeline_v2 persistence failed for %s: %s", session_id, exc)


def _schedule_evict(session_id: str, expected_run: Optional[_Run] = None) -> None:
    """(Re)arm a grace-period eviction for a terminal run with no subscribers.
    Identity-checked so a run that gets replaced/reused is never evicted by a
    stale timer."""
    run = _RUNS.get(session_id)
    if run is None:
        return
    if expected_run is not None and run is not expected_run:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        cur = _RUNS.get(session_id)
        if cur is run_ref and cur.status != "running" and not cur.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    r = _RUNS.get(session_id)
    return bool(r and r.status == "running")


def get_status(session_id: str) -> Optional[str]:
    r = _RUNS.get(session_id)
    return r.status if r else None


def get_run_id(session_id: str) -> Optional[str]:
    """Return the opaque identity of the current detached run, if present."""
    r = _RUNS.get(session_id)
    return r.run_id if r else None


def get_active_run(session_id: str) -> Optional[_Run]:
    """Return the exact active run currently registered for a session."""
    r = _RUNS.get(session_id)
    return r if r and r.status == "running" else None


def describe_run(session_id: str) -> Optional[dict]:
    """Return the owner-gated route's public snapshot of the current run."""
    run = _RUNS.get(session_id)
    if run is None:
        # A process restart clears the live registry, but the additive run
        # state row still gives the UI an honest interrupted identity/cursor.
        try:
            from core.database import ChatRunState, SessionLocal
            with SessionLocal() as db:
                row = db.query(ChatRunState).filter(
                    ChatRunState.session_id == session_id,
                ).order_by(ChatRunState.updated_at.desc()).first()
                if row is None:
                    return None
                return {
                    "run_id": row.run_id,
                    "status": row.status,
                    "started_at": row.started_at.replace(tzinfo=timezone.utc).timestamp() if row.started_at else None,
                    "last_seq": row.last_seq,
                    "next_seq": (row.last_seq or -1) + 1,
                    "context_usage": dict(row.context_snapshot or {}) or None,
                    "context_revision": row.context_revision or 0,
                    "durable_seq": row.durable_seq,
                    "ledger_hash": row.ledger_hash,
                }
        except Exception:
            logger.debug("[agent-run] durable run-state lookup failed", exc_info=True)
            return None
    return {
        "run_id": run.run_id,
        "status": run.status,
        "started_at": run.started_at,
        "last_seq": len(run.buffer) - 1,
        "next_seq": len(run.buffer),
        "context_usage": dict(run.context_usage) if run.context_usage else None,
        "context_revision": run.context_revision,
        "durable_seq": run.durable_seq,
        "ledger_hash": run.ledger_hash,
    }


def recover_durable_runs() -> list[dict]:
    """Materialize interrupted replay runs after a web-process restart.

    Tool calls are never re-executed.  The persisted frames are attached to
    the canonical assistant turn (including partial tool/reasoning evidence),
    the artifact is checkpointed as interrupted, and callers may then start a
    fresh Goal attempt from the durable session ledger.
    """
    recovered = []
    try:
        from core.database import ChatRunState, SessionLocal, utcnow_naive
        from src.chat_replay_log import ReplayLog
        with SessionLocal() as db:
            rows = [
                {
                    "run_id": row.run_id,
                    "session_id": row.session_id,
                    "owner": None if row.owner == _SINGLE_USER_OWNER_KEY else row.owner,
                    "started_at": row.started_at.replace(tzinfo=timezone.utc).timestamp() if row.started_at else time.time(),
                    "context_revision": int(row.context_revision or 0),
                    "context_snapshot": dict(row.context_snapshot or {}) or None,
                    "continuation": dict(row.continuation or {}),
                }
                for row in db.query(ChatRunState).filter(ChatRunState.status == "running").all()
            ]
        for state in rows:
            try:
                log = ReplayLog(replay_root(), state["run_id"], state["session_id"])
                run = _Run()
                run.run_id = state["run_id"]
                run.session_id = state["session_id"]
                run.owner = state["owner"]
                run.started_at = state["started_at"]
                run.buffer = log
                run.context_revision = state["context_revision"]
                run.context_usage = state["context_snapshot"]
                run.status = "stopped"
                run.terminal_status = "stopped"
                _persist_timeline_v2(state["session_id"], run, status="stopped")
                try:
                    log.checkpoint("stopped")
                except OSError:
                    logger.warning("[agent-run] unable to checkpoint interrupted replay %s", state["run_id"])
                with SessionLocal.begin() as db:
                    row = db.query(ChatRunState).filter(ChatRunState.run_id == state["run_id"]).first()
                    if row is not None:
                        row.status = "interrupted"
                        row.terminal_at = utcnow_naive()
                        row.last_seq = len(log) - 1
                        row.durable_seq = row.last_seq
                recovered.append(state)
            except (FileNotFoundError, ValueError, OSError):
                logger.warning("[agent-run] skipping unavailable interrupted replay %s", state["run_id"])
                with SessionLocal.begin() as db:
                    row = db.query(ChatRunState).filter(ChatRunState.run_id == state["run_id"]).first()
                    if row is not None:
                        row.status = "interrupted"
                        row.terminal_at = utcnow_naive()
    except Exception:
        logger.warning("[agent-run] durable run recovery failed", exc_info=True)
    return recovered


def event_page(session_id: str, *, after_seq: int = -1, limit: int = 100) -> Optional[dict]:
    """Return a bounded JSON snapshot without opening a live SSE subscriber."""
    if type(after_seq) is not int or after_seq < -1 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid replay cursor or page size")
    run = _RUNS.get(session_id)
    if run is None:
        # After a process restart the in-memory registry is empty, but the
        # owner-gated API can still page the durable replay artifact by the
        # session/run mapping stored in ChatRunState.
        try:
            from core.database import ChatRunState, SessionLocal
            from src.chat_replay_log import ReplayLog
            with SessionLocal() as db:
                state = db.query(ChatRunState).filter(
                    ChatRunState.session_id == session_id,
                ).order_by(ChatRunState.updated_at.desc()).first()
                if state is None:
                    return None
                log = ReplayLog(replay_root(), state.run_id, session_id)
                page = log.page(after_seq, limit, active=False)
            rows = []
            for item in page.get("events", []):
                raw = "\n".join(
                    line[5:].lstrip() for line in item["event"].splitlines()
                    if line.startswith("data:")
                )
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    data = {"type": "opaque"}
                rows.append({"seq": item["seq"], "data": data})
            return {
                "run_id": state.run_id,
                "status": page.get("status") or state.status,
                "started_at": state.started_at.replace(tzinfo=timezone.utc).timestamp() if state.started_at else None,
                "last_seq": len(log) - 1,
                "next_seq": page.get("next_seq", after_seq),
                "events": rows,
                "has_more": page.get("has_more", False),
                "context_usage": dict(state.context_snapshot or {}) or None,
                "context_revision": state.context_revision or 0,
                "durable_seq": state.durable_seq,
                "ledger_hash": state.ledger_hash,
            }
        except (FileNotFoundError, ValueError, OSError):
            return None
        except Exception:
            logger.debug("[agent-run] durable event-page lookup failed", exc_info=True)
            return None
    count = len(run.buffer)
    if after_seq >= count and count:
        raise ValueError("Replay cursor is ahead of the log")
    rows = []
    for seq in range(after_seq + 1, min(count, after_seq + 1 + limit)):
        frame = run.buffer[seq]
        raw = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
        if raw == "[DONE]":
            data = {"type": "done"}
        else:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = {"type": "opaque"}
        rows.append({"seq": seq, "data": data})
    cursor = rows[-1]["seq"] if rows else after_seq
    return {
        **describe_run(session_id),
        "events": rows,
        "next_cursor": cursor,
        "has_more": cursor + 1 < count,
    }


def normalize_context_usage(data) -> Optional[dict]:
    """Validate a request measurement; never infer occupancy from billing totals."""
    if not isinstance(data, dict):
        return None
    used, window = data.get("used_tokens"), data.get("context_length")
    if (type(used) is not int or used < 0 or type(window) is not int or window <= 0
            or data.get("source") not in ("backend", "estimated")
            or not isinstance(data.get("model"), str) or not data["model"]):
        return None
    result = {key: data[key] for key in ("used_tokens", "context_length", "model", "source")}
    endpoint_key = data.get('endpoint_key')
    if endpoint_key is not None:
        if not isinstance(endpoint_key, str) or len(endpoint_key) != 64 or any(c not in '0123456789abcdef' for c in endpoint_key):
            return None
        result['endpoint_key'] = endpoint_key
    for key in ("prompt_tokens", "round", "compactions"):
        value = data.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
    revision = data.get("context_revision")
    if type(revision) is int and revision >= 0:
        result["context_revision"] = revision
    reason = data.get("context_reason")
    if reason in {"measurement", "compaction"}:
        result["context_reason"] = reason
    threshold = data.get("auto_compact_threshold")
    if type(threshold) in (int, float) and 0 <= threshold <= 100 and math.isfinite(threshold):
        result["auto_compact_threshold"] = threshold
    if type(data.get('auto_compact_enabled')) is bool:
        result['auto_compact_enabled'] = data['auto_compact_enabled']
    result["context_percent"] = min(100.0, round(used / window * 100, 1))
    return result


def get_context_usage(session_id: str, *, include_terminal: bool = False) -> Optional[dict]:
    """Copy the run ledger measurement.

    The legacy active-only behavior remains the default.  Context readers can
    include a just-terminal run to avoid flashing a smaller transcript
    estimate while approval/Stop/error persistence is being completed.
    """
    run = _RUNS.get(session_id)
    if not run or (run.status != "running" and not include_terminal):
        return None
    return dict(run.context_usage) if run and run.context_usage is not None else None


def continuation_for_session(session_id: str) -> dict:
    """Return non-secret execution toggles for a replacement Goal attempt."""
    run = _RUNS.get(session_id)
    if run is not None and run.continuation:
        return dict(run.continuation)
    try:
        from core.database import ChatRunState, SessionLocal
        with SessionLocal() as db:
            row = db.query(ChatRunState).filter(
                ChatRunState.session_id == session_id,
            ).order_by(ChatRunState.updated_at.desc(), ChatRunState.started_at.desc()).first()
            return dict(row.continuation or {}) if row is not None else {}
    except Exception:
        logger.debug("[agent-run] continuation lookup failed", exc_info=True)
        return {}


def context_checkpoint_for_session(session_id: str) -> Optional[dict]:
    value = continuation_for_session(session_id).get("working_checkpoint")
    if not isinstance(value, dict):
        return None
    has_summary = bool(str(value.get("summary") or "").strip())
    has_messages = isinstance(value.get("messages"), list) and bool(value["messages"])
    if not has_summary and not has_messages:
        return None
    return dict(value)


async def _drain(session_id: str, run: _Run, agen: AsyncGenerator[str, None],
                 prev_task: Optional[asyncio.Task] = None) -> None:
    """Pull every event from the wrapped generator into the run buffer, fanning
    each out to live subscribers. Runs to completion regardless of subscribers."""
    subscribers_woken = False

    def _wake_subscribers() -> None:
        nonlocal subscribers_woken
        if subscribers_woken:
            return
        subscribers_woken = True
        _wake_run_subscribers(run)

    # If this run replaced an in-flight one (rapid double-send), wait for that
    # one to fully finish first. Its CancelledError handler calls aclose(), which
    # persists its partial response — letting it complete before we start writing
    # keeps the two runs' session saves sequential instead of interleaved.
    try:
        if prev_task is not None and not prev_task.done():
            await asyncio.wait({prev_task})
        async for ev in agen:
            _publish(run, ev)
        if run.status == "running":
            run.terminal_status = "done"
    except asyncio.CancelledError:
        # Let the wrapped generator's own CancelledError handler run (it saves
        # the partial response to the session).
        try:
            await agen.aclose()
        except Exception:
            pass
        # Keep is_active() true until persistence is complete. Otherwise a
        # concurrent browser poll can replace the live view with stale history.
        run.terminal_status = "stopped"
        # A rapid third replacement can cancel this task while it is still
        # waiting for its predecessor. Close this run's subscribers promptly,
        # but keep the task alive until the predecessor finishes so the next
        # run still observes the transitive session-save ordering barrier.
        _wake_subscribers()
        if prev_task is not None and not prev_task.done():
            try:
                await asyncio.shield(prev_task)
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.error("[agent-run] %s failed: %s", session_id, e, exc_info=True)
        run.terminal_status = "error"
        try:
            await agen.aclose()
        except Exception:
            pass
        try:
            _publish(
                run,
                "event: error\n"
                f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
            )
            _publish(run, "data: [DONE]\n\n")
        except OSError:
            # Disk full/limit must terminate the producer, not recursively try
            # to persist another error or silently continue effectful tools.
            logger.error('[agent-run] replay storage unavailable; run stopped')
    finally:
        terminal_status = run.terminal_status or run.status
        # Persist the final ledger/timeline before exposing terminal status.
        # This is important for approval and Stop: history readers must never
        # observe the short stored-chat estimate in that handoff window.
        _persist_timeline_v2(session_id, run, status=terminal_status)
        if hasattr(run.buffer, 'checkpoint'):
            try:
                run.buffer.checkpoint(terminal_status)
            except OSError:
                logger.error('[agent-run] replay checkpoint unavailable')
        _persist_run_state(run, status=terminal_status, durable=True)
        # Wake every subscriber with the end sentinel so their SSE closes.
        _wake_subscribers()
        # Run is terminal — arm the grace timer so it (and its buffer) is
        # eventually freed even if nobody ever reconnects. subscribe() cancels
        # this on connect and re-arms on disconnect.
        run.status = terminal_status
        _schedule_evict(session_id, run)
        if run.on_terminal is not None and run.status in {"done", "error"}:
            callback = run.on_terminal

            async def _notify_terminal() -> None:
                # Let this drain task become terminal before the controller
                # acquires a lease and starts the next run for the same chat.
                await asyncio.sleep(0)
                try:
                    await callback(run.status)
                except Exception:
                    logger.exception(
                        "[agent-run] terminal controller failed for %s", session_id,
                    )

            asyncio.create_task(_notify_terminal())


def start(
    session_id: str,
    agen: AsyncGenerator[str, None],
    *,
    on_terminal: Optional[Callable[[str], Awaitable[None]]] = None,
    owner: Optional[str] = None,
    continuation: Optional[dict] = None,
) -> _Run:
    """Start a detached run draining `agen` for a session. If a run is already in
    flight for this session (e.g. a rapid double-send), it's cancelled first."""
    # Allocate storage before cancelling the current run. An unavailable disk
    # must not destroy a still-running predecessor on a failed replacement.
    run = _Run()
    run.on_terminal = on_terminal
    run.session_id = str(session_id)
    run.owner = str(owner or "").strip() or None
    prior_continuation = continuation_for_session(session_id)
    run.continuation = {
        **({"working_checkpoint": prior_continuation["working_checkpoint"]}
           if isinstance(prior_continuation.get("working_checkpoint"), dict) else {}),
        **dict(continuation or {}),
    }
    _seed_context_ledger(run)
    if os.getenv('ODYSSEUS_DURABLE_CHAT_REPLAY') == '1':
        from src.chat_replay_log import ReplayLog
        run.buffer = ReplayLog(replay_root(), run.run_id, session_id, create=True)
    _persist_run_state(run)
    prev = _RUNS.get(session_id)
    prev_task: Optional[asyncio.Task] = None
    if prev:
        if prev.task and not prev.task.done():
            # A task cancelled before its first instruction never enters
            # _drain(), so its except/finally blocks cannot update status or
            # wake a response already bound to this exact run. Terminalize it
            # synchronously before cancelling; _drain's cleanup is idempotent
            # when the task had already started.
            if prev.status == "running":
                prev.status = "stopped"
                _persist_run_state(prev, status="stopped", durable=True)
                _wake_run_subscribers(prev)
            prev.task.cancel()
            prev_task = prev.task   # new run awaits this before it starts writing
        if prev.evict_task and not prev.evict_task.done():
            prev.evict_task.cancel()
    _RUNS[session_id] = run
    run.task = asyncio.create_task(_drain(session_id, run, agen, prev_task))
    return run


async def subscribe(
    session_id: str,
    expected_run: Optional[_Run] = None,
    after_seq: int = -1,
) -> AsyncGenerator[str, None]:
    """Replay the run's buffer from the start, then stream live until it ends.
    Safe to call repeatedly (reconnect) and from multiple clients at once.

    ``expected_run`` binds a lazy StreamingResponse body to the same run whose
    identity was put in its response headers. Without that binding, a rapid
    replacement between response construction and body iteration could replay
    the replacement run under the prior run's identity.
    """
    run = expected_run or _RUNS.get(session_id)
    if run is None:
        return
    # A queue carries only a coalesced wake-up, never duplicate token payloads.
    # Slow/disconnected clients read their own cursor from the replay artifact.
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    run.subscribers.add(q)            # register BEFORE replaying so nothing is missed
    # A live subscriber is connected — don't let a pending grace timer evict
    # the run out from under it mid-replay.
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        if type(after_seq) is not int or after_seq < -1:
            return
        next_seq = min(after_seq + 1, len(run.buffer))
        while next_seq < len(run.buffer):
            yield run.buffer[next_seq]
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_idx = 0
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keep slow local models/proxies alive while they prefill before
                # the first token. SSE comments are ignored by the UI but reset
                # browser/proxy idle timers, which prevents "empty response"
                # disconnects on llama.cpp first-token latencies of 30s+.
                if run.status == "running":
                    heartbeat_idx += 1
                    yield f": heartbeat {heartbeat_idx}\n\n"
                    continue
            while next_seq < len(run.buffer):
                yield run.buffer[next_seq]
                next_seq += 1
            if run.status != 'running':
                break
    finally:
        run.subscribers.discard(q)
        # Last subscriber gone on a finished run — (re)arm eviction so the
        # buffer doesn't linger indefinitely.
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id, run)


def stop(session_id: str, expected_run_id: Optional[str] = None) -> bool:
    """Cancel the matching in-flight run (which saves its partial output).

    A stale browser may issue Stop after another tab has replaced the session's
    run. Once the caller knows its opaque run identity, fail closed rather than
    cancelling that newer run.
    """
    run = _RUNS.get(session_id)
    if not expected_run_id or run is None or run.run_id != expected_run_id:
        return False
    if run and run.task and not run.task.done():
        run.task.cancel()
        return True
    return False


async def stop_and_wait(
    session_id: str,
    expected_run_id: Optional[str] = None,
    timeout: float = 15.0,
) -> bool:
    """Cancel one exact run and wait until its partial transcript is durable."""
    run = _RUNS.get(session_id)
    if not expected_run_id or run is None or run.run_id != expected_run_id:
        return False
    task = run.task
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, timeout))
    except asyncio.CancelledError:
        # The run task handles its own cancellation and performs the durable
        # timeline/checkpoint write before completing.  A cancellation raised
        # here therefore still needs the completion check below.
        pass
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for stopped run %s to persist", run.run_id)
        return False
    return bool(task.done() and run.status != "running")
