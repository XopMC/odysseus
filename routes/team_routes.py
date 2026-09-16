"""Cookie-owner team controls. No API token inherits host authority."""
import asyncio
import json
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.routing import APIRoute

from src import team_config
from src.team_store import NotFound, Conflict


class TeamRoute(APIRoute):
    """Translate owner-scoped store outcomes without exposing database details."""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except NotFound:
                raise HTTPException(404, 'Team record not found') from None
            except Conflict as exc:
                raise HTTPException(409, str(exc)) from None
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from None
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, str(exc)) from None
        return handler


def owner_for(request, *, mutation=False):
    if not team_config.enabled():
        raise HTTPException(404, 'Team mode is disabled')
    if getattr(request.state, 'api_token', False) or request.headers.get('X-Odysseus-Internal-Token'):
        raise HTTPException(403, 'Interactive owner login required')
    manager = getattr(request.app.state, 'auth_manager', None)
    # AuthMiddleware already selected and validated the scheme-specific
    # session cookie. Prefer that identity so a browser carrying both HTTP and
    # HTTPS cookies cannot be attributed to the wrong session. The fallback
    # keeps this helper compatible with direct route tests/callers.
    owner = getattr(getattr(request, 'state', None), 'current_user', None)
    if not owner:
        from core.auth import session_cookie_for_request, SESSION_COOKIE
        token = request.cookies.get(session_cookie_for_request(request)) or request.cookies.get(SESSION_COOKIE)
        owner = manager.get_username_for_token(token) if manager and token else None
    if not owner:
        raise HTTPException(401, 'Login required')
    from src.host_execution import enabled_for
    if not enabled_for(owner):
        raise HTTPException(403, 'Team host access is not enabled for this owner')
    return owner


async def body_object(request, limit=4 * 1024 * 1024):
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > limit:
            raise HTTPException(413, 'Request is too large')
    try:
        body = json.loads(raw or b'{}')
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except ValueError:
        raise HTTPException(400, 'JSON object required') from None


def runtime_for(request, *, mutation=False):
    owner = owner_for(request, mutation=mutation)
    from src.team_runtime import get_runtime
    return owner, get_runtime()


