"""Explicit test grant: run fixed shell checks only in disposable temp trees."""
import asyncio
import importlib.util
from pathlib import Path
import shlex

import pytest

from src.engineering_check_runner import EngineeringCheckRunner
from src.engineering_store import EngineeringStore
from src.team_store import Conflict, NotFound, TeamStore


@pytest.fixture
def ctx(tmp_path):
    spec = importlib.util.spec_from_file_location('check_runner_host', Path(__file__).resolve().parents[1] / 'scripts/host_runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = module.Runner(tmp_path / 'runner-state')
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'code.py').write_text('value = 1\n')
    team = TeamStore(tmp_path / 'teams.db')
    projects = EngineeringStore(team)
    project = projects.create_project('owner', name='Disposable check test', root=str(root), host_id='test-host')
    projects.set_policy('owner', project['id'], 1, 'trusted_host', confirmation=True)
    calls = []
    async def transport(host, op, args, *, owner, scope):
        assert host == 'test-host'
        calls.append((op, dict(args)))
        return await asyncio.to_thread(runner.handle, {'op': op, 'args': args, 'owner': owner, 'scope': scope})
    service = EngineeringCheckRunner(team, host_call=transport)
    counter = tmp_path / 'outside-project-counter'
    profile = service.checks.approve_profile('owner', project['id'], name='Fixed test',
              command='printf x >> ' + shlex.quote(str(counter)), confirmation=True)
    service.checks.set_requirement('owner', project['id'], title='Fixed check passes', profile_ids=[profile['id']])
    yield service, runner, project['id'], profile['id'], root, counter, calls, transport
    runner.close()


async def done(service, project, run):
    for _ in range(200):
        if run['status'] not in {'running', 'dispatch_unknown'}:
            return run
        await asyncio.sleep(.025)
        run = await service.poll('owner', project, run['run_id'])
    pytest.fail('Real runner check did not complete')


@pytest.mark.asyncio
async def test_real_check_approved_command_actual_hash_and_durable_restart(ctx):
    service, runner, project, profile, root, counter, calls, transport = ctx
    run = await service.start('owner', project, profile, 'stable-key')
    # New application adapter, same durable DB and host process.
    service = EngineeringCheckRunner(service.team, host_call=transport)
    result = await done(service, project, run)
    assert result['status'] == 'passed'
    assert counter.read_text() == 'x'
    assert result['run']['evidence']['toolchain'].startswith('/bin/bash sha256:')
    assert result['run']['evidence']['exit_code'] == 0
    command = next(args for op, args in calls if op == 'command.start')
    assert command['cwd'] != str(root)
    assert (Path(command['cwd']) / 'code.py').read_text() == 'value = 1\n'
    assert (await service.readiness('owner', project))['ready']
    assert (await service.start('owner', project, profile, 'stable-key'))['run_id'] == run['run_id']
    assert counter.read_text() == 'x'
    (root / 'code.py').write_text('value = 2\n')
    assert not (await service.readiness('owner', project))['ready']
    assert len([c for c in calls if c[0] == 'command.start']) == 1


@pytest.mark.asyncio
async def test_lost_ack_reconnect_finds_same_host_job_without_reexecution(ctx):
    service, runner, project, profile, _, counter, calls, transport = ctx
    async def lost_ack(host, op, args, **kwargs):
        response = await transport(host, op, args, **kwargs)
        if op == 'command.start':
            raise TimeoutError('simulated lost SSH acknowledgement')
        return response
    service.host_call = lost_ack
    run = await service.start('owner', project, profile, 'lost-ack')
    assert run['status'] == 'dispatch_unknown'
    restarted = EngineeringCheckRunner(service.team, host_call=transport)
    final = await done(restarted, project, await restarted.poll('owner', project, run['run_id']))
    assert final['status'] == 'passed'
    assert counter.read_text() == 'x'
    assert len([c for c in calls if c[0] == 'command.start']) == 1


@pytest.mark.asyncio
async def test_lost_copy_ack_recovers_same_copy_without_observation_dispatch(ctx):
    service, runner, project, profile, root, counter, calls, transport = ctx
    async def lost_copy_ack(host, op, args, **kwargs):
        response = await transport(host, op, args, **kwargs)
        if op == 'workspace.verification-copy':
            raise TimeoutError('copy acknowledgement lost')
        return response
    service.host_call = lost_copy_ack
    run = await service.start('owner', project, profile, 'copy-lost-ack')
    assert run['status'] == 'dispatch_unknown'
    assert not counter.exists()
    count = len(calls)
    await service.observe('owner', project, run['run_id'])
    assert not any(op in {'workspace.verification-copy', 'command.start'} for op, _ in calls[count:])
    service = EngineeringCheckRunner(service.team, host_call=transport)
    completed = await done(service, project, await service.start('owner', project, profile, 'copy-lost-ack'))
    assert completed['status'] == 'passed'
    assert counter.read_text() == 'x'
    assert len([c for c in calls if c[0] == 'command.start']) == 1
    copies = [record for record in runner.data['worktrees'].values() if record.get('kind') == 'verification-copy']
    assert len(copies) == 1
    assert copies[0]['path'] != str(root)


