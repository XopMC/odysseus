import asyncio
from unittest.mock import AsyncMock

import pytest

from src import engineering_hosts, team_config, team_workspace
from src.engineering_store import EngineeringStore
from src.team_runtime import TeamRuntime
from src.team_store import TeamStore


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '1')
    store = TeamStore(tmp_path / 'team.db')
    projects = EngineeringStore(store)
    project = projects.create_project('owner', name='Mac project', root='/Users/owner/project', host_id='mac')
    legacy = AsyncMock(return_value={'ok': True, 'result': {'legacy': True}})
    runtime = TeamRuntime(store, host=legacy)
    transport = AsyncMock(return_value={'ok': True, 'result': {'id': 'job', 'exit_code': 0}})
    monkeypatch.setattr(engineering_hosts, 'call', transport)
    return store, projects, project, runtime, legacy, transport


@pytest.mark.asyncio
async def test_project_host_dispatch_revocation_and_readback(context):
    store, projects, project, runtime, legacy, transport = context
    task = store.create_task('owner', 'Task', metadata={'engineering_project_id': project['id']})
    worker = store.add_worker('owner', task['id'], 'Worker')
    with pytest.raises(PermissionError):
        await runtime.host_call('owner', worker['id'], 'command.start', {'command': 'pwd'})
    transport.assert_not_awaited()
    projects.set_policy('owner', project['id'], 1, 'trusted_host', confirmation=True)
    await runtime.host_call('owner', worker['id'], 'command.start', {'command': 'pwd'})
    assert transport.call_args.args[0] == 'mac'
    legacy.assert_not_awaited()
    projects.set_policy('owner', project['id'], 2, None, confirmation=True)
    with pytest.raises(PermissionError):
        await runtime.host_call('owner', worker['id'], 'command.start', {'command': 'pwd'})
    await runtime.host_call('owner', worker['id'], 'terminal.poll', {'id': 'job'})
    await runtime.host_call('owner', worker['id'], 'terminal.stop', {'id': 'job'})
    with pytest.raises(PermissionError):
        await runtime.host_call('other', worker['id'], 'terminal.poll', {'id': 'job'})


@pytest.mark.asyncio
async def test_isolated_project_never_falls_back_to_trusted_team_execution(context, monkeypatch):
    store, projects, project, runtime, legacy, transport = context
    monkeypatch.setenv('ODYSSEUS_ISOLATED_RUNNER_ENABLED', '1')
    task = store.create_task('owner', 'Task', metadata={'engineering_project_id': project['id']})
    worker = store.add_worker('owner', task['id'], 'Worker')
    projects.set_policy('owner', project['id'], 1, 'isolated', confirmation=True)
    with pytest.raises(PermissionError, match='approved verification check'):
        await runtime.host_call('owner', worker['id'], 'command.start', {'command': 'pwd'})
    transport.assert_not_awaited()
    legacy.assert_not_awaited()
    await runtime.host_call('owner', worker['id'], 'terminal.poll', {'id': 'existing-job'})
    assert transport.call_args.args[1] == 'terminal.poll'


@pytest.mark.asyncio
async def test_legacy_host_route_unchanged(context):
    store, _, _, runtime, legacy, transport = context
    task = store.create_task('owner', 'Legacy')
    assert (await runtime.host_call('owner', task['id'], 'resource.snapshot', {}))['legacy']
    transport.assert_not_awaited()
    legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_task_pins_project_root_host_and_revision(context, monkeypatch):
    store, projects, project, runtime, _, _ = context
    route = {'endpoint_id': 'jetson', 'model': 'coder', 'local': True, 'resource_group': 'jetson'}
    monkeypatch.setattr(team_config, 'resolve', lambda *args: route)
    monkeypatch.setattr(team_workspace, 'ensure_team', AsyncMock(return_value={}))
    monkeypatch.setattr(runtime, 'start', lambda: None)
    body = {'goal': 'Test', 'project_id': project['id'], 'project_revision': 1,
            'project_path': '/wrong', 'leader': {'endpoint_id': 'jetson', 'model': 'coder'},
            'workers': [], 'config': {'auto_dispatch': False}}
    with pytest.raises(PermissionError):
        await runtime.create('owner', 'session', body)
    assert store.list_tasks('owner') == []
    projects.set_policy('owner', project['id'], 1, 'trusted_host', confirmation=True)
    with pytest.raises(ValueError):
        await runtime.create('owner', 'session', body)
    body['project_revision'] = 2
    result = await runtime.create('owner', 'session', body)
    assert result['metadata']['project_path'] == project['root']
    assert result['metadata']['execution_host_id'] == 'mac'
    assert result['metadata']['required_runtime'] == 'engineering-v1'
    with pytest.raises(ValueError):
        await runtime.create('owner', 'other-session', dict(body, execution_host_id='attacker'))


