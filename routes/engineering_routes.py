"""Feature-gated, interactive-owner engineering controls; never a model proxy."""
import os

from fastapi import APIRouter, HTTPException, Request
from routes.team_routes import TeamRoute, body_object, owner_for, runtime_for
from src.engineering_store import EngineeringStore


class EngineeringRoute(TeamRoute):
    """Live owner state and command output must not be reused from HTTP caches."""
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), 'Cache-Control': 'no-store'}
                raise
            response.headers['Cache-Control'] = 'no-store'
            return response
        return handler


def enabled():
    return os.environ.get('ODYSSEUS_ENGINEERING_ENABLED') == '1'


def context(request, *, mutation=False):
    if not enabled():
        raise HTTPException(404, 'Engineering workspace is disabled')
    owner, runtime = runtime_for(request, mutation=mutation)
    return owner, EngineeringStore(runtime.store)


def setup_engineering_routes():
    router = APIRouter(prefix='/api/team/engineering', route_class=EngineeringRoute)

    @router.get('/capabilities')
    async def capabilities(request: Request):
        if not enabled():
            return {'enabled': False}
        owner_for(request)
        return {'enabled': True, 'stage': 'foundation', 'features': {
            'projects': True, 'policy': True, 'tool_catalog': True, 'model_probe': True,
            'check_profiles': True,
            'check_runs': True,
            'requirements': True,
            'baseline_comparison': True,
            'context_policy': os.environ.get('ODYSSEUS_CONTEXT_POLICY_ENABLED') == '1',
            'isolated_execution': os.environ.get('ODYSSEUS_ISOLATED_RUNNER_ENABLED') == '1', 'cross_host_workspaces': False,
            # The server-owned LSP bridge is live. Individual language servers
            # remain discoverable capabilities of the selected execution host.
            'lsp': True, 'debug': False, 'experiments': False}}

    @router.get('/hosts')
    async def hosts(request: Request):
        owner, _ = context(request)
        from src.engineering_hosts import public_hosts
        return {'hosts': public_hosts(owner)}

    @router.get('/context-policy')
    async def context_policy(request: Request, project_id: str = '', task_id: str = '', worker_id: str = '', session_id: str = ''):
        owner, store = context(request)
        from src.context_policy_store import ContextPolicyStore
        policies = ContextPolicyStore(store.team)
        scope = dict(project_id=project_id, task_id=task_id, worker_id=worker_id, session_id=session_id)
        return {**policies.get(owner, **scope),
                'last_completed_request': policies.last_completed_request(owner, **scope)}

    @router.post('/context-policy')
    async def save_context_policy(request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 16384)
        if set(body) - {'session_id'} != {'project_id', 'task_id', 'worker_id', 'overrides', 'expected_revisions'}:
            raise HTTPException(400, 'Exact context scope, overrides and revision vector required')
        if any(not isinstance(body.get(key, ''), str) for key in ('project_id', 'task_id', 'worker_id', 'session_id')):
            raise HTTPException(400, 'Context scope IDs must be strings')
        from src.context_policy_store import ContextPolicyStore
        policies = ContextPolicyStore(store.team)
        saved = policies.save(owner, **body)
        scope = {key: body.get(key, '') for key in ('project_id', 'task_id', 'worker_id', 'session_id')}
        return {**saved, 'last_completed_request': policies.last_completed_request(owner, **scope)}

    @router.get('/context-policy/events')
    async def context_policy_events(request: Request, after_seq: int = 0, limit: int = 100):
        owner, store = context(request)
        from src.context_policy_store import ContextPolicyStore
        events = ContextPolicyStore(store.team).events(owner, after_seq=after_seq, limit=limit)
        return {'events': events, 'next_cursor': events[-1]['seq'] if events else after_seq}

    @router.get('/context-presets')
    async def context_presets(request: Request, after_seq: int = 0, limit: int = 50, query: str = ''):
        owner, store = context(request)
        from src.context_policy_store import ContextPolicyStore
        return ContextPolicyStore(store.team).list_presets(owner, after_seq=after_seq, limit=limit, query=query)

    @router.post('/context-presets')
    async def save_context_preset(request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 16384)
        if set(body) - {'kind'} != {'name', 'values', 'preset_id', 'expected_revision'}:
            raise HTTPException(400, 'Exact preset fields and revision required')
        from src.context_policy_store import ContextPolicyStore
        return ContextPolicyStore(store.team).save_preset(owner, **body)

    @router.patch('/context-presets/{preset_id}')
    async def rename_context_preset(request: Request, preset_id: str):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 2048)
        if set(body) != {'name', 'expected_revision'}:
            raise HTTPException(400, 'Preset rename requires only name and revision')
        from src.context_policy_store import ContextPolicyStore
        return ContextPolicyStore(store.team).rename_preset(owner, preset_id, **body)

    @router.delete('/context-presets/{preset_id}')
    async def delete_context_preset(request: Request, preset_id: str, expected_revision: int):
        owner, store = context(request, mutation=True)
        from src.context_policy_store import ContextPolicyStore
        ContextPolicyStore(store.team).delete_preset(owner, preset_id, expected_revision=expected_revision)
        return {'deleted': True}

    @router.get('/model-probe')
    async def describe_model_probe(request: Request, endpoint_id: str, model: str):
        owner, _ = context(request)
        from src.engineering_probe import describe
        return describe(owner, endpoint_id, model)

    @router.post('/model-probe')
    async def run_model_probe(request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 4096)
        if set(body) != {'endpoint_id', 'model', 'confirmation', 'expected_config_digest'}:
            raise HTTPException(400, 'Select and explicitly confirm the current model configuration')
        from src.engineering_probe import describe
        from src.engineering_operations import get_manager
        description = describe(owner, body['endpoint_id'], body['model'])
        if not description['supported']:
            raise HTTPException(400, description['reason'])
        if body['expected_config_digest'] != description['scope']['config_digest']:
            raise HTTPException(409, 'Model configuration changed; confirm it again')
        manager = get_manager(store.team)
        operation = manager.store.create(owner, body)
        manager.start()
        return operation

    @router.get('/operations')
    async def operations(request: Request, kind: str = 'model_probe', after_id: str = '', limit: int = 50,
                         active_only: bool = False, project_id: str = ''):
        owner, store = context(request)
        from src.engineering_operations import Operations
        return Operations(store.team).list(owner, kind=kind, after_id=after_id, limit=limit,
                                          active_only=active_only, project_id=project_id)

    @router.get('/operations/{operation_id}')
    async def operation(operation_id: str, request: Request):
        owner, store = context(request)
        from src.engineering_operations import Operations
        return Operations(store.team).get(owner, operation_id)

    @router.post('/operations/{operation_id}/cancel')
    async def cancel_operation(operation_id: str, request: Request):
        owner, store = context(request, mutation=True)
        if await body_object(request, 4096) != {}:
            raise HTTPException(400, 'Empty cancellation body required')
        from src.engineering_operations import Operations
        return Operations(store.team).cancel(owner, operation_id)

    @router.post('/hosts/{host_id}/probe')
    async def probe(host_id: str, request: Request):
        owner, _ = context(request, mutation=True)
        body = await body_object(request, 4096)
        if body != {'confirmation': True}:
            raise HTTPException(400, 'Confirm the read-only runner handshake explicitly')
        from src.engineering_hosts import call
        response = await call(host_id, 'runner.capabilities', {}, owner=owner, scope='engineering-probe')
        if not response.get('ok'):
            raise HTTPException(502, 'Runner handshake failed; check host configuration')
        return response['result']

    @router.get('/projects')
    async def projects(request: Request, after_id: str = '', limit: int = 100):
        owner, store = context(request)
        records = store.list_projects(owner, after_id=after_id, limit=limit)
        return {'projects': records, 'next_cursor': records[-1]['id'] if len(records) == limit else None}

    @router.post('/projects')
    async def create_project(request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 16384)
        if set(body) != {'name', 'root', 'host_id'}:
            raise HTTPException(400, 'Only name, root and configured host_id are accepted')
        from src.engineering_hosts import public_hosts
        if body['host_id'] not in {host['id'] for host in public_hosts(owner)}:
            raise HTTPException(400, 'Execution host is not configured for this owner')
        return store.create_project(owner, **body)

    @router.get('/projects/{project_id}')
    async def project(project_id: str, request: Request):
        owner, store = context(request)
        return store.get_project(owner, project_id)

    @router.post('/projects/{project_id}/policy')
    async def policy(project_id: str, request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 4096)
        if set(body) != {'expected_revision', 'access_mode', 'confirmation'}:
            raise HTTPException(400, 'Policy requires revision, access_mode and confirmation')
        if body['access_mode'] == 'isolated':
            # A feature flag alone is not proof of a runner.  Verify the fixed
            # server-owned operation before persisting a policy that needs it.
            project = store.get_project(owner, project_id)
            from src.engineering_hosts import call
            response = await call(project['host_id'], 'runner.capabilities', {}, owner=owner,
                                  scope='engineering-policy-' + project_id)
            if not response.get('ok') or 'sandbox.command.start' not in response.get('result', {}).get('supported_ops', []):
                raise HTTPException(409, 'Verified isolated runner is unavailable on this host')
        return store.set_policy(owner, project_id, **body)

    @router.get('/projects/{project_id}/events')
    async def events(project_id: str, request: Request, after_seq: int = 0, limit: int = 100):
        owner, store = context(request)
        records = store.events(owner, project_id, after_seq=after_seq, limit=limit)
        return {'events': records, 'next_cursor': records[-1]['seq'] if records else after_seq}

    @router.get('/projects/{project_id}/check-profiles')
    async def check_profiles(project_id: str, request: Request, after_id: str = '', limit: int = 50):
        owner, store = context(request)
        from src.engineering_checks import EngineeringChecks
        return EngineeringChecks(store.team).list_profiles(owner, project_id, after_id=after_id, limit=limit)

    @router.get('/projects/{project_id}/requirements')
    async def check_requirements(project_id: str, request: Request, after_id: str = '', limit: int = 50):
        owner, store = context(request)
        from src.engineering_checks import EngineeringChecks
        return EngineeringChecks(store.team).list_requirements(owner, project_id, after_id=after_id, limit=limit)

    @router.post('/projects/{project_id}/requirements')
    async def approve_requirement(project_id: str, request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 16384)
        if set(body) != {'title', 'profile_ids', 'mandatory', 'requirement_id', 'expected_revision', 'confirmation'}:
            raise HTTPException(400, 'Exact requirement, approved profiles, revision and confirmation required')
        if body.pop('confirmation') is not True:
            raise HTTPException(403, 'Explicit acceptance-criterion approval required')
        if body['requirement_id'] is not None and not isinstance(body['requirement_id'], str):
            raise HTTPException(400, 'Invalid requirement identity')
        from src.engineering_checks import EngineeringChecks
        return EngineeringChecks(store.team).set_requirement(owner, project_id, **body)

    @router.get('/projects/{project_id}/check-readiness')
    async def check_readiness(project_id: str, request: Request):
        owner, store = context(request)
        from src.engineering_check_runner import EngineeringCheckRunner
        # Workspace identity is obtained from the authenticated runner, never
        # from a client/model claim or an old green result shown in the browser.
        return await EngineeringCheckRunner(store.team).readiness(owner, project_id)

    @router.get('/projects/{project_id}/check-comparison')
    async def check_comparison(project_id: str, request: Request, baseline_run_id: str, check_run_id: str):
        owner, store = context(request)
        from src.engineering_checks import EngineeringChecks
        return EngineeringChecks(store.team).compare_baseline(owner, project_id, baseline_run_id, check_run_id)

    @router.get('/projects/{project_id}/check-runs')
    async def list_check_runs(project_id: str, request: Request, after_id: str = '', limit: int = 50):
        owner, store = context(request)
        if not 1 <= limit <= 100:
            raise HTTPException(400, 'Invalid check history page size')
        from src.engineering_checks import EngineeringChecks
        rows = EngineeringChecks(store.team).list_runs(owner, project_id, after_id=after_id, limit=limit + 1)
        return {'runs': rows[:limit], 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    @router.post('/projects/{project_id}/check-runs')
    async def queue_check(project_id: str, request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 4096)
        if 'project_id' in body:
            raise HTTPException(400, 'Project is selected by the route')
        from src.engineering_operations import get_manager
        manager = get_manager(store.team)
        result = manager.store.create_check(owner, {**body, 'project_id': project_id})
        manager.start()
        return result

    @router.get('/projects/{project_id}/check-runs/{run_id}')
    async def observe_check(project_id: str, run_id: str, request: Request):
        owner, store = context(request)
        from src.engineering_check_runner import EngineeringCheckRunner
        # GET may reconcile authenticated evidence, but must never dispatch code.
        return await EngineeringCheckRunner(store.team).observe(owner, project_id, run_id)

    @router.get('/projects/{project_id}/check-runs/{run_id}/output')
    async def check_output(project_id: str, run_id: str, request: Request, offset: int = 0, limit: int = 16000):
        owner, store = context(request)
        from src.engineering_check_runner import EngineeringCheckRunner
        return await EngineeringCheckRunner(store.team).output(owner, project_id, run_id, offset=offset, limit=limit)

    @router.post('/projects/{project_id}/check-runs/{run_id}/stop')
    async def stop_check(project_id: str, run_id: str, request: Request):
        owner, store = context(request, mutation=True)
        body = await body_object(request, 4096)
        if body != {'confirmation': True}:
            raise HTTPException(400, 'Explicit confirmation for this check process required')
        from src.engineering_check_runner import EngineeringCheckRunner
        return await EngineeringCheckRunner(store.team).stop(owner, project_id, run_id, confirmation=True)

    @router.post('/projects/{project_id}/check-profiles')
    async def approve_check_profile(project_id: str, request: Request):
        # Interactive approval is not execution permission and never runs code.
        owner, store = context(request, mutation=True)
        body = await body_object(request, 32768)
        required = {'name', 'command', 'confirmation', 'profile_id', 'expected_revision'}
        if set(body) != required:
            raise HTTPException(400, 'Exact check command, approval and revision required')
        if body['profile_id'] is not None and not isinstance(body['profile_id'], str):
            raise HTTPException(400, 'Invalid check profile identity')
        from src.engineering_checks import EngineeringChecks
        return EngineeringChecks(store.team).approve_profile(owner, project_id, **body)

    @router.get('/tools')
    async def tools(request: Request, project_id: str):
        owner, store = context(request)
        project = store.get_project(owner, project_id)
        from src.engineering_catalog import project_catalog
        return {'tools': project_catalog(owner, project), 'project_revision': project['revision']}

    @router.post('/projects/{project_id}/lsp/{operation}')
    async def lsp_operation(project_id: str, operation: str, request: Request):
        """Narrow runner adapter; executable, cwd and authorization are server-owned."""
        owner, store = context(request, mutation=True)
        project = store.get_project(owner, project_id)
        fields = {
            'discover': set(),
            'start': {'language', 'idempotency_key', 'expected_revision'},
            'request': {'id', 'method', 'params', 'expected_revision'},
            'diagnostics': {'id', 'uri', 'expected_revision'},
            'stop': {'id'},
        }
        if operation not in fields:
            raise HTTPException(404, 'Unsupported LSP operation')
        body = await body_object(request, 256 * 1024)
        if set(body) != fields[operation]:
            raise HTTPException(400, 'Unexpected or missing LSP fields')
        args = dict(body)
        if operation not in {'discover', 'stop'}:
            revision = args.pop('expected_revision')
            if type(revision) is not int:
                raise HTTPException(400, 'expected_revision must be an integer')
            project = store.assert_access(owner, project_id, effect='execute', revision=revision)
            args['execution_authorized'] = True
        if operation == 'start':
            args['cwd'] = project['root']
        from src.engineering_hosts import call
        response = await call(project['host_id'], 'lsp.' + operation, args,
                              owner=owner, scope='engineering-project-' + project_id)
        if not response.get('ok'):
            raise HTTPException(502, 'LSP runner operation failed or is unsupported')
        return response['result']

    def mcp_context(request, mutation=False):
        owner, store = context(request, mutation=mutation)
        from src import team_mcp
        if not team_mcp.enabled():
            raise HTTPException(404, 'Reviewed Team MCP is disabled')
        team_mcp.assert_review_access(owner)
        return owner, team_mcp.TeamMCPStore(store.team)

    @router.get('/mcp/reviews')
    async def mcp_reviews(request: Request):
        owner, store = mcp_context(request)
        from src.team_mcp import review_catalogue
        return {'catalogue': review_catalogue(), 'reviews': store.list(owner), 'read_only': True}

    @router.post('/mcp/reviews')
    async def mcp_review(request: Request):
        owner, store = mcp_context(request, mutation=True)
        body = await body_object(request, 16384)
        if set(body) != {'tool_id', 'schema_digest', 'effects', 'roles', 'expected_revision', 'confirmation'}:
            raise HTTPException(400, 'Exact tool identity, digest, effects, roles, revision and confirmation required')
        from src.team_mcp import review_catalogue
        current = next((row for row in review_catalogue() if row['tool_id'] == body['tool_id']), None)
        if current is None or not current['available'] or current['schema_digest'] != body['schema_digest']:
            raise HTTPException(409, 'Tool configuration changed or is unavailable; review the current schema')
        return store.review(owner, **body)

    @router.post('/mcp/reviews/revoke')
    async def mcp_revoke(request: Request):
        owner, store = mcp_context(request, mutation=True)
        body = await body_object(request, 4096)
        if set(body) != {'tool_id', 'expected_revision'}:
            raise HTTPException(400, 'Exact tool identity and revision required')
        return store.revoke(owner, **body)

    return router
