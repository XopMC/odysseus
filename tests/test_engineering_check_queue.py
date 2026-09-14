"""Queue-to-real-runner integration; commands confined to the temporary fixture."""
import asyncio
import base64

import pytest

from test_engineering_check_runner import ctx
from src import engineering_hosts
from src.engineering_operations import OperationManager
from src.team_store import NotFound
from types import SimpleNamespace


def enqueue(manager, project, profile, key):
    return manager.store.create_check('owner', {
        'project_id': project, 'profile_id': profile, 'kind': 'check',
        'idempotency_key': key, 'expected_project_revision': 2,
        'expected_profile_revision': 1, 'confirmation': True})


@pytest.mark.asyncio
async def test_saved_queue_to_real_runner_and_exactly_once_result(ctx, monkeypatch):
    service, _, project, profile, _, counter, calls, transport = ctx
    monkeypatch.setattr(engineering_hosts, 'call', transport)
    first = OperationManager(service.team)
    operation = enqueue(first, project, profile, 'queue-real')
    assert not counter.exists()
    # Queued work survives replacing the application manager before dispatch.
    manager = OperationManager(service.team)
    manager.start()
    try:
        async with asyncio.timeout(8):
            while True:
                result = manager.store.get('owner', operation['id'])
                if result['status'] not in {'queued', 'running'}:
                    break
                await asyncio.sleep(.025)
        assert result['status'] == 'completed', result
        assert result['result']['status'] == 'passed'
        assert result['result']['run_id'] == operation['scope']['run_id']
        assert counter.read_text() == 'x'
        assert (await service.readiness('owner', project))['ready']
        assert enqueue(manager, project, profile, 'queue-real')['id'] == operation['id']
        assert len([c for c in calls if c[0] == 'command.start']) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_app_stop_after_dispatch_observes_without_repeating_command(ctx, monkeypatch):
    service, _, project, profile, _, counter, calls, transport = ctx
    entered = asyncio.Event()
    async def delayed_poll(host, op, args, **kwargs):
        if op == 'terminal.poll':
            entered.set()
            await asyncio.Event().wait()
        return await transport(host, op, args, **kwargs)
    monkeypatch.setattr(engineering_hosts, 'call', delayed_poll)
    manager = OperationManager(service.team)
    operation = enqueue(manager, project, profile, 'interrupted-app')
    manager.start()
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        await manager.close()
    assert manager.store.get('owner', operation['id'])['status'] == 'interrupted'
    async with asyncio.timeout(5):
        while True:
            result = await service.observe('owner', project, operation['scope']['run_id'])
            if result['status'] != 'running':
                break
            await asyncio.sleep(.025)
    assert result['status'] == 'passed'
    assert counter.read_text() == 'x'
    assert len([c for c in calls if c[0] == 'command.start']) == 1
    assert manager.store.claim() is None


@pytest.mark.asyncio
async def test_output_pages_and_explicit_process_stop_are_owner_bound(ctx):
    service, _, project, _, _, _, calls, _ = ctx
    profile = service.checks.approve_profile('owner', project, name='Long check',
        command="printf 'abcdef'; sleep 30", confirmation=True)
    result = await service.start('owner', project, profile['id'], 'output-stop')
    try:
        with pytest.raises(NotFound):
            await service.output('attacker', project, result['run_id'])
        with pytest.raises(PermissionError):
            await service.stop('owner', project, result['run_id'], confirmation=False)
        with pytest.raises(ValueError):
            await service.output('owner', project, result['run_id'], limit=60001)
        async with asyncio.timeout(5):
            while True:
                first = await service.output('owner', project, result['run_id'], limit=3)
                if first['next_offset'] == 3:
                    break
                await asyncio.sleep(.025)
        second = await service.output('owner', project, result['run_id'], offset=3, limit=3)
        assert base64.b64decode(first['output_base64']) == b'abc'
        assert base64.b64decode(second['output_base64']) == b'def'
        assert second['next_offset'] == 6
        stop = await service.stop('owner', project, result['run_id'], confirmation=True)
        assert stop['status'] == 'stop_requested'
        async with asyncio.timeout(5):
            while True:
                final = await service.observe('owner', project, result['run_id'])
                if final['status'] != 'running':
                    break
                await asyncio.sleep(.025)
        assert final['status'] == 'cancelled'
        assert final['run']['evidence']['terminal_status'] == 'cancelled'
        assert (await service.stop('owner', project, result['run_id'], confirmation=True))['status'] == 'cancelled'
        assert len([c for c in calls if c[0] == 'terminal.stop']) == 1
        assert len([c for c in calls if c[0] == 'command.start']) == 1
    finally:
        # Fixture closes owned subprocesses even when assertions fail.
        pass


@pytest.mark.asyncio
async def test_public_readiness_uses_real_check_then_detects_source_change(ctx, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.engineering_routes import setup_engineering_routes
    from src import host_execution, team_runtime
    from test_engineering_check_runner import done
    service, _, project, profile, root, _, calls, transport = ctx
    monkeypatch.setenv('ODYSSEUS_TEAM_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '1')
    monkeypatch.setattr(host_execution, 'enabled_for', lambda owner: owner == 'owner')
    monkeypatch.setattr(engineering_hosts, 'call', transport)
    monkeypatch.setattr(team_runtime, 'get_runtime', lambda: SimpleNamespace(store=service.team))
    baseline = await done(service, project, await service.start('owner', project, profile, 'public-baseline', kind='baseline'))
    final = await done(service, project, await service.start('owner', project, profile, 'public-readiness'))
    assert final['status'] == 'passed'
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(get_username_for_token=lambda token: 'owner' if token == 'test-cookie' else None)
    app.include_router(setup_engineering_routes())
    with TestClient(app, cookies={'odysseus_session': 'test-cookie'}) as client:
        url = '/api/team/engineering/projects/' + project + '/check-readiness'
        first = client.get(url)
        assert first.status_code == 200, first.text
        assert first.json()['ready'] is True
        assert first.json()['requirements'][0]['checks'][0]['run_id'] == final['run_id']
        comparison = client.get('/api/team/engineering/projects/' + project + '/check-comparison',
                                params={'baseline_run_id': baseline['run_id'], 'check_run_id': final['run_id']})
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()['classification'] == 'remained_passing'
        assert comparison.json()['individual_failures_compared'] is False
        history_url = '/api/team/engineering/projects/' + project + '/check-runs'
        history = client.get(history_url, params={'limit': 1}).json()
        assert history['runs'][0]['id'] == final['run_id']
        older = client.get(history_url, params={'limit': 1, 'after_id': history['next_cursor']}).json()
        assert older['runs'][0]['id'] == baseline['run_id']
        assert older['next_cursor'] is None
        (root / 'code.py').write_text('value = 3\n')
        second = client.get(url)
        assert second.status_code == 200, second.text
        assert second.json()['ready'] is False
        assert second.json()['snapshot_id'] != first.json()['snapshot_id']
        assert second.json()['workspace_hash'] != first.json()['workspace_hash']
    assert len([call for call in calls if call[0] == 'command.start']) == 2