def test_verified_project_memory_is_bounded_and_never_implicit_external_transfer(context, monkeypatch):
    store, projects, project, runtime, _, _ = context
    local = {'endpoint_id': 'local', 'model': 'coder', 'local': True, 'resource_group': 'local'}
    remote = {'endpoint_id': 'remote', 'model': 'coder', 'local': False, 'resource_group': 'remote'}
    projects.save_memory('owner', project['id'], memory_id='', kind='architecture', text='Verified local fact',
                         source='README.md:1', state='verified', expected_revision=0, confirmation=True)
    projects.save_memory('owner', project['id'], memory_id='', kind='hypothesis', text='Do not send this',
                         source='notes', state='proposed', expected_revision=0, confirmation=True)
    task = store.create_task('owner', 'Task', metadata={'engineering_project_id': project['id']})
    worker = store.add_worker('owner', task['id'], 'Worker', profile={'endpoint_id': 'local', 'model': 'coder'})
    monkeypatch.setattr(team_config, 'resolve', lambda owner, endpoint, model: local if endpoint == 'local' else remote)
    assert runtime.verified_project_memory('owner', store.get_task('owner', task['id']), worker) == [
        {'kind': 'architecture', 'text': 'Verified local fact', 'source': 'README.md:1'}]
    worker['profile']['endpoint_id'] = 'remote'
    assert runtime.verified_project_memory('owner', store.get_task('owner', task['id']), worker) == []


@pytest.mark.asyncio
async def test_disabled_feature_cannot_resume_project_host_work(context, monkeypatch):
    store, _, project, runtime, _, transport = context
    task = store.create_task('owner', 'Task', metadata={'engineering_project_id': project['id']})
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '0')
    with pytest.raises(PermissionError):
        await runtime.host_call('owner', task['id'], 'resource.snapshot', {})
    transport.assert_not_awaited()


@pytest.mark.parametrize('enabled', ['0', '1'])
@pytest.mark.parametrize('required', ['engineering-v1', 'engineering-v2', '', None, False])
@pytest.mark.asyncio
async def test_runtime_gate_before_direct_claims_and_effects(context, monkeypatch, enabled, required):
    store, _, _, runtime, legacy, transport = context
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', enabled)
    task = store.create_task('owner', 'Tagged', metadata={'required_runtime': required})
    assert runtime.supports_task(task) == (required == 'engineering-v1' and enabled == '1')
    if runtime.supports_task(task):
        return
    before = store.get_task('owner', task['id'])
    calls = [
        runtime.coordinate('owner', task['id']),
        runtime.accept_result('owner', task['id'], 'missing'),
        runtime.prepare_workers('owner', task['id']),
        runtime.run_worker('owner', task['id'], {}),
        runtime.execute_worker('owner', task['id'], {}, 'token'),
        runtime.model_call('owner', task['id'], {}, 'token', [], []),
        runtime.execute_tool('owner', task['id'], {}, 'bash', {}, 'call', '/tmp'),
        runtime.host_call('owner', task['id'], 'command.start', {'command': 'pwd'}),
    ]
    for call in calls:
        with pytest.raises(PermissionError, match='unavailable execution runtime'):
            await call
    assert store.get_task('owner', task['id']) == before
    legacy.assert_not_awaited()
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_scheduler_skips_new_work_but_claims_legacy(context, monkeypatch):
    store, _, _, runtime, _, _ = context
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '0')
    legacy = store.create_task('owner', 'Legacy')
    tagged = [store.create_task('owner', 'New', metadata=meta) for meta in (
        {'required_runtime': 'engineering-v1'}, {'required_runtime': 'future'},
        {'required_runtime': ''}, {'engineering_project_id': 'unmarked-project'})]
    for task in [legacy, *tagged]:
        store.add_worker('owner', task['id'], 'Worker')
    runtime.coordinate = AsyncMock()
    runtime.prepare_workers = AsyncMock()
    runtime.run_worker = AsyncMock()
    async def one_iteration(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr('src.team_runtime.asyncio.sleep', one_iteration)
    with pytest.raises(asyncio.CancelledError):
        await runtime.pump()
    runtime.coordinate.assert_awaited_once_with('owner', legacy['id'])
    assert store.list_workers('owner', legacy['id'])[0]['status'] == 'running'
    for task in tagged:
        assert store.list_workers('owner', task['id'])[0]['status'] == 'pending'
    await runtime.close()
