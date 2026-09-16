from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.engineering_routes import setup_engineering_routes
from src.team_store import TeamStore
from src import team_runtime, host_execution, engineering_hosts


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_TEAM_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '1')
    monkeypatch.setattr(host_execution, 'enabled_for', lambda owner: owner in {'alice', 'bob'})
    monkeypatch.setattr(engineering_hosts, 'public_hosts', lambda owner: [{'id': 'legacy-jetson', 'name': 'Jetson', 'platform': 'unknown', 'status': 'configured'}])
    async def folder_probe(*_args, **_kwargs):
        return {'ok': True, 'result': {'exit_code': 0, 'entries': []}}
    monkeypatch.setattr(engineering_hosts, 'call', folder_probe)
    store = TeamStore(tmp_path / 'teams.db')
    monkeypatch.setattr(team_runtime, 'get_runtime', lambda: SimpleNamespace(store=store))
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(get_username_for_token=lambda token: {'alice-cookie': 'alice', 'bob-cookie': 'bob'}.get(token))
    app.include_router(setup_engineering_routes())
    with TestClient(app, cookies={'odysseus_session': 'alice-cookie'}, headers={'Origin': 'http://testserver'}) as value:
        yield value


ROOT = '/api/team/engineering'


def test_chat_context_policy_api_real_database_owner_and_versions(client, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core import database
    engine = create_engine('sqlite:///' + str(tmp_path / 'chat-policy.db'))
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    @contextmanager
    def connection():
        with factory() as db:
            yield db
    monkeypatch.setattr(database, 'get_db_session', connection)
    try:
        with factory() as db:
            for identity, owner in [('a', 'alice'), ('b', 'alice'), ('private', 'bob')]:
                db.add(database.Session(id=identity, owner=owner, name=identity, endpoint_url='http://fixture.invalid', model='fixture'))
            db.commit()
        url = ROOT + '/context-policy'
        initial = client.get(url, params={'session_id': 'a'}).json()
        assert initial['revisions'] == {'owner': 0, 'session:a': 0}
        body = {'session_id': 'a', 'project_id': '', 'task_id': '', 'worker_id': '',
                'overrides': {'output_reserve': 1536}, 'expected_revisions': initial['revisions']}
        saved = client.post(url, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()['effective']['output_reserve'] == 1536
        assert client.post(url, json=body).status_code == 409
        assert client.get(url, params={'session_id': 'b'}).json()['effective']['output_reserve'] == 4096
        assert client.get(url).json()['configured'] is False
        assert client.get(url, params={'session_id': 'private'}).status_code == 404
        assert client.get(url, params={'session_id': 'a', 'task_id': 'task'}).status_code == 400
        client.cookies.set('odysseus_session', 'bob-cookie')
        assert client.get(url, params={'session_id': 'a'}).status_code == 404
        assert client.post(url, json=body).status_code == 404
        assert client.get(url + '/events').json()['events'] == []
    finally:
        engine.dispose()


def test_context_presets_owner_cas_and_csrf(client):
    url = ROOT + '/context-presets'
    policy = client.get(ROOT + '/context-policy').json()['effective']
    body = {'name': 'Долгая работа', 'values': policy, 'preset_id': '', 'expected_revision': 0}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    preset = response.json()
    assert client.get(ROOT + '/context-policy').json()['configured'] is False
    assert client.get(url).json()['items'] == [preset]
    assert client.get(url, params={'query': 'ДОЛГАЯ'}).json()['items'] == [preset]
    assert client.get(url, params={'query': 'missing'}).json()['items'] == []
    assert client.get(url, params={'query': 'x' * 121}).status_code == 400
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).json()['items'] == []
    assert client.delete(url + '/' + preset['id'], params={'expected_revision': 1}).status_code == 404
    client.cookies.set('odysseus_session', 'alice-cookie')
    body.update(preset_id=preset['id'], expected_revision=1, name='Renamed')
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 409
    assert client.delete(url + '/' + preset['id'], params={'expected_revision': 1}).status_code == 409
    renamed = client.patch(url + '/' + preset['id'], json={'name': 'Only name', 'expected_revision': 2})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()['values'] == policy
    assert client.patch(url + '/' + preset['id'], json={'name': 'Stale', 'expected_revision': 2}).status_code == 409
    assert client.patch(url + '/' + preset['id'], json={'name': 'Bad', 'expected_revision': 3, 'values': {}}).status_code == 400
    assert client.delete(url + '/' + preset['id'], params={'expected_revision': 3}).status_code == 200
    assert client.get(url).json()['items'] == []


def test_partial_preset_api_preserves_fields_and_checks_destination(client):
    url = ROOT + '/context-presets'
    body = {'name': 'Target only', 'values': {'target_percent': 80}, 'kind': 'overrides',
            'preset_id': '', 'expected_revision': 0}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['values'] == {'target_percent': 80}
    assert response.json()['kind'] == 'overrides'
    assert client.post(url, json={**body, 'kind': 'full'}).status_code == 400
    assert client.post(url, json={**body, 'kind': 'invalid'}).status_code == 400
    policy = client.get(ROOT + '/context-policy').json()
    assert policy['configured'] is False
    destination = {'session_id': '', 'project_id': '', 'task_id': '', 'worker_id': '',
                   'expected_revisions': policy['revisions'], 'overrides': response.json()['values']}
    assert client.post(ROOT + '/context-policy', json=destination).status_code == 400
    assert client.get(ROOT + '/context-policy').json()['configured'] is False


def test_context_policy_versions_scope_and_no_side_effecting_probe(client):
    url = ROOT + '/context-policy'
    initial = client.get(url).json()
    assert initial['configured'] is False
    body = {'project_id': '', 'task_id': '', 'worker_id': '',
            'overrides': {'trigger_percent': 80}, 'expected_revisions': initial['revisions']}
    saved = client.post(url, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['effective']['trigger_percent'] == 80
    assert client.post(url, json=body).status_code == 409
    assert client.get(url + '/events').json()['events'][0]['revision'] == 1
    project = create(client)
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).json()['configured'] is False
    assert client.get(url + '/events').json()['events'] == []
    assert client.get(url + '?project_id=' + project['id']).status_code == 404


def create(client):
    response = client.post(ROOT + '/projects', json={'name': 'Example', 'root': '/work/example', 'host_id': 'legacy-jetson'})
    assert response.status_code == 200, response.text
    return response.json()


def test_context_completed_request_api_preserves_observation_and_owner_boundary(client):
    store = team_runtime.get_runtime().store
    task = store.create_task('alice', 'Task')['id']
    worker = store.add_worker('alice', task, 'Worker')['id']
    store.add_event('alice', task, 'worker_metrics', {'worker_id': worker,
                    'context_policy': {'max_output_tokens': 512, 'revisions': {}}})
    url = ROOT + '/context-policy'
    scope = {'project_id': '', 'task_id': task, 'worker_id': worker}
    loaded = client.get(url, params=scope).json()
    observation = loaded['last_completed_request']
    assert observation['context_policy']['max_output_tokens'] == 512
    saved = client.post(url, json={**scope, 'overrides': {'output_reserve': 768},
                                  'expected_revisions': loaded['revisions']})
    assert saved.status_code == 200, saved.text
    assert saved.json()['last_completed_request'] == observation
    assert saved.json()['effective']['output_reserve'] == 768
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url, params=scope).status_code == 404


