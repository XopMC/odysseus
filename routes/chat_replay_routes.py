"""Read-only owner-gated access to bounded detached-run replay artifacts."""
import os
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from routes.session_routes import _verify_session_owner
from src import agent_runs
from src.chat_replay_log import ReplayLog


def setup_chat_replay_routes():
    router = APIRouter()

    @router.get('/api/chat/replay/{session_id}')
    async def replay(request: Request, session_id: str, run_id: str,
                     after_seq: int = -1, limit: int = 100):
        _verify_session_owner(request, session_id)
        if os.getenv('ODYSSEUS_DURABLE_CHAT_REPLAY') != '1':
            raise HTTPException(404, 'Durable chat replay is disabled')
        try:
            log = ReplayLog(agent_runs.replay_root(), run_id, session_id)
            run = agent_runs.get_active_run(session_id)
            return JSONResponse(log.page(after_seq, limit, active=bool(run and run.run_id == run_id)),
                                headers={'Cache-Control': 'private, no-store'})
        except FileNotFoundError:
            raise HTTPException(404, 'Replay not found') from None
        except ValueError:
            raise HTTPException(400, 'Invalid replay request') from None
        except OSError:
            raise HTTPException(503, 'Replay storage unavailable') from None

    return router