@pytest.mark.asyncio
async def test_revocation_during_copy_prevents_command_dispatch(ctx):
    service, _, project, profile, _, counter, calls, transport = ctx
    async def revoke_after_copy(host, op, args, **kwargs):
        response = await transport(host, op, args, **kwargs)
        if op == 'workspace.verification-copy':
            service.checks.projects.set_policy('owner', project, 2, None, confirmation=True)
        return response
    service.host_call = revoke_after_copy
    with pytest.raises((PermissionError, Conflict)):
        await service.start('owner', project, profile, 'copy-revoked')
    assert not counter.exists()
    assert not any(op == 'command.start' for op, _ in calls)


@pytest.mark.asyncio
async def test_hash_changed_before_spawn_never_executes(ctx):
    service, _, project, profile, root, counter, _, transport = ctx
    async def race(host, op, args, **kwargs):
        if op == 'command.start':
            (root / 'code.py').write_text('changed before spawn\n')
        return await transport(host, op, args, **kwargs)
    service.host_call = race
    run = await service.start('owner', project, profile, 'race')
    assert run['status'] == 'dispatch_unknown'
    assert not counter.exists()
    assert (await service.poll('owner', project, run['run_id']))['status'] == 'stale'


@pytest.mark.asyncio
async def test_revocation_ownership_and_forged_job_evidence_fail_closed(ctx):
    service, _, project, profile, _, counter, calls, transport = ctx
    service.checks.projects.set_policy('owner', project, 2, None, confirmation=True)
    with pytest.raises(PermissionError):
        await service.start('owner', project, profile, 'revoked')
    with pytest.raises(NotFound):
        await service.start('attacker', project, profile, 'wrong-owner')
    assert not counter.exists()
    service.checks.projects.set_policy('owner', project, 3, 'trusted_host', confirmation=True)
    async def forged(host, op, args, **kwargs):
        response = await transport(host, op, args, **kwargs)
        if op == 'terminal.poll' and response.get('ok'):
            response['result']['check_evidence'] = {'run_id': 'other-run'}
        return response
    service.host_call = forged
    with pytest.raises(Conflict):
        await service.start('owner', project, profile, 'forged')


@pytest.mark.asyncio
async def test_command_modifying_code_is_stale_and_failure_is_not_pass(ctx):
    service, _, project, _, root, _, _, _ = ctx
    profile = service.checks.approve_profile('owner', project, name='Mutates source',
                                           command='printf changed > code.py', confirmation=True)
    changed = await done(service, project, await service.start('owner', project, profile['id'], 'changes'))
    assert changed['status'] == 'stale'
    assert (root / 'code.py').read_text() == 'value = 1\n'
    profile = service.checks.approve_profile('owner', project, name='Fails', command='exit 7', confirmation=True)
    failed = await done(service, project, await service.start('owner', project, profile['id'], 'failure'))
    assert failed['status'] == 'failed'
    assert failed['run']['evidence']['exit_code'] == 7


@pytest.mark.asyncio
async def test_digest_refuses_links_and_binds_hidden_files(ctx):
    service, runner, project, _, root, counter, _, _ = ctx
    def digest():
        return runner.handle({'op': 'workspace.digest', 'args': {'cwd': str(root)},
                              'owner': 'owner', 'scope': 'engineering-project-' + project})
    first = digest()['result']['sha256']
    (root / '.hidden').write_text('tracked by digest')
    assert digest()['result']['sha256'] != first
    (root / 'link').symlink_to(counter)
    assert not digest()['ok']


@pytest.mark.asyncio
async def test_lost_ack_after_revocation_is_read_only_recovery(ctx):
    service, _, project, profile, _, counter, calls, transport = ctx
    async def lost_ack(host, op, args, **kwargs):
        result = await transport(host, op, args, **kwargs)
        if op == 'command.start':
            raise TimeoutError('lost reply')
        return result
    service.host_call = lost_ack
    run = await service.start('owner', project, profile, 'revoke-after-send')
    service.checks.projects.set_policy('owner', project, 2, None, confirmation=True)
    service.host_call = transport
    final = await done(service, project, await service.poll('owner', project, run['run_id']))
    assert final['status'] == 'stale'
    assert counter.read_text() == 'x'
    assert len([c for c in calls if c[0] == 'command.start']) == 1


