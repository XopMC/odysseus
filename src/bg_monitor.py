"""Always-on monitor that auto-continues the agent when a background job
(see src/bg_jobs.py) finishes.

Reliability is the whole point: completion → agent re-invocation must never
silently no-op. The monitor drains `bg_jobs.pending_followups()` every tick and
only calls `mark_followed_up()` AFTER the outcome is persisted. This means
delivered, not necessarily task-complete: an unfinished/error/approval outcome
is saved explicitly and is not blindly replayed on the next tick. A timed-out
or dead job still produces a follow-up, so the user always hears back.
"""

from __future__ import annotations

import asyncio
import json
import logging

from src import bg_jobs
from src.prompt_security import untrusted_context_message

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5

def _followup_limits():
    """Honor the same configured budgets as foreground Agent turns."""
    from src.agent_tools import MAX_AGENT_ROUNDS
    from src.settings import get_setting

    try:
        rounds = int(get_setting("agent_max_rounds", MAX_AGENT_ROUNDS) or MAX_AGENT_ROUNDS)
    except (TypeError, ValueError):
        rounds = MAX_AGENT_ROUNDS
    try:
        tool_calls = int(get_setting("agent_max_tool_calls", 0))
    except (TypeError, ValueError):
        tool_calls = 0
    return max(1, min(rounds, 200)), max(0, tool_calls)


def _background_result_message(rec):
    inject = (
        f"[Background job {rec['id']} finished]\n\n"
        f"{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Don't repeat work that's already done. "
        "If the task is now complete, give the user the final result."
    )
    return untrusted_context_message("background job output", inject)


async def _drain_agent(sess, messages, *, outcome=None):
    """Run the agent loop headless against a session. Returns
    (final_prose, tool_events) — tool_events in the same shape the live chat
    saves, so the frontend rebuilds them as standard agent-thread tool cards.
    The optional outcome dict distinguishes a completed stream from a safety
    stop, provider failure, or required user input without changing the tuple
    returned to existing callers.
    """
    from src.agent_loop import stream_agent_loop
    full = ""
    tool_events = []
    round_num = 1
    terminal = None
    saw_done = False
    max_rounds, max_tool_calls = _followup_limits()
    async for chunk in stream_agent_loop(
        sess.endpoint_url, sess.model, messages,
        headers=getattr(sess, "headers", None),
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=max_rounds,
        max_tool_calls=max_tool_calls,
        owner=getattr(sess, "owner", None),
    ):
        # Provider errors have an `event: error` prelude rather than starting
        # with data. Ignoring those used to turn a failed stream into success.
        if chunk.startswith("event: error"):
            terminal = {"status": "failed", "reason": "provider_error"}
        body = "\n".join(line[5:].lstrip() for line in chunk.splitlines()
                         if line.startswith("data:")).strip()
        if body == "[DONE]":
            saw_done = True
            continue
        if not body:
            continue
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        event_type = d.get("type")
        if event_type in {
            "rounds_exhausted", "budget_exceeded", "loop_breaker_triggered",
            "intent_nudge_exhausted",
        }:
            if not terminal or terminal["status"] != "failed":
                terminal = {"status": "unfinished", "reason": event_type}
        elif event_type in {"context_compaction_failed", "agent_terminal"} or d.get("error"):
            terminal = {"status": "failed", "reason": event_type or "provider_error"}
        elif (event_type == "ask_user"
              or (event_type == "tool_output" and isinstance(d.get("ask_user"), dict))):
            if not terminal or terminal["status"] != "failed":
                terminal = {"status": "waiting_user", "reason": "ask_user"}
        if "delta" in d:
            delta = d.get("delta")
            if isinstance(delta, str):
                if d.get("thinking"):
                    continue
                full += delta
        elif d.get("type") == "agent_step":
            round_num = d.get("round", round_num)
        elif d.get("type") == "tool_output":
            # Mirror the live chat's tool_event shape (chat_routes / chatRenderer).
            tool_event = {
                "round": round_num,
                "tool": d.get("tool"),
                "command": d.get("command"),
                "output": d.get("output"),
                "exit_code": d.get("exit_code"),
            }
            if isinstance(d.get("ask_user"), dict):
                # Preserve exact-approval cards from a tainted background-job
                # continuation so the user can authorize the sealed action on
                # the next foreground turn instead of losing it headlessly.
                tool_event["ask_user"] = d["ask_user"]
            tool_events.append(tool_event)
    if outcome is not None:
        outcome.update(terminal or {
            "status": "completed" if saw_done else "unfinished",
            "reason": "normal_finish" if saw_done else "stream_incomplete",
        })
    return full, tool_events