def test_project_policy_workflow_and_other_owner(client):
    project = create(client)
    assert project['access_mode'] is None
    url = ROOT + '/projects/' + project['id']
    assert client.get(ROOT + '/projects').json()['projects'][0]['id'] == project['id']
    assert client.post(url + '/policy', json={'expected_revision': 1, 'access_mode': 'trusted_host', 'confirmation': True}).status_code == 200
    assert client.post(url + '/policy', json={'expected_revision': 1, 'access_mode': None, 'confirmation': True}).status_code == 409
    assert client.post(url + '/policy', json={'expected_revision': 2, 'access_mode': 'isolated', 'confirmation': True}).status_code == 403
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).status_code == 404
    assert client.get(url + '/events').status_code == 404
    assert client.get(ROOT + '/projects').json()['projects'] == []


def test_project_memory_api_owner_confirmation_and_revision(client):
    project = create(client)
    url = ROOT + '/projects/' + project['id'] + '/memory'
    body = {'memory_id': '', 'kind': 'architecture', 'text': 'Runner owns PTY state',
            'source': 'host-runner protocol', 'state': 'verified',
            'expected_revision': 0, 'confirmation': False}
    assert client.post(url, json=body).status_code == 403
    saved = client.post(url, json={**body, 'confirmation': True})
    assert saved.status_code == 200, saved.text
    item = saved.json()
    assert client.get(url).json()['items'] == [item]
    stale = {'memory_id': item['id'], 'kind': item['kind'], 'text': 'changed',
             'source': item['source'], 'state': item['state'],
             'expected_revision': 0, 'confirmation': True}
    assert client.post(url, json=stale).status_code == 409
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).status_code == 404
    assert client.request('DELETE', url + '/' + item['id'], json={'expected_revision': 1, 'confirmation': True}).status_code == 404
    client.cookies.set('odysseus_session', 'alice-cookie')
    assert client.request('DELETE', url + '/' + item['id'], json={'expected_revision': 1, 'confirmation': False}).status_code == 403
    assert client.request('DELETE', url + '/' + item['id'], json={'expected_revision': 1, 'confirmation': True}).status_code == 200
    assert client.get(url).json()['items'] == []


