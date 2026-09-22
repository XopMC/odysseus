"""Read-only owner-gated access to bounded detached-run replay artifacts."""
import os
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from routes.session_routes import _verify_session_owner
from src import agent_runs
from src.auth_helpers import effective_user, require_chat_api_token_scope
from src.chat_replay_log import ReplayLog
from src.incident_export import build_incident_archive


def setup_chat_replay_routes():
    router = APIRouter()

    @router.get('/api/chat/incident/{session_id}', dependencies=[Depends(require_chat_api_token_scope)])
    def incident_export(request: Request, session_id: str):
        _verify_session_owner(request, session_id)
        archive = build_incident_archive(session_id, effective_user(request))
        return Response(
            archive, media_type='application/zip',
            headers={
                'Cache-Control': 'private, no-store',
                'Content-Disposition': 'attachment; filename="odysseus-incident.zip"',
                'X-Content-Type-Options': 'nosniff',
            },
        )

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