def setup_team_routes():
    router = APIRouter(prefix='/api/team', route_class=TeamRoute)

    @router.get('/capabilities')
    async def capabilities(request: Request):
        if not team_config.enabled():
            return {'enabled': False, 'host_enabled': False}
        try:
            owner_for(request)
        except HTTPException:
            return {'enabled': False, 'host_enabled': False}
        return {'enabled': True, 'host_enabled': True, 'max_workers': None,
                'engineering_enabled': os.environ.get('ODYSSEUS_ENGINEERING_ENABLED') == '1',
                'concurrency_policy': 'backend_resource_groups',
                'durable_events': True, 'external_requires_consent': True}

    @router.get('/models')
    async def models(request: Request):
        return {'models': team_config.models(owner_for(request))}

    @router.get('/presets')
    async def presets(request: Request):
        owner_for(request)
        return {'presets': team_config.PRESETS}

    @router.get('/profiles')
    async def profiles(request: Request):
        owner, runtime = runtime_for(request)
        return {'profiles': runtime.store.list_profiles(owner)}

    @router.post('/profiles')
    async def save_profile(request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        body = await body_object(request, 65536)
        profile = body.get('profile')
        if not isinstance(profile, dict) or set(profile) - {'project_path', 'test_command', 'build_command', 'install_command', 'run_command', 'constraints'}:
            raise HTTPException(400, 'Explicit project path, commands and constraints required')
        if any(not isinstance(value, str) for value in profile.values()):
            raise HTTPException(400, 'Profile fields must be strings')
        return runtime.store.save_profile(owner, body['name'], profile)

    @router.get('/session/{session_id}')
    async def session_snapshot(session_id: str, request: Request):
        owner, runtime = runtime_for(request)
        from routes.session_routes import _verify_session_owner
        _verify_session_owner(request, session_id)
        tasks = [t for t in runtime.store.list_tasks(owner) if t['metadata'].get('session_id') == session_id]
        if not tasks:
            return {'team_id': None, 'status': 'idle', 'tasks': [], 'workers': [], 'last_seq': 0}
        return runtime.snapshot(owner, tasks[0]['id'])

    @router.post('/session/{session_id}/start')
    async def start(session_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        from routes.session_routes import _verify_session_owner
        _verify_session_owner(request, session_id)
        body = await body_object(request)
        existing = [t for t in runtime.store.list_tasks(owner)
                    if t['metadata'].get('session_id') == session_id
                    and t['status'] not in {'done', 'accepted', 'cancelled'}]
        if existing:
            raise HTTPException(409, 'This chat already has an unfinished team task')
        try:
            return await runtime.create(owner, session_id, body)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get('/{team_id}/events')
    async def events(team_id: str, request: Request, after_seq: int = 0):
        owner, runtime = runtime_for(request)
        runtime.store.get_task(owner, team_id)
        async def stream():
            cursor = max(0, after_seq)
            last_heartbeat = 0
            while not await request.is_disconnected():
                found = runtime.store.events(owner, team_id, after_seq=cursor, limit=200)
                for event in found:
                    cursor = event['seq']
                    yield 'id: ' + str(cursor) + '\ndata: ' + json.dumps({'team_id': team_id, **event}, ensure_ascii=False) + '\n\n'
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= 10:
                    yield ': heartbeat\n\n'
                    last_heartbeat = now
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @router.get('/{team_id}/timeline')
    async def timeline(team_id: str, request: Request, after_seq: int = 0, limit: int = 200):
        """Finite, owner-scoped event replay used before opening the live SSE stream.

        A browser reload must not turn a running Team conversation into one
        synthetic streaming bubble.  SSE is intentionally forward-only; this
        endpoint is the durable replay counterpart and is deliberately paged so
        a very long task cannot pin an HTTP response indefinitely.
        """
        if after_seq < 0 or limit < 1 or limit > 200:
            raise HTTPException(400, 'after_seq must be nonnegative and limit must be 1..200')
        owner, runtime = runtime_for(request)
        runtime.store.get_task(owner, team_id)
        found = runtime.store.events(owner, team_id, after_seq=after_seq, limit=limit)
        return {'events': found,
                'next_cursor': found[-1]['seq'] if len(found) == limit else None}

    @router.get('/{team_id}')
    async def snapshot(team_id: str, request: Request):
        owner, runtime = runtime_for(request)
        return runtime.snapshot(owner, team_id)

    @router.get('/{team_id}/resources')
    async def resources(team_id: str, request: Request):
        owner, runtime = runtime_for(request)
        snapshot = runtime.snapshot(owner, team_id)
        result = snapshot['resources']
        result['cost_basis'] = 'Conservative reservations and usage-based estimates; not a provider invoice'
        result['scheduler'] = [
            {'worker_id': worker['id'], 'name': worker['name'], 'model': worker['profile'].get('model'),
             'resource_group': worker.get('resource_group'), 'status': worker['status'],
             'queued': worker['status'] == 'pending'} for worker in snapshot['workers']]
        recent = runtime.store.events(owner, team_id, after_seq=max(0, snapshot['last_seq'] - 500), limit=500)
        latest = {}
        for event in recent:
            if event['type'] in {'worker_metrics', 'worker_context'}:
                payload = event['payload']
                key = str(payload.get('worker_id')) + ':' + event['type']
                latest[key] = payload
        result['worker_metrics'] = list(latest.values())
        try:
            result['host'] = await runtime.host_call(owner, team_id, 'resource.snapshot', {})
        except Exception:
            result['host'] = {'available': False}
        return result

    @router.get('/{team_id}/artifacts')
    async def artifacts(team_id: str, request: Request):
        owner, runtime = runtime_for(request)
        return {'artifacts': runtime.store.list_artifacts(owner, team_id)}

    @router.get('/{team_id}/artifacts/{artifact_id}/content')
    async def artifact_content(team_id: str, artifact_id: str, request: Request):
        owner, runtime = runtime_for(request)
        from src.team_artifact_files import open_screenshot
        path, content_type = open_screenshot(runtime.store, owner, team_id, artifact_id)
        return FileResponse(path, media_type=content_type, headers={
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        })

    @router.get('/{team_id}/workers/{worker_id}/checkpoint')
    async def checkpoint(team_id: str, worker_id: str, request: Request):
        owner, runtime = runtime_for(request)
        return {'checkpoint': runtime.store.load_checkpoint(owner, team_id, worker_id)}

    @router.get('/{team_id}/intents')
    async def intents(team_id: str, request: Request):
        owner, runtime = runtime_for(request)
        return {'intents': runtime.store.list_tool_intents(owner, team_id)}

    @router.post('/{team_id}/intents/{intent_id}/resolve')
    async def resolve_intent(team_id: str, intent_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        body = await body_object(request, 65536)
        if body.get('confirmation') is not True or body.get('status') not in {'done', 'not_run'}:
            raise HTTPException(400, 'Inspect the command outcome and explicitly confirm reconciliation')
        result = body.get('result')
        if not isinstance(result, dict) or not result.get('output'):
            raise HTTPException(400, 'Record evidence from the inspected outcome')
        if body['status'] == 'done' and type(result.get('exit_code')) is not int:
            raise HTTPException(400, 'Record the inspected integer exit code for a completed command')
        if body['status'] == 'not_run':
            result = {**result, 'exit_code': 1, 'not_executed': True}
        runtime.store.resolve_tool_intent(owner, team_id, intent_id, result, status=body['status'])
        return runtime.snapshot(owner, team_id)

    @router.post('/{team_id}/guidance')
    async def guidance(team_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        runtime.store.get_task(owner, team_id)
        body = await body_object(request, 65536)
        text = str(body.get('text') or body.get('message') or '').strip()
        if not text:
            raise HTTPException(400, 'Guidance text required')
        worker_id = body.get('worker_id') or body.get('target_worker')
        if worker_id:
            runtime.store.get_worker(owner, team_id, worker_id)
        runtime.event(owner, team_id, 'guidance', {'worker_id': worker_id, 'text': text})
        return {'ok': True}

    @router.post('/{team_id}/config')
    async def config(team_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        task = runtime.store.get_task(owner, team_id)
        body = await body_object(request, 65536)
        allowed = {'auto_dispatch', 'auto_continue', 'reviewer', 'web', 'external', 'trusted_host'}
        if set(body) - allowed or any(type(value) is not bool for value in body.values()):
            raise HTTPException(400, 'Only explicit boolean task permissions may be changed')
        runtime.store.update_task_metadata(owner, team_id, {'config': {**task['metadata']['config'], **body}})
        runtime.event(owner, team_id, 'permissions_changed', body)
        return runtime.snapshot(owner, team_id)

    @router.post('/{team_id}/approvals')
    async def approve(team_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        try:
            runtime.approve_cloud(owner, team_id, await body_object(request, 65536))
        except (ValueError, KeyError, TypeError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from None
        return runtime.snapshot(owner, team_id)

    @router.post('/{team_id}/approvals/revoke')
    async def revoke(team_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        body = await body_object(request, 65536)
        runtime.store.revoke_endpoint(owner, team_id, body['endpoint_id'])
        return runtime.snapshot(owner, team_id)

    @router.post('/{team_id}/workers')
    async def add_worker(team_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        if runtime.store.get_task(owner, team_id)['status'] in {'done', 'accepted', 'cancelled'}:
            raise HTTPException(409, 'Start a new team task after completion or cancellation')
        runtime.add_worker(owner, team_id, await body_object(request, 65536), allow_pool_add=True)
        task = runtime.store.get_task(owner, team_id)
        if task['status'] in {'blocked', 'waiting_approval'}:
            runtime.store.set_task_status(owner, team_id, 'running')
        runtime.start()
        return runtime.snapshot(owner, team_id)

    @router.post('/{team_id}/workers/{worker_id}/{action}')
    @router.post('/{team_id}/tasks/{worker_id}/{action}')
    async def worker_action(team_id: str, worker_id: str, action: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        worker = runtime.store.get_worker(owner, team_id, worker_id)
        body = await body_object(request, 65536)
        if action in {'resume', 'reassign', 'reject'} and (
                worker['status'] == 'cancelled' or
                runtime.store.get_task(owner, team_id)['status'] in {'done', 'accepted', 'cancelled'}):
            raise HTTPException(409, 'Cancelled scopes cannot be resumed; create a new task or worker')
        if action == 'accept':
            await runtime.accept_result(owner, team_id, worker_id)
        elif action == 'reject':
            runtime.store.reject_worker(owner, team_id, worker_id, str(body.get('reason') or 'Human review requested changes'))
        elif action in {'pause', 'cancel', 'resume'}:
            if action in {'pause', 'cancel'}:
                runtime.store.stop_worker(owner, team_id, worker_id,
                                          status='paused' if action == 'pause' else 'cancelled')
                running = runtime.active.get((owner, team_id, worker_id))
                if running:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                if action == 'cancel':
                    await runtime.stop_host_jobs(owner, team_id, worker)
            else:
                runtime.store.update_worker(owner, team_id, worker_id, status='pending')
        elif action == 'reassign':
            if worker['status'] == 'running':
                raise HTTPException(409, 'Pause worker before changing its model')
            selection = {'endpoint_id': body['endpoint_id'], 'model': body['model']}
            allowed = runtime.store.get_task(owner, team_id)['metadata']
            pool = [allowed['leader'], *allowed['participants']]
            if selection not in [{'endpoint_id': p['endpoint_id'], 'model': p['model']} for p in pool]:
                raise HTTPException(403, 'Choose a model from this task approved pool')
            profile, group = runtime.worker_profile(owner, {**worker['profile'], **selection})
            runtime.store.update_worker(owner, team_id, worker_id, profile=profile, resource_group=group, status='pending')
        else:
            raise HTTPException(404, 'Unknown worker action')
        if action in {'resume', 'reassign', 'reject'}:
            task = runtime.store.get_task(owner, team_id)
            resumes = {**task['metadata'].get('manual_resume', {}), worker_id: worker.get('attempt_id')}
            runtime.store.update_task_metadata(owner, team_id, {'manual_resume': resumes})
            if task['status'] in {'blocked', 'waiting_approval', 'paused'}:
                runtime.store.set_task_status(owner, team_id, 'running')
        runtime.start()
        return runtime.snapshot(owner, team_id)

    @router.post('/{team_id}/host')
    async def host(team_id: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        task = runtime.store.get_task(owner, team_id)
        body = await body_object(request)
        op, args = body.get('op'), body.get('args') or {}
        allowed = {'terminal.create', 'terminal.list', 'terminal.poll', 'terminal.input', 'terminal.resize',
                   'terminal.interrupt', 'terminal.stop', 'file.call', 'file.upload', 'file.download',
                   'file.checkpoint.list', 'file.rollback',
                   'git.worktree.create', 'git.diff', 'git.integrate', 'git.rollback', 'resource.snapshot'}
        if op not in allowed or not isinstance(args, dict):
            raise HTTPException(400, 'Unknown host action')
        if not task['metadata']['config'].get('trusted_host') and op not in {'terminal.list', 'terminal.poll', 'terminal.stop', 'terminal.interrupt', 'resource.snapshot'}:
            raise HTTPException(403, 'Host access was revoked')
        # Browser controls own a team scope; a worker identifier is verified
        # before allowing observation/control of that worker's managed jobs.
        scope = team_id
        worker_id = body.get('worker_id')
        if worker_id:
            runtime.store.get_worker(owner, team_id, worker_id)
            scope = worker_id
        from src.team_host import call
        response = await call(op, args, owner=owner, scope=scope)
        if response.get('ok') and op not in {'terminal.list', 'terminal.poll', 'file.download', 'file.checkpoint.list', 'resource.snapshot'}:
            result = response.get('result') or {}
            runtime.event(owner, team_id, 'host_changed', {'op': op, 'scope': scope,
                'id': result.get('id') if isinstance(result, dict) else None})
        return response

    @router.post('/{team_id}/{action}')
    async def task_action(team_id: str, action: str, request: Request):
        owner, runtime = runtime_for(request, mutation=True)
        task = runtime.store.get_task(owner, team_id)
        if action not in {'pause', 'resume', 'cancel'}:
            raise HTTPException(404, 'Unknown team action')
        if action != 'cancel' and task['status'] in {'done', 'accepted', 'cancelled'}:
            raise HTTPException(409, 'Completed or cancelled tasks cannot be resumed; start a new task')
        if action == 'resume':
            runtime.store.update_task_metadata(owner, team_id, {'manual_resume': {
                w['id']: w.get('attempt_id') for w in runtime.store.list_workers(owner, team_id)}})
        runtime.store.set_task_status(owner, team_id, {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelled'}[action])
        if action in {'pause', 'cancel'}:
            running = [t for (o, tid, _), t in runtime.active.items() if o == owner and tid == team_id]
            for execution in running:
                execution.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            if action == 'cancel':
                # Fence/cancel dispatch first, then stop accepted host jobs.
                await runtime.stop_host_jobs(owner, team_id)
        else:
            runtime.start()
        return runtime.snapshot(owner, team_id)

    return router