def test_isolated_policy_requires_verified_runner_capability(client, monkeypatch):
    project = create(client)
    calls = []
    async def capability(host_id, op, args, *, owner, scope):
        calls.append((host_id, op, args, owner, scope))
        return {'ok': True, 'result': {'supported_ops': ['sandbox.command.start']}}
    monkeypatch.setenv('ODYSSEUS_ISOLATED_RUNNER_ENABLED', '1')
    monkeypatch.setattr(engineering_hosts, 'call', capability)
    url = ROOT + '/projects/' + project['id'] + '/policy'
    saved = client.post(url, json={'expected_revision': 1, 'access_mode': 'isolated', 'confirmation': True})
    assert saved.status_code == 200, saved.text
    assert saved.json()['access_mode'] == 'isolated'
    assert calls == [('legacy-jetson', 'runner.capabilities', {}, 'alice', 'engineering-policy-' + project['id'])]

    async def folder_probe(*_args, **_kwargs):
        return {'ok': True, 'result': {'exit_code': 0, 'entries': []}}
    monkeypatch.setattr(engineering_hosts, 'call', folder_probe)
    second = create(client)
    async def unavailable(*_args, **_kwargs): return {'ok': True, 'result': {'supported_ops': []}}
    monkeypatch.setattr(engineering_hosts, 'call', unavailable)
    denied = client.post(ROOT + '/projects/' + second['id'] + '/policy',
                         json={'expected_revision': 1, 'access_mode': 'isolated', 'confirmation': True})
    assert denied.status_code == 409


def test_off_flag_and_cookie_origin_boundaries(client, monkeypatch):
    assert client.get(ROOT + '/projects').headers['cache-control'] == 'no-store'
    assert client.get(ROOT + '/capabilities').json()['stage'] == 'foundation'
    assert client.post(ROOT + '/projects', headers={'Origin': 'https://attacker.invalid'}, json={}).status_code == 400
    assert client.get(ROOT + '/projects', headers={'X-Odysseus-Internal-Token': 'model-token'}).status_code == 403
    client.cookies.clear()
    assert client.get(ROOT + '/projects').status_code == 401
    assert client.get(ROOT + '/projects').headers['cache-control'] == 'no-store'
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '0')
    assert client.get(ROOT + '/capabilities').json() == {'enabled': False}
    assert client.get(ROOT + '/projects').status_code == 404


def test_no_forged_permissions_or_unconfigured_hosts(client):
    payload = {'name': 'Bad', 'root': '/work/bad', 'host_id': 'unconfigured'}
    assert client.post(ROOT + '/projects', json=payload).status_code == 400
    payload.update(host_id='legacy-jetson', access_mode='trusted_host')
    assert client.post(ROOT + '/projects', json=payload).status_code == 400
    assert client.get(ROOT + '/projects?limit=10000').status_code == 400


