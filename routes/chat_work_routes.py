"""Owner-scoped public API for durable Plan and Goal controls."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute

from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src.chat_work_store import WorkConflict, WorkNotFound, store


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
    if not owner:
        raise HTTPException(401, "Login required")
    if mutation and request.headers.get("origin") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Same-origin browser action required")
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

    @router.get("/{session_id}/events")
    async def events(session_id: str, request: Request, after: int = 0, limit: int = 100):
        rows = store.events(_owner(request, session_id), session_id, after=after, limit=limit)
        return {"events": rows, "next_cursor": rows[-1]["seq"] if rows else after}

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
        try:
            goal = store.goal_action(owner, session_id, action, body["expected_revision"])
        except WorkConflict as exc:
            raise HTTPException(409, str(exc)) from None
        if action == "pause":
            from src import agent_runs
            run = agent_runs.describe_run(session_id)
            if run and run["status"] == "running":
                agent_runs.stop(session_id, run["run_id"])
        return goal

    @router.post("/{session_id}/goal-lease")
    async def goal_lease(session_id: str, request: Request):
        owner = _owner(request, session_id, mutation=True)
        if await _json(request) != {}:
            raise HTTPException(400, "Empty goal lease body required")
        from src import agent_runs
        if agent_runs.is_active(session_id):
            raise HTTPException(409, "The current goal attempt is still running")
        token = store.acquire_goal_lease(owner, session_id)
        if not token:
            raise HTTPException(409, "Goal is not ready for another attempt")
        return {"lease_token": token}

    return router
