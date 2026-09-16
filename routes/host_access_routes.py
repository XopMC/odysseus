"""Human-only one-shot elevation; never accepts internal/delegated authority."""
import asyncio
import json
import os
import shlex
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from src.constants import STATIC_DIR


def authorize(request, mutation=False):
    from src.host_execution import enabled_for
    if (getattr(request.state, 'api_token', False)
            or request.headers.get('X-Odysseus-Internal-Token')):
        raise HTTPException(403, 'Interactive owner login required')
    manager = getattr(request.app.state, 'auth_manager', None)
    from core.auth import session_cookie_for_request, SESSION_COOKIE
    cookie = request.cookies.get(session_cookie_for_request(request)) or request.cookies.get(SESSION_COOKIE)
    owner = manager.get_username_for_token(cookie) if manager and cookie else None
    if not owner or not enabled_for(owner):
        raise HTTPException(403, 'Host access is not enabled for this account')
    if request.url.scheme != 'https' and request.url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(400, 'Use HTTPS or a localhost SSH tunnel before entering a sudo password')
    if mutation:
        if request.headers.get('x-odysseus-host-action') != 'one-shot-sudo':
            raise HTTPException(403, 'Explicit host action required')
    return owner


def setup_host_access_routes():
    router = APIRouter()

    @router.get('/host-access')
    async def page(request: Request):
        authorize(request)
        return FileResponse(Path(STATIC_DIR) / 'host-access.html', headers={'Cache-Control': 'no-store'})

    @router.post('/api/host-access/sudo')
    async def sudo(request: Request):
        authorize(request, True)
        if int(request.headers.get('content-length', '0')) > 65536:
            raise HTTPException(413, 'Request too large')
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, 'Request too large')
        try:
            body = json.loads(raw)
            argv = shlex.split(body['command'])
            password = body['password']
            if not argv or not isinstance(password, str) or not password or '\n' in password:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, 'Command and password required') from None
        from src.host_execution import ssh_argv
        helper = os.environ.get('ODYSSEUS_HOST_SUDO_HELPER', '/usr/local/libexec/odysseus-host-sudo.py')
        proc = await asyncio.create_subprocess_exec(
            *ssh_argv('/usr/bin/python3 -I ' + shlex.quote(helper)), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(json.dumps({'argv': argv, 'password': password}).encode()), 40)
            result = json.loads(output)
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        except (asyncio.TimeoutError, ValueError):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            result = {'error': 'Host connection failed; command state is unknown. Inspect before retrying.', 'exit_code': 124}
        # Do not include body, command, or credentials in logs/history.
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    return router