@pytest.mark.asyncio
async def test_timeout_is_persisted_and_never_passes_even_with_zero_exit(ctx):
    service, _, project, profile, _, _, _, transport = ctx
    async def timeout_observation(host, op, args, **kwargs):
        response = await transport(host, op, args, **kwargs)
        # Exercise the authenticated runner-result boundary; the subprocess
        # itself is real, but inject the timeout/exit race deterministically.
        if op == 'terminal.poll' and response.get('ok'):
            job = response['result']
            if job.get('status') == 'exited':
                job['status'] = 'timed_out'
                job['exit_code'] = 0
        return response
    service.host_call = timeout_observation
    result = await done(service, project, await service.start('owner', project, profile, 'timeout'))
    assert result['status'] == 'timed_out'
    assert result['run']['evidence']['terminal_status'] == 'timed_out'
    assert not (await service.readiness('owner', project))['ready']
    restarted = EngineeringCheckRunner(service.team, host_call=transport)
    assert (await restarted.poll('owner', project, result['run_id']))['status'] == 'timed_out'


@pytest.mark.parametrize('digest_number', [1, 2])
@pytest.mark.parametrize('change', ['cancel', 'project', 'profile'])
@pytest.mark.asyncio
async def test_queued_check_fences_after_each_awaited_digest(ctx, digest_number, change):
    service, _, project, profile, _, counter, calls, transport = ctx
    active, seen = True, 0
    async def delayed(host, op, args, **kwargs):
        nonlocal active, seen
        response = await transport(host, op, args, **kwargs)
        if op == 'workspace.digest':
            seen += 1
            if seen == digest_number:
                if change == 'cancel':
                    active = False
                elif change == 'project':
                    service.checks.projects.set_policy('owner', project, 2, None, confirmation=True)
                else:
                    service.checks.approve_profile('owner', project, name='Changed', command='true',
                        confirmation=True, profile_id=profile, expected_revision=1)
        return response
    service.host_call = delayed
    with pytest.raises((PermissionError, Conflict)):
        await service.start('owner', project, profile, 'queued', check_active=lambda: active,
                            expected_project_revision=2, expected_profile_revision=1)
    assert not counter.exists()
    assert not any(op == 'command.start' for op, _ in calls)


@pytest.mark.asyncio
async def test_stale_queue_revisions_fail_before_any_rpc(ctx):
    service, _, project, profile, _, _, calls, _ = ctx
    for kwargs in ({'expected_project_revision': 1}, {'expected_profile_revision': 2},
                   {'check_active': lambda: False}):
        with pytest.raises((Conflict, PermissionError)):
            await service.start('owner', project, profile, 'stale-queue', **kwargs)
    assert calls == []


@pytest.mark.asyncio
async def test_observe_missing_job_never_dispatches_and_retry_revision_is_fenced(ctx):
    service, _, project, profile, _, counter, calls, transport = ctx
    async def connection_failed(host, op, args, **kwargs):
        if op == 'command.start':
            raise TimeoutError('No acknowledgement; job may or may not have started')
        return await transport(host, op, args, **kwargs)
    service.host_call = connection_failed
    run = await service.start('owner', project, profile, 'unknown')
    assert run['status'] == 'dispatch_unknown'
    service.host_call = transport
    calls.clear()
    observed = await service.observe('owner', project, run['run_id'])
    assert observed['status'] == 'dispatch_unknown'
    assert [op for op, _ in calls] == ['terminal.list']
    assert not counter.exists()
    service.checks.approve_profile('owner', project, name='Reapproved', command='true',
                                  confirmation=True, profile_id=profile, expected_revision=1)
    calls.clear()
    with pytest.raises(Conflict):
        await service.start('owner', project, profile, 'unknown', expected_profile_revision=1)
    assert calls == []


@pytest.mark.asyncio
async def test_observe_success_reconciles_lost_ack_without_command_start(ctx):
    service, _, project, profile, _, counter, calls, transport = ctx
    async def lost_ack(host, op, args, **kwargs):
        result = await transport(host, op, args, **kwargs)
        if op == 'command.start':
            raise TimeoutError('lost acknowledgement')
        return result
    service.host_call = lost_ack
    run = await service.start('owner', project, profile, 'observe-only')
    service.host_call = transport
    calls.clear()
    for _ in range(100):
        result = await service.observe('owner', project, run['run_id'])
        if result['status'] != 'running':
            break
        await asyncio.sleep(.025)
    assert result['status'] == 'passed'
    assert counter.read_text() == 'x'
    assert not any(op == 'command.start' for op, _ in calls)
