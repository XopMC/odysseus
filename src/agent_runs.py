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
from datetime import datetime, timedelta
import json
import logging
import math
import os
import time
import uuid
from typing import AsyncGenerator, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def replay_root():
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, 'chat-replay')


class _Run:
    __slots__ = (
        "buffer", "subscribers", "status", "task", "evict_task", "run_id",
        "context_usage", "started_at", "round", "segment", "tool_counter",
        "active_tool_call_id", "on_terminal",
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


_RUNS: Dict[str, _Run] = {}

# How long a FINISHED run (and its full replay buffer) is retained after the
# last subscriber disconnects, so a reconnect within the window can still
# replay the result. After this, the run is evicted to bound memory — without
# it, every session that ever streamed kept its entire event log forever.
_EVICT_GRACE_S = 180


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
    # Bind measurements to this exact run, not a session lookup: a cancelled
    # predecessor can still publish while its replacement is being started.
    try:
        payload = json.loads("\n".join(
            line[5:].lstrip() for line in ev.splitlines() if line.startswith("data:")
        ))
        if isinstance(payload, dict) and payload.get("type") == "context_usage":
            snapshot = normalize_context_usage(payload.get("data"))
            if snapshot is not None:
                run.context_usage = snapshot
    except (TypeError, ValueError):
        pass  # Other SSE events, comments and [DONE] are not measurements.
    run.buffer.append(ev)
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


def _persist_timeline_v2(session_id: str, run: _Run) -> None:
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
        for index in range(min(len(run.buffer), 5000)):
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
            events.append(item)
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
        for round_number in range(1, max_round + 1):
            parts = round_parts.get(round_number, {"thinking": [], "text": []})
            thinking = "".join(parts["thinking"]).strip()
            text = "".join(parts["text"]).strip()
            round_texts.append((f"<think>\n{thinking}\n</think>\n\n" if thinking else "") + text)
        tool_events = []
        for tool_id in tool_order:
            event = tools[tool_id]
            progress_tail = event.pop("_progress_tail", "")
            if run.status == "stopped" and event.get("exit_code") is None:
                event["exit_code"] = 130
                event["output"] = ((progress_tail + "\n") if progress_tail else "") + "Interrupted by user."
            tool_events.append(event)

        legacy_metadata = {}
        if run.status in {"stopped", "error"}:
            legacy_metadata["stopped"] = run.status == "stopped"
            legacy_metadata["cancelled"] = run.status == "stopped" and not substantive
            if round_texts:
                legacy_metadata["round_texts"] = round_texts
            if tool_events:
                legacy_metadata["tool_events"] = tool_events
            if actual_model:
                legacy_metadata["model"] = actual_model
            if actual_endpoint_id:
                legacy_metadata["endpoint_id"] = actual_endpoint_id
            if actual_endpoint_label:
                legacy_metadata["endpoint_label"] = actual_endpoint_label

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
            if row is None and run.status in {"stopped", "error"}:
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
                metadata["working_context"] = dict(run.context_usage)
            metadata["timeline_v2"] = {
                "version": 2,
                "run_id": run.run_id,
                "started_at": run.started_at,
                "status": run.status,
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
        return None
    return {
        "run_id": run.run_id,
        "status": run.status,
        "started_at": run.started_at,
        "last_seq": len(run.buffer) - 1,
        "next_seq": len(run.buffer),
        "context_usage": dict(run.context_usage) if run.context_usage else None,
    }


def event_page(session_id: str, *, after_seq: int = -1, limit: int = 100) -> Optional[dict]:
    """Return a bounded JSON snapshot without opening a live SSE subscriber."""
    if type(after_seq) is not int or after_seq < -1 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid replay cursor or page size")
    run = _RUNS.get(session_id)
    if run is None:
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
    threshold = data.get("auto_compact_threshold")
    if type(threshold) in (int, float) and 0 <= threshold <= 100 and math.isfinite(threshold):
        result["auto_compact_threshold"] = threshold
    if type(data.get('auto_compact_enabled')) is bool:
        result['auto_compact_enabled'] = data['auto_compact_enabled']
    result["context_percent"] = min(100.0, round(used / window * 100, 1))
    return result


def get_context_usage(session_id: str) -> Optional[dict]:
    """Copy the active run's latest measurement; terminal history is read separately."""
    run = get_active_run(session_id)
    return dict(run.context_usage) if run and run.context_usage is not None else None


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
            run.status = "done"
    except asyncio.CancelledError:
        # Let the wrapped generator's own CancelledError handler run (it saves
        # the partial response to the session).
        try:
            await agen.aclose()
        except Exception:
            pass
        # Keep is_active() true until persistence is complete. Otherwise a
        # concurrent browser poll can replace the live view with stale history.
        run.status = "stopped"
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
        run.status = "error"
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
        _persist_timeline_v2(session_id, run)
        if hasattr(run.buffer, 'checkpoint'):
            try:
                run.buffer.checkpoint(run.status)
            except OSError:
                logger.error('[agent-run] replay checkpoint unavailable')
        # Wake every subscriber with the end sentinel so their SSE closes.
        _wake_subscribers()
        # Run is terminal — arm the grace timer so it (and its buffer) is
        # eventually freed even if nobody ever reconnects. subscribe() cancels
        # this on connect and re-arms on disconnect.
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
) -> _Run:
    """Start a detached run draining `agen` for a session. If a run is already in
    flight for this session (e.g. a rapid double-send), it's cancelled first."""
    # Allocate storage before cancelling the current run. An unavailable disk
    # must not destroy a still-running predecessor on a failed replacement.
    run = _Run()
    run.on_terminal = on_terminal
    if os.getenv('ODYSSEUS_DURABLE_CHAT_REPLAY') == '1':
        from src.chat_replay_log import ReplayLog
        run.buffer = ReplayLog(replay_root(), run.run_id, session_id, create=True)
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
        pass
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for stopped run %s to persist", run.run_id)
    return True
