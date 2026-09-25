"""Server-owned Goal continuation dispatch.

UI actions may request Resume or revise an objective, but the browser never
owns the next attempt.  Once durable state is active this controller acquires
the lease and starts the detached run before the mutation request returns.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
from src.constants import internal_api_base

logger = logging.getLogger(__name__)


def _known_dispatch_rejection(status: int, raw: bytes) -> tuple[str, str]:
    """Classify only fixed server messages; never persist arbitrary detail text."""
    if status == 400:
        try:
            detail = json.loads(raw[:2048]).get("detail")
        except (ValueError, AttributeError, TypeError):
            detail = None
        if isinstance(detail, str):
            if detail.startswith("No model selected for this chat"):
                return "model_unselected", "Goal continuation: no model selected"
            if detail.startswith("Selected model endpoint was removed") or detail.startswith("Selected model endpoint is not configured"):
                return "model_endpoint_unavailable", "Goal continuation: selected model endpoint is unavailable"
    return f"http_{status}", f"Goal continuation HTTP {status}"


async def dispatch_goal_continuation(owner: str | None, session_id: str, *, reason: str,
                                     expected_goal_id: str | None = None,
                                     expected_attempt: int | None = None) -> bool:
    from src import agent_runs
    from src.chat_effect_inbox import inbox
    from src.chat_work_store import WorkConflict, WorkNotFound, store

    if not session_id or agent_runs.is_active(session_id):
        return False
    if await asyncio.to_thread(inbox.unknown, owner, session_id):
        logger.warning("Goal continuation fenced by unknown effect for session %s", session_id)
        return False
    lease = await asyncio.to_thread(
        store.acquire_goal_lease, owner, session_id,
        expected_goal_id=expected_goal_id, expected_attempt=expected_attempt,
    )
    if not lease:
        return False
    prior = agent_runs.continuation_for_session(session_id)
    current = await asyncio.to_thread(store.get, owner, session_id)
    objective = str((current.get("goal") or {}).get("objective") or "")
    form = {
        "session": session_id,
        "message": (
            "Continue the current active Goal from its durable checkpoint. "
            "Current Goal objective (not a previous completed Goal): "
            + json.dumps(objective, ensure_ascii=False) + "\n"
            "Apply the latest user guidance and change approach after repeated "
            "failures. An interrupted tool action marked no_retry must not be "
            "repeated or treated as proof; use a distinct safe verification "
            "action if needed. Do not claim Goal completion in prose: call "
            "complete_goal only after verified evidence."
        ),
        "mode": "agent",
        "goal_continuation": "true",
        "goal_lease_token": lease,
        "allow_bash": "true" if prior.get("allow_bash") is True else "false",
        "allow_web_search": "true" if prior.get("allow_web_search") is True else "false",
    }
    from src.subagent_delivery import claim_pending, release_claim
    delivery_token = await asyncio.to_thread(claim_pending, owner, session_id)
    if delivery_token:
        form["subagent_delivery_token"] = delivery_token
    headers = {"Origin": internal_api_base()}
    if owner:
        headers.update({
            INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN,
            "X-Odysseus-Owner": str(owner),
        })
    failure = None
    failure_code = None
    try:
        timeout = httpx.Timeout(20.0, read=20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{internal_api_base()}/api/chat_stream",
                headers=headers, data=form,
            ) as response:
                if response.status_code >= 400:
                    raw = await response.aread() if response.status_code == 400 else b""
                    failure_code, failure = _known_dispatch_rejection(response.status_code, raw)
    except Exception:
        failure = "Goal continuation connection failed"
        failure_code = "transport"
    if failure:
        if delivery_token and failure_code and failure_code != "transport":
            await asyncio.to_thread(release_claim, owner, session_id, delivery_token)
        logger.warning("%s for session %s (%s)", failure, session_id, reason)
        try:
            await asyncio.to_thread(
                store.record_goal_failure, owner, session_id, failure,
                {"reason": "continuation_dispatch_failed", "dispatch_reason": reason,
                 "failure_code": failure_code},
                force_wait_user=True,
                expected_goal_id=expected_goal_id,
                expected_attempt=expected_attempt,
            )
        except (WorkNotFound, WorkConflict):
            pass
        return False
    return True
