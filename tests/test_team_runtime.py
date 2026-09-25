"""Team runtime integration: fake model/host, real durable SQLite and policies."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.team_runtime import TeamRuntime, _exact_read_only_acceptance, _exact_acceptance_target
from src.team_store import TeamStore, NotFound, Conflict


def answer(text='Verified the requested change', calls=None):
    result = {'role': 'assistant', 'content': text}
    if calls:
        result['tool_calls'] = calls
    return result


def tool(name='read_file', args=None, identifier='call-1'):
    return {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(args or {'path': '/project/example.py'})}}


PYTHON_SCHEMA = {'type': 'function', 'function': {
    'name': 'python', 'description': 'Run Python code',
    'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}},
                   'required': ['code']}}}


class TeamRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.
        self.store = TeamStore(Path(self.directory.name) / 'teams.db', clock=lambda: self.now)
        self.selection = {'endpoint_id': 'local', 'model': 'fixture'}
        self.config = {'trusted_host': True, 'web': False, 'external': False, 'reviewer': False}
        self.task = self.store.create_task('owner', 'Verify change', budget_microusd=100_000,
            metadata={'session_id': 'session-a', 'goal': 'Fix and verify the example',
                      'project_path': '/project', 'config': self.config,
                      'leader': self.selection, 'participants': []})
        self.messages = []
        self.tool_schemas = []
        self.responses = []
        self.host_calls = []
        self.host_result = {'output': 'def correct(): return True', 'exit_code': 0}
        self.route = {'endpoint_id': 'local', 'model': 'fixture', 'local': True,
                      'resource_group': 'jetson', 'url': 'http://never-called.invalid/v1/chat/completions'}
        self.runtime = TeamRuntime(self.store, complete=self.complete, host=self.host)
        self.resolve_patch = patch('src.team_config.resolve', side_effect=lambda *_: self.route)
        self.schema_patch = patch('src.team_tools.schemas', return_value=[])
        self.resolve_patch.start()
        self.schema_patch.start()
        self.addCleanup(self.resolve_patch.stop)
        self.addCleanup(self.schema_patch.stop)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def complete(self, route, messages, tools, *, max_tokens, on_delta):
        self.messages.append(copy.deepcopy(messages))
        self.tool_schemas.append(copy.deepcopy(tools))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        await on_delta(value.get('content', ''))
        return {'message': value, 'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
                'duration': 1., 'ttft': .1, 'generation_tps': 5.}

    async def host(self, op, args, *, owner, scope):
        self.host_calls.append((op, copy.deepcopy(args), owner, scope))
        return {'ok': True, 'result': copy.deepcopy(self.host_result)}

    def worker(self, *, role='executor', kind='worker', **profile):
        return self.store.add_worker('owner', self.task['id'], 'Work', profile={
            **self.selection, 'role': role, 'kind': kind, 'cwd': '/project',
            'objective': 'Inspect example', 'acceptance': 'Show evidence', **profile})

    async def execute(self, worker):
        claim = self.store.claim_worker('owner', self.task['id'], worker_id=worker['id'])
        await self.runtime.run_worker('owner', self.task['id'], claim)
        return self.store.get_worker('owner', self.task['id'], worker['id'])

    async def test_native_tool_round_is_durable_done_but_not_accepted(self):
        worker = self.worker()
        self.responses = [answer('', [tool()]), answer()]
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['successful_tools'], 1)
        self.assertEqual(self.host_calls[0][0], 'file.call')
        self.assertEqual(self.host_calls[0][2:], ('owner', worker['id']))
        self.assertEqual(self.messages[-1][-1]['role'], 'tool')
        checkpoint = self.store.load_checkpoint('owner', self.task['id'], worker['id'])
        self.assertTrue(any(m['role'] == 'tool' for m in checkpoint['payload']['messages']))
        self.assertEqual(self.store.list_tool_intents('owner', self.task['id'])[0]['status'], 'done')
        self.assertEqual(self.store.get_task('owner', self.task['id'])['status'], 'running')

    async def test_tool_error_category_and_next_action_are_durable(self):
        worker = self.worker()
        self.host_result = {'error': 'Fixture missing', 'code': 'not_found', 'exit_code': 1}
        self.responses = [answer('', [tool()]), answer()]
        await self.execute(worker)
        record = self.store.list_tool_intents('owner', self.task['id'])[0]
        self.assertEqual(record['result']['error_category'], 'not_found')
        self.assertIn('next_action', record['result'])
        self.assertFalse(record['result']['retryable'])

    async def test_basic_pool_becomes_planner_assignments_without_implicit_host_access(self):
        pool = [{'endpoint_id': f'local-{i}', 'model': 'same-name', 'role': 'executor'} for i in range(6)]
        with patch.object(self.runtime, 'start'):
            created = await self.runtime.create('owner', 'basic-chat', {
                'goal': 'Implement six independent parts and verify them',
                'project_path': '/project', 'leader': self.selection, 'workers': pool,
                'config': {'auto_dispatch': True, 'auto_continue': True,
                           'trusted_host': False, 'external': False, 'reviewer': False}})
        team_id = created['team_id']
        initial = self.store.list_workers('owner', team_id)
        self.assertEqual(len(initial), 1)
        self.assertEqual(initial[0]['profile']['kind'], 'planner')
        self.assertFalse(self.host_calls)
        claim = self.store.claim_worker('owner', team_id, worker_id=initial[0]['id'])
        plan = {'tasks': [{'participant': i, 'objective': f'Implement part {i}',
                          'acceptance': f'Test part {i}', 'depends_on': []} for i in range(6)]}
        self.store.finish_worker('owner', team_id, initial[0]['id'], claim['lease_token'], {'plan': plan})
        await self.runtime.coordinate('owner', team_id)
        assigned = [w for w in self.store.list_workers('owner', team_id) if w['profile']['kind'] == 'worker']
        self.assertEqual(len(assigned), 6)
        self.assertEqual({w['profile']['endpoint_id'] for w in assigned}, {p['endpoint_id'] for p in pool})
        self.assertEqual({w['profile']['objective'] for w in assigned}, {f'Implement part {i}' for i in range(6)})
        self.assertFalse(self.host_calls)
        await self.runtime.coordinate('owner', team_id)
        self.assertEqual(len(self.store.list_workers('owner', team_id)), 7, 'reconciliation must not duplicate assignments')

    async def test_saved_plan_rejects_noninteger_participant_before_any_assignment(self):
        for invalid in (False, 0.5, '0', None):
            with self.subTest(participant=invalid), patch.object(self.runtime, 'start'):
                created = await self.runtime.create('owner', 'invalid-plan', {
                    'goal': 'Two independent tasks', 'project_path': '/project',
                    'leader': self.selection, 'workers': [self.selection],
                    'config': {'trusted_host': False, 'reviewer': False}})
                tid = created['team_id']
                planner = self.store.list_workers('owner', tid)[0]
                claim = self.store.claim_worker('owner', tid, worker_id=planner['id'])
                self.store.finish_worker('owner', tid, planner['id'], claim['lease_token'], {'plan': {'tasks': [
                    {'participant': 0, 'objective': 'Valid first assignment'},
                    {'participant': invalid, 'objective': 'Invalid later assignment'}]}})
                with self.assertRaises(ValueError):
                    await self.runtime.coordinate('owner', tid)
                self.assertEqual(len(self.store.list_workers('owner', tid)), 1)

    async def test_snapshot_cursor_and_budget_match_durable_event_log(self):
        self.worker()
        event = self.runtime.event('owner', self.task['id'], 'guidance', {'text': 'Run tests'})
        snapshot = self.runtime.snapshot('owner', self.task['id'])
        self.assertEqual(snapshot['last_seq'], event['seq'])
        self.assertEqual(snapshot['resources']['remaining_microusd'], 100_000)
        self.assertEqual(snapshot['tasks'][0]['depends_on'], [])
        self.assertEqual(self.store.events('owner', self.task['id'], after_seq=event['seq']), [])
        with self.assertRaises(NotFound):
            self.runtime.snapshot('other-owner', self.task['id'])

    async def test_unsupported_prose_cannot_be_marked_done(self):
        worker = self.worker()
        self.responses = [answer('I will fix it')] * 3
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('without any verified tool result', result['result']['error'])
        self.assertEqual(self.host_calls, [])

    async def test_manual_resume_after_unverified_prose_gets_fresh_tool_chance(self):
        worker = self.worker(objective='Inspect example using read_file')
        self.responses = [answer('I inspected the file and it is correct')] * 3
        failed = await self.execute(worker)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(self.host_calls, [])

        self.store.update_worker('owner', self.task['id'], worker['id'], status='pending')
        self.responses = [answer('', [tool('read_file', {'path': '/project/example.py'}, 'read-1')]),
                          answer('Verified from the file result')]
        resumed = await self.execute(worker)

        self.assertEqual(resumed['status'], 'done')
        self.assertEqual(resumed['result']['successful_tools'], 1)
        self.assertTrue(any(op == 'file.call' for op, *_ in self.host_calls))
        self.assertTrue(any('explicitly resumed' in str(message.get('content'))
                            for request in self.messages for message in request))

    async def test_repeated_failed_read_stops_without_false_success(self):
        worker = self.worker()
        self.host_result = {'error': 'No such file', 'exit_code': 1}
        self.responses = [answer('', [tool(identifier=f'call-{i}')]) for i in range(3)]
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Repeated tool failure', result['result']['error'])
        self.assertEqual(len(self.host_calls), 2)

    async def test_exact_read_only_acceptance_can_finish_after_stagnant_status_reads(self):
        worker = self.worker(
            objective='Compute 17 * 19 = 323 using only the selected workers.',
            acceptance="The result must be exactly '323'.",
            write_scope=[],
        )
        final = {
            'completed': True,
            'result': '323',
            'acceptance_met': True,
            'verification': {'expected': '323', 'actual': '323', 'match': True},
            'paths_touched': [],
            'files_modified': False,
            'network_used': False,
            'host_tools_used': False,
            'unresolved_issues': [],
        }
        self.responses = [
            answer('', [tool('team_status', {'limit': 10}, identifier=f'status-{i}')])
            for i in range(3)
        ] + [answer(json.dumps(final))]

        result = await self.execute(worker)

        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['successful_tools'], 0)
        self.assertEqual(self.host_calls, [])

    async def test_exact_read_only_acceptance_can_finish_without_tool_calls(self):
        worker = self.worker(
            objective='Compute 17 * 19 = 323 using only the selected workers.',
            acceptance="The result must be exactly '323'.",
            write_scope=[],
        )
        final = {
            'completed': True,
            'result': '323',
            'acceptance_met': True,
            'verification': {'expected': '323', 'actual': '323', 'match': True},
            'paths_touched': [],
            'files_modified': False,
            'network_used': False,
            'host_tools_used': False,
            'unresolved_issues': [],
        }
        self.responses = [answer(json.dumps(final))]

        result = await self.execute(worker)

        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['completion_validation'], 'exact_read_only_acceptance')
        self.assertEqual(self.tool_schemas, [[]], 'exact-value read-only workers must not receive tools')
        self.assertEqual(self.host_calls, [])

    async def test_planner_arithmetic_prose_acceptance_gets_tool_free_correction(self):
        self.store.update_task_metadata('owner', self.task['id'], {
            'goal': 'QA arithmetic: compute twelve times eleven. Reasoning only; do not use tools.'})
        worker = self.worker(
            name='Compute 12 × 11',
            objective='Calculate 12 × 11 using arithmetic reasoning.',
            acceptance='The computed result equals exactly 132, confirmed by decomposition.',
            write_scope=[],
        )
        final = {
            'completed': True, 'result': '132', 'acceptance_met': True,
            'verification': {'expected': '132', 'actual': '132', 'match': True},
            'paths_touched': [], 'files_modified': False, 'network_used': False,
            'host_tools_used': False, 'unresolved_issues': [],
        }
        self.responses = [answer('12 × 11 = 132'), answer(json.dumps(final))]
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['completion_validation'], 'exact_read_only_acceptance')
        self.assertEqual(self.tool_schemas, [[], []])
        self.assertEqual(self.host_calls, [])

    async def test_planner_arithmetic_wrong_acceptance_still_requires_evidence(self):
        self.store.update_task_metadata('owner', self.task['id'], {
            'goal': 'QA arithmetic: compute twelve times eleven. Reasoning only; do not use tools.'})
        worker = self.worker(
            name='Compute 12 × 11', objective='Calculate 12 × 11.',
            acceptance='The computed result equals exactly 133.', write_scope=[],
        )
        self.responses = [answer('133')] * 3
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('without any verified tool result', result['result']['error'])

    async def test_planner_arithmetic_numeric_json_is_verified_without_tools(self):
        self.store.update_task_metadata('owner', self.task['id'], {
            'goal': 'QA arithmetic: compute twelve times eleven. Reasoning only; do not use tools.'})
        worker = self.worker(
            name='Compute 12 × 10', objective='Calculate 12 multiplied by 10: 12 × 10 = 120',
            acceptance='Result is exactly 120, verified by place value', write_scope=[],
        )
        final = {
            'completed': True, 'result': 120, 'acceptance_met': True,
            'verification': {'expected': 120, 'actual': 120, 'match': True},
            'paths_touched': [], 'files_modified': False, 'network_used': False,
            'host_tools_used': False, 'unresolved_issues': [],
        }
        self.responses = [answer(json.dumps(final))]
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['completion_validation'], 'exact_read_only_acceptance')
        self.assertEqual(self.tool_schemas, [[]])

    async def test_coordinator_accepts_server_checked_arithmetic_without_reviewer_tools(self):
        goal = ('QA arithmetic: compute twelve times eleven. Success criterion: final result 132. '
                'Reasoning only; do not use host commands, files, network, or tools.')
        self.store.update_task_metadata('owner', self.task['id'], {'goal': goal})
        worker = self.worker(name='Verify final result',
                             objective='Confirm the final answer is 132',
                             acceptance='Final result equals 132; all prior subtasks accepted',
                             write_scope=[])
        final = {'completed': True, 'result': 132, 'acceptance_met': True,
                 'verification': {'expected': 132, 'actual': 132, 'match': True},
                 'paths_touched': [], 'files_modified': False,
                 'network_used': False, 'host_tools_used': False,
                 'unresolved_issues': []}
        self.responses = [answer(json.dumps(final))]
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'done')
        await self.runtime.coordinate('owner', self.task['id'])
        self.assertEqual(self.store.get_worker('owner', self.task['id'], worker['id'])['status'], 'accepted')
        self.assertFalse(any(w['profile'].get('kind') == 'verification' for w in
                             self.store.list_workers('owner', self.task['id'])))
        self.assertTrue(any(e['type'] == 'worker_exact_result_verified' for e in
                            self.store.events('owner', self.task['id'])))

    async def test_failed_dependency_does_not_leave_team_running_forever(self):
        parent = self.worker()
        self.store.add_worker('owner', self.task['id'], 'Dependent',
                              profile={**self.selection, 'kind': 'worker', 'role': 'executor',
                                       'objective': 'Use parent result', 'acceptance': 'Verify it'},
                              depends_on=[parent['id']])
        self.store.update_worker('owner', self.task['id'], parent['id'], status='failed')
        self.store.set_task_status('owner', self.task['id'], 'running')
        await self.runtime.coordinate('owner', self.task['id'])
        self.assertEqual(self.store.get_task('owner', self.task['id'])['status'], 'blocked')

    def test_planner_multistep_arithmetic_uses_server_checked_goal(self):
        goal = ('QA arithmetic: compute twelve times eleven. Success criterion: final result 132. '
                'Reasoning only; do not use host commands, files, network, or tools.')
        for name, objective, acceptance in (
            ('Sum partial products', 'Add the partial results: 120 + 12 = 132',
             'Sum equals exactly 132; cross-check multiplication'),
            ('Verify final result', 'Confirm the final answer is 132',
             'Final result equals 132; all prior subtasks accepted'),
        ):
            with self.subTest(name=name):
                profile = {'kind': 'worker', 'name': name, 'objective': objective,
                           'acceptance': acceptance, 'write_scope': []}
                self.assertEqual(_exact_acceptance_target(profile, goal), '132')
                final = {'completed': True, 'result': 132, 'acceptance_met': True,
                         'verification': {'expected': 132, 'actual': 132, 'match': True},
                         'paths_touched': [], 'files_modified': False,
                         'network_used': False, 'host_tools_used': False,
                         'unresolved_issues': []}
                self.assertTrue(_exact_read_only_acceptance(profile, json.dumps(final), goal))
                if name == 'Verify final result':
                    self.assertIsNone(_exact_acceptance_target(profile, 'Implement and verify a project'))

    async def test_exact_read_only_worker_corrects_bare_value_without_suggesting_tools(self):
        worker = self.worker(objective='Return exactly 42 using only reasoning.',
                             acceptance='The result must be exactly "42".', write_scope=[])
        final = {
            'completed': True, 'result': '42', 'acceptance_met': True,
            'verification': {'expected': '42', 'actual': '42', 'match': True},
            'paths_touched': [], 'files_modified': False, 'network_used': False,
            'host_tools_used': False, 'unresolved_issues': [],
        }
        self.responses = [answer('42'), answer(json.dumps(final))]

        result = await self.execute(worker)

        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['completion_validation'], 'exact_read_only_acceptance')
        self.assertEqual(self.tool_schemas, [[], []])
        self.assertEqual(self.host_calls, [])
        self.assertIn('structured result', self.messages[0][0]['content'])
        self.assertIn('JSON contract', self.messages[1][-1]['content'])
        self.assertNotIn('Use the allowed tools', self.messages[1][-1]['content'])

    async def test_resumed_exact_read_only_worker_does_not_receive_host_tool_nudge(self):
        worker = self.worker(objective='Return exactly 42 using only reasoning.',
                             acceptance='The result must be exactly "42".', write_scope=[])
        self.responses = [answer('42') for _ in range(3)]
        failed = await self.execute(worker)
        self.assertEqual(failed['status'], 'failed')
        self.store.update_worker('owner', self.task['id'], worker['id'], status='pending')
        self.responses = [answer(json.dumps({
            'completed': True, 'result': '42', 'acceptance_met': True,
            'verification': {'expected': '42', 'actual': '42', 'match': True},
            'paths_touched': [], 'files_modified': False, 'network_used': False,
            'host_tools_used': False, 'unresolved_issues': [],
        }))]

        resumed = await self.execute(worker)

        self.assertEqual(resumed['status'], 'done')
        prompt = json.dumps(self.messages[-1])
        self.assertIn('explicitly resumed', prompt)
        self.assertIn('structured result', prompt)
        self.assertNotIn('Use an actually available tool', prompt)

    async def test_explicit_python_requirement_cannot_finish_with_exact_self_report(self):
        self.store.update_task_metadata('owner', self.task['id'], {
            'goal': 'Compute 6 * 7 using Python and return exactly 42'})
        worker = self.worker(objective='Calculate 6 * 7 using Python',
                             acceptance="The result must be exactly '42'.", write_scope=[])
        claimed = json.dumps({
            'completed': True, 'result': '42', 'acceptance_met': True,
            'verification': {'expected': '42', 'actual': '42', 'match': True},
            'paths_touched': [], 'files_modified': False, 'network_used': False,
            'host_tools_used': False, 'unresolved_issues': [],
        })
        self.responses = [answer(claimed) for _ in range(3)]
        with patch('src.team_tools.schemas', return_value=[PYTHON_SCHEMA]):
            result = await self.execute(worker)

        self.assertEqual(result['status'], 'failed')
        self.assertFalse(self.host_calls)
        self.assertTrue(all(any(tool['function']['name'] == 'python' for tool in tools)
                            for tools in self.tool_schemas), 'Python must remain offered')

    async def test_unrelated_successful_tool_does_not_satisfy_required_python(self):
        self.store.update_task_metadata('owner', self.task['id'], {'goal': 'Compute 6 * 7 using Python'})
        worker = self.worker(objective='Calculate 6 * 7 using Python',
                             acceptance="The result must be exactly '42'.", write_scope=[])
        self.responses = [answer('', [tool('read_file', {'path': '/project/example.py'})])]
        self.responses.extend(answer('42') for _ in range(3))

        with patch('src.team_tools.schemas', return_value=[PYTHON_SCHEMA]):
            result = await self.execute(worker)

        self.assertEqual(result['status'], 'failed')
        self.assertEqual([intent['name'] for intent in self.store.list_tool_intents(
            'owner', self.task['id'], worker['id'])], ['read_file'])
        self.assertIn('Required Python execution', result['result']['error'])

    async def test_verified_python_intent_allows_completion_and_acceptance(self):
        self.store.update_task_metadata('owner', self.task['id'], {'goal': 'Compute 6 * 7 using Python'})
        worker = self.worker(objective='Calculate 6 * 7 using Python',
                             acceptance="The result must be exactly '42'.", write_scope=['.'])
        self.host_result = {'output': '42\n', 'exit_code': 0}
        self.responses = [answer('', [tool('python', {'code': 'print(6 * 7)'})]),
                          answer('The verified Python output is 42')]

        async def python_host(owner, scope, op, args):
            self.host_calls.append((op, copy.deepcopy(args), owner, scope))
            if op == 'command.start':
                return {'id': 'qa-python-job'}
            if op == 'terminal.poll':
                return {'status': 'done', 'output': '42\n', 'next_offset': 3, 'exit_code': 0}
            raise AssertionError(f'Unexpected host operation: {op}')

        with patch('src.team_tools.schemas', return_value=[PYTHON_SCHEMA]), \
                patch.object(self.runtime, 'host_call', new=python_host):
            result = await self.execute(worker)

        self.assertEqual(result['status'], 'done', result)
        intents = self.store.list_tool_intents('owner', self.task['id'], worker['id'])
        self.assertEqual([(item['name'], item['status']) for item in intents], [('python', 'done')])
        await self.runtime.accept_result('owner', self.task['id'], worker['id'])
        self.assertEqual(self.store.get_worker('owner', self.task['id'], worker['id'])['status'], 'accepted')

    async def test_required_python_with_read_only_scope_blocks_before_model_call(self):
        self.store.update_task_metadata('owner', self.task['id'], {'goal': 'Вычисли 6 * 7 с помощью Python'})
        worker = self.worker(objective='Calculate 6 * 7',
                             acceptance="The result must be exactly '42'.", write_scope=[])

        result = await self.execute(worker)

        self.assertEqual(result['status'], 'waiting_approval')
        self.assertIn('python tool is not permitted', result['result']['error'])
        self.assertFalse(self.messages)
        self.assertFalse(self.host_calls)

    async def test_python_tool_output_must_match_exact_acceptance(self):
        self.store.update_task_metadata('owner', self.task['id'], {'goal': 'Compute 6 * 7 using Python'})
        worker = self.worker(objective='Calculate 6 * 7 using Python',
                             acceptance="The result must be exactly '42'.", write_scope=['.'])
        self.responses = [answer('', [tool('python', {'code': 'print(41)'})])]
        self.responses.extend(answer('42') for _ in range(3))

        async def wrong_python(owner, scope, op, args):
            if op == 'command.start':
                return {'id': 'wrong-python-job'}
            if op == 'terminal.poll':
                return {'status': 'done', 'output': '41\n', 'next_offset': 3, 'exit_code': 0}
            raise AssertionError(f'Unexpected host operation: {op}')

        with patch('src.team_tools.schemas', return_value=[PYTHON_SCHEMA]), \
                patch.object(self.runtime, 'host_call', new=wrong_python):
            result = await self.execute(worker)

        self.assertEqual(result['status'], 'failed')
        self.assertIn('Required Python execution', result['result']['error'])
        self.assertEqual(self.store.list_tool_intents('owner', self.task['id'], worker['id'])[0]['status'], 'done')

    async def test_human_acceptance_cannot_override_missing_required_python(self):
        self.store.update_task_metadata('owner', self.task['id'], {'goal': 'Compute 6 * 7 using Python'})
        worker = self.worker(objective='Calculate 6 * 7 using Python',
                             acceptance="The result must be exactly '42'.", write_scope=[])
        claim = self.store.claim_worker('owner', self.task['id'], worker_id=worker['id'])
        self.store.finish_worker('owner', self.task['id'], worker['id'], claim['lease_token'],
                                 {'summary': '42', 'completed': True})

        with self.assertRaisesRegex(Conflict, 'Required Python execution'):
            await self.runtime.accept_result('owner', self.task['id'], worker['id'])
        self.assertEqual(self.store.get_worker('owner', self.task['id'], worker['id'])['status'], 'done')

    async def test_exact_read_only_acceptance_rejects_wrong_value_after_stagnant_reads(self):
        worker = self.worker(
            objective='Compute 17 * 19 = 323 using only the selected workers.',
            acceptance="The result must be exactly '323'.",
            write_scope=[],
        )
        final = {
            'completed': True,
            'result': '324',
            'acceptance_met': True,
            'verification': {'expected': '323', 'actual': '324', 'match': False},
            'paths_touched': [],
            'files_modified': False,
            'network_used': False,
            'host_tools_used': False,
            'unresolved_issues': [],
        }
        self.responses = [
            answer('', [tool('team_status', {'limit': 10}, identifier=f'status-{i}')])
            for i in range(3)
        ] + [answer(json.dumps(final))]

        result = await self.execute(worker)

        self.assertEqual(result['status'], 'failed')
        self.assertIn('No progress after repeated unchanged reads', result['result']['error'])

    async def test_reviewer_cannot_mutate_host_even_if_model_requests_it(self):
        worker = self.worker(role='reviewer', kind='verification', target_worker='not-dispatched')
        self.responses = [answer('', [tool('write_file', {'path': '/project/a', 'content': 'x'})])]
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'waiting_approval')
        self.assertEqual(self.host_calls, [])
        self.assertEqual(self.store.list_tool_intents('owner', self.task['id']), [])

    async def test_host_revocation_is_rechecked_between_tools_in_same_answer(self):
        worker = self.worker()
        self.responses = [answer('', [tool(identifier='read'), tool('write_file',
            {'path': '/project/a', 'content': 'x'}, identifier='write')])]
        original_host = self.host
        async def revoke_after_read(*args, **kwargs):
            result = await original_host(*args, **kwargs)
            self.store.update_task_metadata('owner', self.task['id'],
                                           {'config': {**self.config, 'trusted_host': False}})
            return result
        self.runtime.host = revoke_after_read
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'waiting_approval')
        self.assertEqual(len(self.host_calls), 1)
        self.assertEqual(len(self.store.list_tool_intents('owner', self.task['id'])), 1)

    async def test_owner_guidance_enters_context_without_other_workers_guidance(self):
        worker = self.worker()
        self.runtime.event('owner', self.task['id'], 'guidance', {'worker_id': worker['id'], 'text': 'Check edge cases'})
        self.runtime.event('owner', self.task['id'], 'guidance', {'worker_id': 'other', 'text': 'UNRELATED_GUIDANCE'})
        self.responses = [answer('', [tool()]), answer()]
        await self.execute(worker)
        prompt = json.dumps(self.messages[0])
        self.assertIn('Check edge cases', prompt)
        self.assertNotIn('UNRELATED_GUIDANCE', prompt)

    async def test_cloud_without_approval_never_reaches_transport(self):
        worker = self.worker()
        self.route['local'] = False
        self.store.update_task_metadata('owner', self.task['id'], {'config': {**self.config, 'external': True},
            'external_data_scopes': {'local': 'assigned_context'}})
        result = await self.execute(worker)
        self.assertEqual(result['status'], 'waiting_approval')
        self.assertEqual(self.messages, [])
        self.assertEqual(self.store.list_reservations('owner', self.task['id']), [])

    async def test_cloud_unknown_usage_charges_reserved_ceiling_once(self):
        worker = self.worker()
        self.route['local'] = False
        self.store.update_task_metadata('owner', self.task['id'], {'config': {**self.config, 'external': True},
            'external_data_scopes': {'local': 'assigned_context'}})
        self.store.approve_endpoint('owner', self.task['id'], 'local', 100_000, 1_000_000, 1_000_000)
        claim = self.store.claim_worker('owner', self.task['id'])
        self.responses = [asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.model_call('owner', self.task['id'], claim, claim['lease_token'],
                                          [{'role': 'user', 'content': 'Hello'}], [])
        reservation = self.store.list_reservations('owner', self.task['id'])[0]
        self.assertEqual(reservation['status'], 'settled_unknown')
        self.assertEqual(reservation['charged_microusd'], reservation['reserved_microusd'])
        self.assertEqual(self.store.get_task('owner', self.task['id'])['reserved_microusd'], 0)

    async def test_two_runtime_instances_do_not_coordinate_same_task_concurrently(self):
        entered, leave = asyncio.Event(), asyncio.Event()
        calls = []
        async def coordinate(owner, task_id, token):
            self.store.assert_coordinator(owner, task_id, token)
            calls.append(token)
            entered.set()
            await leave.wait()
        other = TeamRuntime(TeamStore(self.store.path, clock=lambda: self.now), host=self.host)
        self.runtime._coordinate = coordinate
        other._coordinate = coordinate
        first = asyncio.create_task(self.runtime.coordinate('owner', self.task['id']))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(other.coordinate('owner', self.task['id']), 1)
            self.assertEqual(len(calls), 1)
        finally:
            leave.set()
            await first
            await other.close()
        self.assertIsNotNone(self.store.claim_coordinator('owner', self.task['id']))


if __name__ == '__main__':
    unittest.main()
