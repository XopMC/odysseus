import asyncio
import copy
import json

import pytest

from src.context_policy_store import ContextPolicyStore
from src.team_runtime import TeamRuntime
from src.team_store import Conflict, TeamStore


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_ENGINEERING_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_CONTEXT_POLICY_ENABLED', '1')
    store = TeamStore(tmp_path / 'teams.db')
    selection = {'endpoint_id': 'local', 'model': 'fixture'}
    task = store.create_task('owner', 'Work', metadata={
        'goal': 'Inspect and verify code', 'project_path': '/project', 'leader': selection,
        'participants': [selection], 'config': {'trusted_host': True, 'web': False,
                                               'external': False, 'reviewer': False}})
    worker = store.add_worker('owner', task['id'], 'Worker', profile={**selection,
        'role': 'executor', 'kind': 'worker', 'cwd': '/project', 'objective': 'Inspect code',
        'acceptance': 'Verified output'})
    worker = store.claim_worker('owner', task['id'], worker_id=worker['id'])
    calls, responses = [], []
    async def complete(route, messages, tools, **kwargs):
        calls.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools), **kwargs})
        response = responses.pop(0) if responses else {'role': 'assistant', 'content': 'Verified summary.'}
        if callable(response):
            response = await response()
        return {'message': response, 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}
    async def host(*args, **kwargs):
        return {'ok': True, 'result': {'output': 'Verified source file', 'exit_code': 0}}
    runtime = TeamRuntime(store, complete=complete, host=host)
    monkeypatch.setattr('src.team_config.resolve', lambda *args: {
        **selection, 'url': 'http://fixture/v1/chat/completions', 'local': True, 'resource_group': 'fixture'})
    monkeypatch.setattr('src.model_context.budget_context_for_model', lambda *args, **kwargs: 4096)
    monkeypatch.setattr('src.team_tools.schemas', lambda *args, **kwargs: [])
    monkeypatch.setattr('src.team_collaboration.schemas', lambda *args, **kwargs: [])
    policies = ContextPolicyStore(store)
    return runtime, store, task['id'], worker, policies, calls, responses


def save(ctx, overrides, **scope):
    policies = ctx[4]
    current = policies.get('owner', **scope)
    return policies.save('owner', overrides=overrides, expected_revisions=current['revisions'], **scope)


def compact_profile(ctx):
    save(ctx, {'output_reserve': 512, 'summary_tokens': 128, 'safety_tokens': 0,
               'safety_percent': 0, 'trigger_percent': 60, 'target_percent': 30,
               'recent_tokens': 0, 'recent_groups': 0})


async def request(ctx, messages, tools=None):
    runtime, _, task, worker, *_ = ctx
    return await runtime.model_call('owner', task, worker, worker['lease_token'], messages, tools or [])


@pytest.mark.asyncio
async def test_no_profile_and_either_disabled_flag_preserve_legacy(ctx, monkeypatch):
    monkeypatch.setattr('src.model_context.budget_context_for_model', lambda *args, **kwargs: 0)
    await request(ctx, [{'role': 'user', 'content': 'legacy'}])
    compact_profile(ctx)
    for flag in ('ODYSSEUS_ENGINEERING_ENABLED', 'ODYSSEUS_CONTEXT_POLICY_ENABLED'):
        monkeypatch.setenv(flag, '0')
        await request(ctx, [{'role': 'user', 'content': 'x' * 30000}])
        monkeypatch.setenv(flag, '1')
    assert all(call['max_tokens'] == 4096 for call in ctx[5])


@pytest.mark.asyncio
async def test_full_request_schema_noauto_and_unknown_backend_block(ctx, monkeypatch):
    save(ctx, {'auto_compact': False, 'requested_window': 2097152, 'output_reserve': 256,
               'safety_tokens': 0, 'safety_percent': 0})
    with pytest.raises(PermissionError):
        await request(ctx, [{'role': 'user', 'content': 'x' * 16000}])
    with pytest.raises(ValueError):
        await request(ctx, [{'role': 'user', 'content': 'tiny'}],
                      [{'type': 'function', 'function': {'name': 'schema', 'description': 'x' * 16000}}])
    monkeypatch.setattr('src.model_context.budget_context_for_model', lambda *args, **kwargs: 0)
    with pytest.raises(PermissionError, match='not confirmed'):
        await request(ctx, [{'role': 'user', 'content': 'tiny'}])
    assert ctx[5] == []


