"""Owner-scoped UI API for parallel Agent subagents."""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src.owner_identity import auth_disabled
from src.subagent_runtime import runtime
from src.subagent_limits import MAX_ACTIVE_PER_MODEL


def _owner(request: Request, session_id: str):
    _verify_session_owner(request, session_id)
    owner = effective_user(request)
    if not owner and not auth_disabled():
        raise HTTPException(401, "Login required")
    return owner


async def _body(request: Request, limit=32 * 1024):
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, "Request is too large")
    try:
        value = json.loads(raw or b"{}")
    except Exception:
        raise HTTPException(400, "JSON object required") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON object required")
    return value


def setup_subagent_routes():
    router = APIRouter(prefix="/api/chat/subagents")

    @router.get("/{session_id}")
    async def snapshot(session_id: str, request: Request):
        owner = _owner(request, session_id)
        rows = runtime.list(owner, session_id)
        return {
            "subagents": rows,
            "active": sum(row["status"] in {"queued", "running", "waiting_user", "stopping"} for row in rows),
            "max_active_per_model": MAX_ACTIVE_PER_MODEL,
            "latest_cursor": runtime.latest_cursor(owner, session_id),
        }

    @router.get("/{session_id}/events")
    async def events(session_id: str, request: Request, after: int = 0,
                     limit: int = 200, child_id: str = "", tail: bool = False):
        rows = runtime.events(_owner(request, session_id), session_id, after=after,
                              limit=limit, child_id=child_id or None, tail=tail)
        return {"events": rows, "next_cursor": rows[-1]["seq"] if rows else after}

    @router.get("/{session_id}/events/stream")
    async def event_stream(session_id: str, request: Request, after: int = 0):
        owner = _owner(request, session_id)
        try:
            cursor = int(request.headers.get("Last-Event-ID") or after)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid event cursor") from None
        if cursor < 0:
            raise HTTPException(400, "Invalid event cursor")

        async def generate():
            nonlocal cursor
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                rows = runtime.events(owner, session_id, after=cursor, limit=200)
                if rows:
                    for row in rows:
                        cursor = int(row["seq"])
                        yield f"id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.5)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={
            "Cache-Control": "private, no-store", "X-Accel-Buffering": "no",
        })

    @router.get("/{session_id}/{child_id}")
    async def detail(session_id: str, child_id: str, request: Request):
        row = runtime.get(_owner(request, session_id), session_id, child_id)
        if not row:
            raise HTTPException(404, "Subagent not found")
        return row

    @router.post("/{session_id}/{child_id}/message")
    async def message(session_id: str, child_id: str, request: Request):
        data = await _body(request)
        result = await runtime.message(_owner(request, session_id), session_id,
                                       child_id, data.get("message") or "")
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    @router.post("/{session_id}/{child_id}/stop")
    async def stop(session_id: str, child_id: str, request: Request):
        result = await runtime.stop(_owner(request, session_id), session_id, child_id)
        if result.get("error"):
            raise HTTPException(404, result["error"])
        return result

    @router.delete("/{session_id}/{child_id}")
    async def remove(session_id: str, child_id: str, request: Request):
        result = await runtime.remove(_owner(request, session_id), session_id, child_id)
        if result.get("error"):
            raise HTTPException(404, result["error"])
        return result

    return router