def test_host_probe_requires_explicit_confirmation(client, monkeypatch):
    calls = []
    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        return {'ok': True, 'result': {'protocol_version': 1, 'platform': {'os': 'darwin'}}}
    monkeypatch.setattr(engineering_hosts, 'call', call)
    assert client.get(ROOT + '/hosts').status_code == 200
    assert calls == []
    assert client.post(ROOT + '/hosts/legacy-jetson/probe', json={}).status_code == 400
    assert calls == []
    assert client.post(ROOT + '/hosts/legacy-jetson/probe', json={'confirmation': True}).json()['protocol_version'] == 1
    assert len(calls) == 1


def test_check_command_approval_is_owner_scoped_versioned_and_never_executes(client, monkeypatch):
    project = create(client)
    async def forbidden(*args, **kwargs):
        pytest.fail('Approving or reading a check must not contact the host')
    monkeypatch.setattr(engineering_hosts, 'call', forbidden)
    url = ROOT + '/projects/' + project['id'] + '/check-profiles'
    body = {'name': 'Unit tests', 'command': 'python -m pytest', 'confirmation': True,
            'profile_id': None, 'expected_revision': None}
    assert client.post(url, json={**body, 'confirmation': False}).status_code == 403
    assert client.post(url, json={**body, 'host_id': 'arbitrary'}).status_code == 400
    assert client.post(url, json=body, headers={'X-Odysseus-Internal-Token': 'model'}).status_code == 403
    saved = client.post(url, json=body)
    assert saved.status_code == 200, saved.text
    profile = saved.json()
    assert profile['revision'] == 1
    assert client.get(url).json()['profiles'] == [profile]
    update = {**body, 'profile_id': profile['id'], 'expected_revision': 1, 'command': 'python -m pytest tests/unit'}
    assert client.post(url, json=update).json()['revision'] == 2
    assert client.post(url, json=update).status_code == 409
    assert client.get(ROOT + '/projects/' + project['id']).json()['access_mode'] is None
    assert client.get(url + '?limit=0').status_code == 400
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).status_code == 404
    assert client.post(url, json=body).status_code == 404


