"""Server-owned Goal continuation dispatch.

UI actions may request Resume or revise an objective, but the browser never
owns the next attempt.  Once durable state is active this controller acquires
the lease and starts the detached run before the mutation request returns.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
from src.constants import internal_api_base

logger = logging.getLogger(__name__)


async def dispatch_goal_continuation(owner: str | None, session_id: str, *, reason: str) -> bool:
    from src import agent_runs
    from src.chat_work_store import WorkNotFound, store

    if not session_id or agent_runs.is_active(session_id):
        return False
    lease = await asyncio.to_thread(store.acquire_goal_lease, owner, session_id)
    if not lease:
        return False
    prior = agent_runs.continuation_for_session(session_id)
    form = {
        "session": session_id,
        "message": (
            "Continue the active goal from its durable checkpoint. Apply the "
            "latest user guidance, change approach after repeated failures, and "
            "call complete_goal only after verified completion."
        ),
        "mode": "agent",
        "goal_continuation": "true",
        "goal_lease_token": lease,
        "allow_bash": "true" if prior.get("allow_bash") is True else "false",
        "allow_web_search": "true" if prior.get("allow_web_search") is True else "false",
    }
    headers = {"Origin": internal_api_base()}
    if owner:
        headers.update({
            INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN,
            "X-Odysseus-Owner": str(owner),
        })
    failure = None
    try:
        timeout = httpx.Timeout(20.0, read=20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{internal_api_base()}/api/chat_stream",
                headers=headers, data=form,
            ) as response:
                if response.status_code >= 400:
                    failure = f"Goal continuation HTTP {response.status_code}"
    except Exception:
        failure = "Goal continuation connection failed"
    if failure:
        logger.warning("%s for session %s (%s)", failure, session_id, reason)
        try:
            await asyncio.to_thread(
                store.record_goal_failure, owner, session_id, failure,
                {"reason": reason},
            )
        except WorkNotFound:
            pass
        return False
    return True
