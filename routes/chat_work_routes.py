"""Owner-scoped public API for durable Plan and Goal controls."""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse

from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src.chat_work_store import WorkConflict, WorkNotFound, store
from src.owner_identity import auth_disabled


class ChatWorkRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except WorkNotFound as exc:
                raise HTTPException(404, str(exc)) from None
            except WorkConflict as exc:
                raise HTTPException(409, str(exc)) from None
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from None
            response.headers["Cache-Control"] = "private, no-store"
            return response
        return handler


def _owner(request, session_id, mutation=False):
    _verify_session_owner(request, session_id)
    owner = effective_user(request)
    if not owner and not auth_disabled():
        raise HTTPException(401, "Login required")
    return owner


async def _json(request, limit=64 * 1024):
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, "Request is too large")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "JSON object required") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON object required")
    return data


def setup_chat_work_routes():
    router = APIRouter(prefix="/api/chat/work", route_class=ChatWorkRoute)

    @router.get("/{session_id}")
    async def snapshot(session_id: str, request: Request):
        return store.get(_owner(request, session_id), session_id)

    @router.get("/{session_id}/why-waiting")
    async def why_waiting(session_id: str, request: Request):
        """Content-free, owner-scoped diagnosis shared by every client."""
        owner = _owner(request, session_id)
        from src import agent_runs
        from src.chat_effect_inbox import inbox
        from src.run_wait_state import compose_wait_panel, selected_endpoint_host
        from src.subagent_runtime import runtime
        from core.database import SessionLocal, Session as DbSession

        goal = store.wait_metadata(owner, session_id)
        run = agent_runs.describe_run(session_id)
        children = runtime.active_summary(
            owner, session_id,
            parent_run_id=run.get("run_id") if run else None,
        )
        selected_endpoint_label = None
        if not ((run or {}).get("wait_state") or {}).get("endpoint_label"):
            try:
                with SessionLocal() as db:
                    endpoint_url = db.query(DbSession.endpoint_url).filter(
                        DbSession.id == session_id,
                    ).scalar()
                selected_endpoint_label = selected_endpoint_host(endpoint_url)
            except Exception:
                selected_endpoint_label = None
        return compose_wait_panel(
            run=run, goal=goal, children=children,
            selected_endpoint_label=selected_endpoint_label,
            unknown_effects=len(inbox.unknown(owner, session_id))
            if goal.get("wait_reason") == "unknown_side_effect" else None,
            blocking_effects=len(inbox.blocking(owner, session_id))
            if goal.get("wait_reason") == "unknown_side_effect" else None,
            pending_effects=len(inbox.pending_actions(owner, session_id))
            if goal.get("wait_reason") == "unknown_side_effect" else None,
        )

    @router.get("/{session_id}/run-inspector")
    async def run_inspector(session_id: str, request: Request, limit: int = 20,
                            before_run_id: str | None = None):
        from src.run_inspector import snapshot
        return snapshot(_owner(request, session_id), session_id, limit=limit,
                        before_run_id=before_run_id)

    @router.get("/{session_id}/run-inspector/{run_id}/events")
    async def run_inspector_events(session_id: str, run_id: str, request: Request,
                                   before_seq: int | None = None, limit: int = 200):
        from src.run_inspector import event_refs
        result = event_refs(_owner(request, session_id), session_id, run_id,
                            before_seq=before_seq, limit=limit)
        if result is None:
            raise HTTPException(404, "Run not found")
        return result

    @router.get("/{session_id}/run-inspector/artifacts/{evidence_id}")
    async def run_inspector_artifact(session_id: str, evidence_id: str, request: Request):
        from src.run_inspector import artifact_detail
        result = artifact_detail(_owner(request, session_id), session_id, evidence_id)
        if result is None:
            raise HTTPException(404, "Artifact not found")
        return result

    @router.get("/{session_id}/unknown-effects")
    async def unknown_effects(session_id: str, request: Request):
        """Content-free durable reconciliation inbox; reads never authorize replay."""
        from src.chat_effect_inbox import inbox
        return {"effects": inbox.pending_actions(_owner(request, session_id), session_id)}

    @router.post("/{session_id}/unknown-effects/{intent_id}/verify")
    async def effect_verify(session_id: str, intent_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"expected_revision", "outcome", "evidence"}:
            raise HTTPException(400, "Exact revision, verification outcome, and evidence required")
        from src.chat_effect_inbox import inbox
        return inbox.verify(
            owner, session_id, intent_id,
            expected_revision=body["expected_revision"],
            outcome=body["outcome"], evidence=body["evidence"],
        )

    @router.post("/{session_id}/unknown-effects/{intent_id}/authorize-retry")
    async def effect_authorize_retry(session_id: str, intent_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"expected_revision"}:
            raise HTTPException(400, "Exact effect revision required")
        from src.chat_effect_inbox import inbox
        return inbox.authorize_retry(
            owner, session_id, intent_id,
            expected_revision=body["expected_revision"],
        )

    @router.post("/{session_id}/unknown-effects/{intent_id}/no-retry")
    async def effect_no_retry(session_id: str, intent_id: str, request: Request):
        """Owner explicitly declines replay; this is not a verification claim."""
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"expected_revision"}:
            raise HTTPException(400, "Exact effect revision required")
        from src.chat_effect_inbox import inbox
        return inbox.no_retry(
            owner, session_id, intent_id,
            expected_revision=body["expected_revision"],
        )

    @router.get("/{session_id}/events")
    async def events(session_id: str, request: Request, after: int = 0, limit: int = 100):
        rows = store.events(_owner(request, session_id), session_id, after=after, limit=limit)
        return {"events": rows, "next_cursor": rows[-1]["seq"] if rows else after}

    @router.get("/{session_id}/events/stream")
    async def event_stream(session_id: str, request: Request, after: int = 0):
        owner = _owner(request, session_id)
        raw_cursor = request.headers.get("Last-Event-ID") or str(after)
        try:
            cursor = int(raw_cursor)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid event cursor") from None
        if cursor < 0:
            raise HTTPException(400, "Invalid event cursor")
        # StreamingResponse sends HTTP 200 before its generator runs. Validate
        # the durable owner/session row first so a missing chat is a normal
        # 404, not an exception after headers were committed (server-side 500).
        store.get(owner, session_id)

        async def generate():
            nonlocal cursor
            heartbeat_at = time.monotonic()
            while not await request.is_disconnected():
                rows = store.events(owner, session_id, after=cursor, limit=100)
                if rows:
                    for event in rows:
                        cursor = int(event["seq"])
                        yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    heartbeat_at = time.monotonic()
                    continue
                if time.monotonic() - heartbeat_at >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/{session_id}/plan")
    async def save_plan(session_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"title", "steps", "expected_revision"}:
            raise HTTPException(400, "Exact plan fields and revision required")
        try:
            return store.save_plan(owner, session_id, **body)
        except WorkConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/{session_id}/plan/{action}")
    async def plan_action(session_id: str, action: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"expected_revision"}:
            raise HTTPException(400, "Exact plan revision required")
        if action == "cancel":
            from src import agent_runs
            run = agent_runs.describe_run(session_id)
            if run and run.get("status") == "running":
                if not await agent_runs.stop_and_wait(
                    session_id, run["run_id"], reason="plan_cancelled",
                ):
                    raise HTTPException(409, "The current plan attempt is still stopping; retry shortly")
        try:
            return store.plan_action(owner, session_id, action, body["expected_revision"])
        except WorkConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/{session_id}/goal")
    async def create_goal(session_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"objective"}:
            raise HTTPException(400, "A single goal objective is required")
        return store.ensure_goal(owner, session_id, body["objective"])

    @router.post("/{session_id}/goal/{action}")
    async def goal_action(session_id: str, action: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"expected_revision"}:
            raise HTTPException(400, "Exact goal revision required")
        if action == "resume":
            from src.chat_effect_inbox import inbox
            if inbox.blocking(owner, session_id):
                raise HTTPException(409, "Tool effect must be reconciled or explicitly authorized before Goal resumes")
        # Stop the exact detached attempt before changing durable Goal state.
        # Otherwise a slow run can publish progress after Cancel/Pause and
        # resurrect the goal on another browser.
        if action in {"pause", "cancel"}:
            from src import agent_runs
            run = agent_runs.describe_run(session_id)
            if run and run["status"] == "running":
                stopped = await agent_runs.stop_and_wait(
                    session_id, run["run_id"],
                    reason="goal_paused" if action == "pause" else "goal_cancelled",
                )
                if not stopped:
                    raise HTTPException(409, "The current attempt is still stopping; retry shortly")
        try:
            goal = store.goal_action(owner, session_id, action, body["expected_revision"])
        except WorkConflict as exc:
            raise HTTPException(409, str(exc)) from None
        if action == "resume":
            from src.goal_controller import dispatch_goal_continuation
            started = await dispatch_goal_continuation(owner, session_id, reason="goal_resumed")
            if not started:
                raise HTTPException(503, "Goal continuation did not start; inspect status before retrying")
        return goal

    @router.post("/{session_id}/goal-revise")
    async def revise_goal(session_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"objective", "expected_revision", "run_id"}:
            raise HTTPException(400, "Exact goal objective, revision and run id required")
        from src.chat_effect_inbox import inbox
        if inbox.blocking(owner, session_id):
            raise HTTPException(409, "Tool effect must be reconciled before Goal revision")
        from src import agent_runs
        run = agent_runs.describe_run(session_id)
        active_id = run.get("run_id") if run and run.get("status") == "running" else None
        if active_id and body["run_id"] != active_id:
            raise HTTPException(409, "Active run changed; reload")
        if active_id:
            # Wait for the exact attempt to persist its terminal snapshot
            # before changing the objective and starting a continuation.
            stopped = await agent_runs.stop_and_wait(
                session_id, active_id, reason="goal_revised",
            )
            if not stopped:
                raise HTTPException(409, "The current attempt is still stopping; retry shortly")
        goal = store.revise_goal(owner, session_id, body["objective"], body["expected_revision"])
        from src.goal_controller import dispatch_goal_continuation
        started = await dispatch_goal_continuation(owner, session_id, reason="goal_revised")
        if not started:
            raise HTTPException(503, "Goal continuation did not start; inspect status before retrying")
        return goal

    @router.post("/{session_id}/goal-guidance")
    async def goal_guidance(session_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        body = await _json(request)
        if set(body) != {"message"}:
            raise HTTPException(400, "A single guidance message is required")
        return store.add_goal_guidance(owner, session_id, body["message"])

    @router.post("/{session_id}/goal-lease")
    async def goal_lease(session_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        if await _json(request) != {}:
            raise HTTPException(400, "Empty goal lease body required")
        from src.chat_effect_inbox import inbox
        if inbox.unknown(owner, session_id):
            raise HTTPException(409, "Unknown tool effect must be reconciled before Goal resumes")
        from src import agent_runs
        if agent_runs.is_active(session_id):
            raise HTTPException(409, "The current goal attempt is still running")
        token = store.acquire_goal_lease(owner, session_id)
        if not token:
            raise HTTPException(409, "Goal is not ready for another attempt")
        return {"lease_token": token}

    return router