async def _run_followup(rec: dict) -> bool:
    """Return True when the outcome was delivered, not when the task is done.

    A saved pause/failure is handled once: blindly rerunning the same job result
    can repeat already completed effectful actions. Only a busy/not-ready
    session is deferred without consuming the result.
    """
    from src.ai_interaction import get_session_manager
    from core.models import ChatMessage

    sm = get_session_manager()
    if not sm:
        return False  # not ready yet — retry
    sess = sm.get_session(rec["session_id"])
    if not sess:
        # Session was deleted — nothing to continue. Consider it handled so we
        # don't retry forever.
        logger.info("bg-followup: session %s gone for job %s — skipping", rec.get("session_id"), rec.get("id"))
        return True

    # Don't write into a session that's mid-stream. The followup appends to
    # history + save_sessions(); a concurrent live turn does the same, and with
    # no per-session lock the two interleave (reordered/clobbered messages).
    # Defer — return False so we retry on the next tick once the turn finishes.
    try:
        from src import agent_runs
        if agent_runs.is_active(sess.id):
            logger.info("bg-followup: session %s busy (live turn) — deferring job %s", sess.id, rec.get("id"))
            return False
    except Exception:
        pass

    context = sess.get_context_messages()
    context.append(_background_result_message(rec))

    outcome = {}
    full, tool_events = await _drain_agent(sess, context, outcome=outcome)
    if outcome["status"] == "waiting_user":
        note = ("[Background continuation is waiting for your answer or approval. "
                "The task is not completed.]")
        full = f"{full}\n\n{note}".strip()
    elif outcome["status"] != "completed":
        note = (f"[Background continuation not completed ({outcome['reason']}). "
                "Progress was saved; continue the task from this chat. "
                "The background command will not be replayed automatically.]")
        full = f"{full}\n\n{note}".strip()

    # Persist ONLY the assistant continuation so it renders as a normal agent
    # turn — a standard chat bubble plus `tool_events` that the frontend
    # rebuilds into the usual agent-thread tool cards (chatRenderer:1494). The
    # trigger isn't saved as its own message (it'd be an out-of-place bubble);
    # the raw job output is stashed in metadata for traceability instead.
    sm.add_message(sess.id, ChatMessage(
        "assistant", full,
        metadata={
            "tool_events": tool_events,
            "model": sess.model,
            "bg_job_id": rec["id"],
            "bg_result": bg_jobs.result_text(rec)[:4000],
            "bg_followup": outcome,
        },
    ))
    sm.save_sessions()
    logger.info("bg-followup: delivered session %s job %s status=%s (%d chars, %d tools)",
                sess.id, rec["id"], outcome["status"], len(full), len(tool_events))
    return True


async def _loop():
    while True:
        try:
            for rec in bg_jobs.pending_followups():
                try:
                    if await _run_followup(rec):
                        bg_jobs.mark_followed_up(rec["id"])
                except Exception as e:
                    # Idempotent: leave followed_up=False so the next tick retries.
                    logger.warning("bg-followup failed for %s (will retry): %s", rec.get("id"), e)
        except Exception as e:
            logger.warning("bg-monitor tick error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_S)


def start_bg_monitor():
    """Idempotent — start the always-on background-job monitor."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        return _monitor_task
    _monitor_task = asyncio.create_task(_loop())
    logger.info("Background-job monitor started (poll %ds)", POLL_INTERVAL_S)
    return _monitor_task