def test_check_profiles_page_without_total_cap_or_foreign_cursor(client):
    project = create(client)
    url = ROOT + '/projects/' + project['id'] + '/check-profiles'
    expected = set()
    for index in range(103):
        record = client.post(url, json={'name': str(index), 'command': 'true', 'confirmation': True,
                                        'profile_id': None, 'expected_revision': None})
        assert record.status_code == 200, record.text
        expected.add(record.json()['id'])
    seen, cursor = [], ''
    while True:
        response = client.get(url, params={'limit': 50, 'after_id': cursor})
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(record['id'] for record in page['profiles'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert len(seen) == 103 and set(seen) == expected
    other = create(client)
    foreign = ROOT + '/projects/' + other['id'] + '/check-profiles'
    assert client.get(foreign, params={'after_id': seen[0]}).status_code == 404


def test_check_launch_returns_durable_identity_without_waiting_for_runner(client, monkeypatch):
    from src.engineering_operations import OperationManager
    monkeypatch.setattr(OperationManager, 'start', lambda self: None)
    project = create(client)
    async def forbidden(*args, **kwargs):
        pytest.fail('Queueing must not call the host')
    monkeypatch.setattr(engineering_hosts, 'call', forbidden)
    base = ROOT + '/projects/' + project['id']
    profile = client.post(base + '/check-profiles', json={'name': 'Tests', 'command': 'true',
        'confirmation': True, 'profile_id': None, 'expected_revision': None}).json()
    body = {'profile_id': profile['id'], 'kind': 'check', 'idempotency_key': 'stable',
            'expected_project_revision': 2, 'expected_profile_revision': 1, 'confirmation': True}
    assert client.post(base + '/check-runs', json=body).status_code == 403
    assert client.post(base + '/policy', json={'expected_revision': 1,
        'access_mode': 'trusted_host', 'confirmation': True}).status_code == 200
    queued = client.post(base + '/check-runs', json=body)
    assert queued.status_code == 200, queued.text
    assert queued.json()['status'] == 'queued'
    filtered = ROOT + '/operations?kind=check_run&project_id=' + project['id']
    assert client.get(filtered).json()['operations'][0]['id'] == queued.json()['id']
    run_url = base + '/check-runs/' + queued.json()['scope']['run_id']
    assert client.get(run_url).status_code == 404
    assert client.get(run_url + '/output').status_code == 404
    assert client.post(run_url + '/stop', json={'confirmation': False}).status_code == 400
    assert client.post(run_url + '/stop', json={'confirmation': True, 'job_id': 'forged'}).status_code == 400
    assert client.post(base + '/check-runs', json=body).json()['id'] == queued.json()['id']
    assert client.post(base + '/check-runs', json={**body, 'command': 'arbitrary'}).status_code == 400
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.post(base + '/check-runs', json=body).status_code == 404
    assert client.get(filtered).status_code == 404


def test_requirements_are_explicit_owner_criteria_and_readiness_uses_runner_hash(client, monkeypatch):
    project = create(client)
    base = ROOT + '/projects/' + project['id']
    profile = client.post(base + '/check-profiles', json={'name': 'Tests', 'command': 'true',
        'confirmation': True, 'profile_id': None, 'expected_revision': None}).json()
    body = {'title': 'Existing tests pass', 'profile_ids': [profile['id']], 'mandatory': True,
            'requirement_id': None, 'expected_revision': None, 'confirmation': True}
    assert client.post(base + '/requirements', json={**body, 'confirmation': False}).status_code == 403
    assert client.post(base + '/requirements', json=body,
                       headers={'X-Odysseus-Internal-Token': 'model'}).status_code == 403
    saved = client.post(base + '/requirements', json=body)
    assert saved.status_code == 200, saved.text
    record = client.get(base + '/requirements?limit=1').json()
    assert record['next_cursor'] is None
    assert record['requirements'][0]['mandatory'] is True
    updated = {**body, 'requirement_id': saved.json()['id'], 'expected_revision': 1, 'title': 'Reviewed criterion'}
    assert client.post(base + '/requirements', json=updated).json()['revision'] == 2
    assert client.post(base + '/requirements', json=updated).status_code == 409
    async def digest(host, op, args, **kwargs):
        assert op == 'workspace.digest' and args == {'cwd': '/work/example'}
        assert kwargs['owner'] == 'alice'
        return {'ok': True, 'result': {'sha256': 'a' * 64}}
    monkeypatch.setattr(engineering_hosts, 'call', digest)
    readiness = client.get(base + '/check-readiness?workspace_hash=forged')
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()['workspace_hash'] == 'a' * 64
    assert readiness.json()['ready'] is False
    assert readiness.json()['requirements'][0]['checks'][0]['status'] == 'missing_or_stale'
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(base + '/requirements').status_code == 404
    assert client.post(base + '/requirements', json=body).status_code == 404
    assert client.get(base + '/check-readiness').status_code == 404


def test_model_probe_is_interactive_owner_only_and_not_arbitrary_proxy(client, monkeypatch):
    from src import engineering_probe
    calls = []
    from src.engineering_operations import OperationManager
    monkeypatch.setattr(OperationManager, 'start', lambda self: None)
    monkeypatch.setattr(engineering_probe, 'describe', lambda owner, endpoint_id, model:
        {'owner': owner, 'supported': True, 'scope': {'model': model, 'config_digest': 'a' * 64}})
    async def probe(owner, **kwargs):
        calls.append((owner, kwargs))
        return {'status': 'partial'}
    monkeypatch.setattr(engineering_probe, 'probe', probe)
    assert client.get(ROOT + '/model-probe?endpoint_id=local&model=exact').json()['owner'] == 'alice'
    body = {'endpoint_id': 'local', 'model': 'exact', 'confirmation': True, 'expected_config_digest': 'a' * 64}
    assert client.post(ROOT + '/model-probe', json={**body, 'url': 'http://forged'}).status_code == 400
    assert client.post(ROOT + '/model-probe', json=body, headers={'X-Odysseus-Internal-Token': 'model'}).status_code == 403
    assert calls == []
    assert client.post(ROOT + '/model-probe', json={**body, 'expected_config_digest': 'b'*64}).status_code == 409
    operation = client.post(ROOT + '/model-probe', json=body).json()
    assert operation['status'] == 'queued'
    assert operation['scope']['endpoint_id'] == 'local'
    url = ROOT + '/operations/' + operation['id']
    assert client.get(url).json()['id'] == operation['id']
    assert client.get(ROOT + '/operations').json()['operations'][0]['id'] == operation['id']
    assert client.get(ROOT + '/operations?active_only=true&limit=1').json()['operations'][0]['id'] == operation['id']
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).status_code == 404
    assert client.post(url + '/cancel', json={}).status_code == 404
    assert client.get(ROOT + '/operations').json()['operations'] == []
    client.cookies.set('odysseus_session', 'alice-cookie')
    assert client.post(url + '/cancel', json={'arbitrary': True}).status_code == 400
    assert client.post(url + '/cancel', json={}).json()['status'] == 'cancelled'
    assert client.get(ROOT + '/operations?active_only=true').json()['operations'] == []
    assert calls == []


def test_lsp_project_policy_and_server_owned_authority(client, monkeypatch):
    project = create(client)
    base = ROOT + '/projects/' + project['id']
    calls = []
    async def call(host, op, args, **kwargs):
        calls.append((host, op, args, kwargs))
        return {'ok': True, 'result': {'id': 'lsp-session'}}
    monkeypatch.setattr(engineering_hosts, 'call', call)
    body = {'language': 'python', 'idempotency_key': 'once', 'expected_revision': 1}
    assert client.post(base + '/lsp/start', json=body).status_code == 403
    assert not calls
    assert client.post(base + '/policy', json={'expected_revision': 1, 'access_mode': 'trusted_host', 'confirmation': True}).status_code == 200
    assert client.post(base + '/lsp/start', json=body).status_code == 409
    body['expected_revision'] = 2
    assert client.post(base + '/lsp/start', json={**body, 'execution_authorized': True}).status_code == 400
    assert client.post(base + '/lsp/start', json={**body, 'cwd': '/other'}).status_code == 400
    assert client.post(base + '/lsp/start', json=body).status_code == 200
    assert calls[-1][0:3] == ('legacy-jetson', 'lsp.start', {'language': 'python', 'idempotency_key': 'once', 'cwd': '/work/example', 'execution_authorized': True})
    assert client.post(base + '/policy', json={'expected_revision': 2, 'access_mode': None, 'confirmation': True}).status_code == 200
    assert client.post(base + '/lsp/diagnostics', json={'id': 'lsp-session', 'uri': 'file:///work/example/a.py', 'expected_revision': 3}).status_code == 403
    assert client.post(base + '/lsp/stop', json={'id': 'lsp-session'}).status_code == 200
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.post(base + '/lsp/stop', json={'id': 'lsp-session'}).status_code == 404


def test_reviewed_mcp_requires_current_human_review(client, monkeypatch):
    from src import team_mcp
    monkeypatch.setenv('ODYSSEUS_TEAM_MCP_ENABLED', '1')
    monkeypatch.setattr(team_mcp, 'assert_review_access', lambda owner: None)
    identity = 'mcp__fixture__lookup'
    digest = 'a' * 64
    monkeypatch.setattr(team_mcp, 'review_catalogue', lambda: [{'tool_id': identity, 'schema_digest': digest, 'available': True}])
    url = ROOT + '/mcp/reviews'
    payload = {'tool_id': identity, 'schema_digest': digest, 'effects': ['read_public'], 'roles': ['researcher'], 'expected_revision': 0, 'confirmation': True}
    assert client.post(url, json={**payload, 'schema_digest': 'b' * 64}).status_code == 409
    assert client.post(url, json={**payload, 'confirmation': False}).status_code == 403
    assert client.post(url, headers={'X-Odysseus-Internal-Token': 'model'}, json=payload).status_code == 403
    assert client.post(url, json=payload).json()['revision'] == 1
    assert client.post(url, json=payload).status_code == 409
    assert client.post(url + '/revoke', json={'tool_id': identity, 'expected_revision': 1}).json()['enabled'] is False
    client.cookies.set('odysseus_session', 'bob-cookie')
    assert client.get(url).json()['reviews'] == []
    assert client.post(url + '/revoke', json={'tool_id': identity, 'expected_revision': 2}).status_code == 404