@pytest.mark.asyncio
async def test_real_compactor_caps_summary_replaces_working_list_and_checkpoints(ctx):
    compact_profile(ctx)
    messages = [{'role': 'user', 'content': 'Keep this original goal'}] + [
        {'role': 'assistant', 'content': f'Evidence {i}: ' + 'x' * 600} for i in range(16)]
    before = copy.deepcopy(messages)
    await request(ctx, messages)
    assert len(ctx[5]) == 2
    assert [call['max_tokens'] for call in ctx[5]] == [128, 512]
    assert len(messages) < len(before)
    assert any(m.get('_agent_working_summary') for m in messages)
    assert ctx[5][-1]['messages'] == messages
    events = ctx[1].events('owner', ctx[2], limit=100)
    event = next(e['payload'] for e in events if e['type'] == 'worker_context_policy')
    assert event['before_estimated_tokens'] > event['after_estimated_tokens']
    assert event['status'] == 'compacted'
    with ctx[1]._tx(write=False) as db:
        checkpoints = [json.loads(row[0]) for row in db.execute('SELECT payload FROM team_checkpoints')]
    assert any(row.get('context_policy_request', {}).get('messages') == before for row in checkpoints)
    await request(ctx, messages)
    assert len(ctx[5]) == 3  # no repeated summary of the original context


@pytest.mark.asyncio
async def test_worker_loop_resolves_changed_worker_override_on_next_round(ctx):
    compact_profile(ctx)
    runtime, store, task, worker, policies, calls, responses = ctx
    async def first_round():
        save(ctx, {'output_reserve': 256}, task_id=task, worker_id=worker['id'])
        return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read1', 'type': 'function',
             'function': {'name': 'read_file', 'arguments': '{"path":"/project/code.py"}'}}]}
    responses.extend([first_round, {'role': 'assistant', 'content': 'Verified the source from the tool result.'}])
    await runtime.run_worker('owner', task, worker)
    assert store.get_worker('owner', task, worker['id'])['status'] == 'done'
    assert [c['max_tokens'] for c in calls] == [512, 256]
    observed = policies.last_completed_request('owner', task_id=task, worker_id=worker['id'])
    assert observed['context_policy']['max_output_tokens'] == 256
    assert observed['context_policy']['source'] == 'estimated'
    assert observed['context_policy']['revisions'] == policies.get('owner', task_id=task, worker_id=worker['id'])['revisions']
    save(ctx, {'output_reserve': 768}, task_id=task, worker_id=worker['id'])
    assert policies.last_completed_request('owner', task_id=task, worker_id=worker['id']) == observed
    assert observed['context_policy']['revisions'] != policies.get('owner', task_id=task, worker_id=worker['id'])['revisions']


@pytest.mark.asyncio
async def test_failed_model_request_does_not_claim_completed_policy(ctx):
    compact_profile(ctx)
    async def fail():
        raise ConnectionError('lost connection after dispatch')
    ctx[6].append(fail)
    with pytest.raises(ConnectionError):
        await request(ctx, [{'role': 'user', 'content': 'test'}])
    assert ctx[4].last_completed_request('owner', task_id=ctx[2], worker_id=ctx[3]['id']) is None


@pytest.mark.asyncio
async def test_invalid_inherited_policy_blocks_instead_of_fallback(ctx):
    save(ctx, {'trigger_percent': 70, 'target_percent': 50})
    save(ctx, {'target_percent': 65}, task_id=ctx[2], worker_id=ctx[3]['id'])
    save(ctx, {'trigger_percent': 60, 'target_percent': 50})
    with pytest.raises(PermissionError, match='invalid'):
        await request(ctx, [{'role': 'user', 'content': 'test'}])
    assert ctx[5] == []


@pytest.mark.asyncio
async def test_policy_change_while_backend_probe_blocks(ctx, monkeypatch):
    compact_profile(ctx)
    def changing_probe(*args, **kwargs):
        save(ctx, {'output_reserve': 256})
        return 4096
    monkeypatch.setattr('src.model_context.budget_context_for_model', changing_probe)
    with pytest.raises(PermissionError, match='changed'):
        await request(ctx, [{'role': 'user', 'content': 'test'}])
    assert ctx[5] == []


@pytest.mark.asyncio
async def test_finalizer_uses_same_real_model_budget(ctx):
    compact_profile(ctx)
    runtime, store, task, worker, _, calls, _ = ctx
    await runtime.finalize('owner', task, worker, worker['lease_token'], {})
    assert len(calls) == 1 and calls[0]['max_tokens'] == 512


