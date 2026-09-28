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


def _dispatch_retry_delay(attempt: int) -> float:
    return min(5.0, 0.25 * (2 ** min(max(0, int(attempt) - 1), 5)))


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
    # An uncertain side effect fences that exact action, not the whole Goal.
    # The continuation receives a warning and can make independent progress;
    # it must not infer the result or repeat an equivalent action.
    unresolved_effects = await asyncio.to_thread(inbox.unknown, owner, session_id)
    lease = await asyncio.to_thread(
        store.acquire_goal_lease, owner, session_id,
        expected_goal_id=expected_goal_id, expected_attempt=expected_attempt,
    )
    if not lease:
        return False
    prior = agent_runs.continuation_for_session(session_id)
    current = await asyncio.to_thread(store.get, owner, session_id)
    current_goal = current.get("goal") or {}
    objective = str(current_goal.get("objective") or "")
    dispatched_goal_id = current_goal.get("id")
    dispatched_attempt = int(current_goal.get("attempt") or 1)
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
    if reason == "question_timeout":
        form["message"] += (
            "\nThe user did not answer your ordinary question within one minute. "
            "Choose the best safe option from available evidence and continue. "
            "This timeout is not approval for any tool effect or permission request."
        )
    if unresolved_effects:
        form["message"] += (
            f"\nThere are {len(unresolved_effects)} unresolved tool outcome(s). "
            "Do not repeat or infer the result of any such action. Continue independent "
            "safe work; use read-only verification where useful, and ask the user only "
            "if the remaining objective truly depends on the uncertain effect."
        )
    repeated_stalls = int((current_goal.get("checkpoint") or {}).get("premature_stop_runs") or 0)
    if repeated_stalls:
        form["message"] += (
            f"\nThe previous attempt repeated an answer without progress ({repeated_stalls} recovery event(s)). "
            "Do not restate it; inspect the latest checkpoint, choose a different concrete action, "
            "and keep the Goal active until verified completion or a genuine user decision."
        )
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
    dispatch_attempts = 0
    timeout = httpx.Timeout(20.0, read=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for dispatch_attempts in range(1, 11):
                try:
                    async with client.stream(
                        "POST", f"{internal_api_base()}/api/chat_stream",
                        headers=headers, data=form,
                    ) as response:
                        if response.status_code < 400:
                            failure = None
                            failure_code = None
                            break
                        raw = await response.aread() if response.status_code == 400 else b""
                        failure_code, failure = _known_dispatch_rejection(response.status_code, raw)
                except Exception:
                    failure = "Goal continuation connection failed"
                    failure_code = "transport"

                # A POST response can be lost after the server accepted the lease.
                # Check both the in-memory run registry and the durable attempt CAS
                # before retrying, so an ambiguous transport never starts a duplicate.
                if agent_runs.is_active(session_id):
                    failure = None
                    failure_code = None
                    break
                try:
                    observed = await asyncio.to_thread(store.get, owner, session_id)
                    observed_goal = observed.get("goal") or {}
                except Exception:
                    observed_goal = {}
                if (observed_goal.get("id") == dispatched_goal_id
                        and int(observed_goal.get("attempt") or 0) > dispatched_attempt):
                    failure = None
                    failure_code = None
                    break

                # A missing/unavailable selected model is a user configuration
                # decision. Transport, authorization, rate-limit and 5xx failures
                # are retried ten times by this controller before pausing the Goal.
                if failure_code in {"model_unselected", "model_endpoint_unavailable", "http_400"}:
                    break
                if dispatch_attempts < 10:
                    await asyncio.sleep(_dispatch_retry_delay(dispatch_attempts))
    except Exception:
        # Client construction/teardown failure is still a bounded dispatch
        # failure. Never strand the active Goal because transport setup raised.
        failure = "Goal continuation connection failed"
        failure_code = "transport"
        dispatch_attempts = max(dispatch_attempts, 10)
    if failure:
        if delivery_token:
            await asyncio.to_thread(release_claim, owner, session_id, delivery_token)
        logger.warning("%s for session %s (%s)", failure, session_id, reason)
        permanent_configuration = failure_code in {
            "model_unselected", "model_endpoint_unavailable", "http_400",
        }
        try:
            await asyncio.to_thread(
                store.record_goal_failure, owner, session_id, failure,
                {"reason": "continuation_dispatch_failed", "dispatch_reason": reason,
                 "failure_code": failure_code,
                 "failure_class": "provider_http" if not permanent_configuration else "configuration",
                 "retry_exhausted": not permanent_configuration,
                 "dispatch_attempts": dispatch_attempts},
                force_wait_user=permanent_configuration,
                failure_attempts=10 if not permanent_configuration else 1,
                expected_goal_id=expected_goal_id,
                expected_attempt=expected_attempt,
            )
        except (WorkNotFound, WorkConflict):
            pass
        return False
    return True