@pytest.mark.asyncio
async def test_summary_timeout_fails_closed_without_main_dispatch(ctx, monkeypatch):
    compact_profile(ctx)
    from src.context_policy import ContextPolicy
    # Keep this timeout-path unit test short without changing the production
    # minimum of ten minutes for slow local model summarization.
    monkeypatch.setattr(ContextPolicy, 'effective_summary_timeout_seconds',
                        property(lambda policy: policy.summary_timeout_seconds))
    save(ctx, {'summary_timeout_seconds': 5}, task_id=ctx[2], worker_id=ctx[3]['id'])
    async def slow_summary():
        await asyncio.sleep(20)
        return {'role': 'assistant', 'content': 'late'}
    ctx[6].append(slow_summary)
    messages = [{'role': 'user', 'content': 'Goal'}] + [
        {'role': 'assistant', 'content': 'x' * 610} for _ in range(16)]
    before = copy.deepcopy(messages)
    with pytest.raises(PermissionError, match='compaction'):
        await request(ctx, messages)
    assert len(ctx[5]) == 1 and ctx[5][0]['max_tokens'] == 128
    assert messages == before


@pytest.mark.asyncio
async def test_actual_planner_loop_uses_policy_budget(ctx):
    compact_profile(ctx)
    runtime, _, task, worker, _, calls, responses = ctx
    responses.append({'role': 'assistant', 'content': json.dumps({'tasks': [{
        'name': 'Inspect', 'objective': 'Read code', 'acceptance': 'Evidence from file',
        'participant': 0, 'depends_on': [], 'write_scope': []}]})})
    result = await runtime.execute_planner('owner', task, worker, worker['lease_token'], {})
    assert result['completed'] is True
    assert len(calls) == 1 and calls[0]['max_tokens'] == 512


@pytest.mark.asyncio
async def test_changed_policy_during_summary_keeps_original_and_blocks_dispatch(ctx):
    compact_profile(ctx)
    async def change_policy():
        save(ctx, {'output_reserve': 256})
        return {'role': 'assistant', 'content': 'A factual short summary.'}
    ctx[6].append(change_policy)
    messages = [{'role': 'user', 'content': 'Goal'}] + [
        {'role': 'assistant', 'content': 'x' * 610} for _ in range(16)]
    before = copy.deepcopy(messages)
    with pytest.raises(PermissionError, match='changed'):
        await request(ctx, messages)
    assert messages == before
    assert len(ctx[5]) == 1


@pytest.mark.parametrize('change', ['external_consent', 'cancel', 'endpoint', 'same_policy_revision'])
@pytest.mark.asyncio
async def test_summary_await_rechecks_permissions_route_and_policy_revision(ctx, monkeypatch, change):
    compact_profile(ctx)
    runtime, store, task, worker, _, calls, responses = ctx
    route = {'endpoint_id': 'local', 'model': 'fixture', 'url': 'http://fixture/v1/chat/completions',
             'local': False, 'resource_group': 'external'}
    monkeypatch.setattr('src.team_config.resolve', lambda *args: dict(route))
    meta = store.get_task('owner', task)['metadata']
    store.update_task_metadata('owner', task, {'config': {**meta['config'], 'external': True},
                                             'external_data_scopes': {'local': 'assigned_context'}})
    store.set_task_budget('owner', task, 100000)
    store.approve_endpoint('owner', task, 'local', 100000, 1000000, 1000000)
    async def revoke_after_summary():
        if change == 'external_consent':
            store.update_task_metadata('owner', task, {'external_data_scopes': {}})
        elif change == 'cancel':
            store.set_task_status('owner', task, 'cancelled')
        elif change == 'endpoint':
            route['url'] = 'http://different-endpoint/v1/chat/completions'
        else:
            compact_profile(ctx)  # same values, NEW revision must invalidate request
        return {'role': 'assistant', 'content': 'A short factual summary.'}
    responses.append(revoke_after_summary)
    messages = [{'role': 'user', 'content': 'Goal'}] + [
        {'role': 'assistant', 'content': 'x' * 610} for _ in range(16)]
    with pytest.raises((PermissionError, Conflict)):
        await request(ctx, messages)
    assert len(calls) == 1  # authorized summary only; main request never sent
    with store._tx(write=False) as db:
        assert db.execute('SELECT COUNT(*) FROM team_reservations').fetchone()[0] == 1
