"""Actual Team native-tool loop and durable ledger, only model/MCP I/O faked."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.team_mcp import TeamMCPStore, review_catalogue, _server_configs, _owner_policy, dispatch
from src.team_runtime import TeamRuntime
from src.team_store import TeamStore
from src import team_tools


class TeamMCPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TeamStore(Path(temporary.name) / 'team.db')
        self.policy = TeamMCPStore(self.store)
        self.name = 'mcp__docs__search'
        self.calls, self.sent, self.responses = [], [], []
        self.revoke_after_first = False
        outer = self
        class Manager:
            def get_all_tools(self):
                return [{'server_id': 'docs', 'name': 'search', 'description': 'Read public docs',
                         'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}]
            def get_server_status(self, server):
                return {'status': 'connected', 'transport': 'stdio'}
            def is_builtin(self, server):
                return False
            async def call_tool(self, name, args):
                outer.calls.append((name, copy.deepcopy(args)))
                if outer.revoke_after_first:
                    outer.policy.revoke('owner', outer.name, 1)
                return {'stdout': 'Public documentation evidence', 'exit_code': 0}
        self.manager = Manager()
        self.selection = {'endpoint_id': 'local', 'model': 'fixture'}
        self.config = {'trusted_host': False, 'web': False, 'external': False, 'mcp': True}
        self.task = self.store.create_task('owner', 'Read public docs', metadata={
            'goal': 'Read documentation', 'project_path': '/project', 'config': self.config,
            'leader': self.selection, 'participants': [self.selection]})
        self.runtime = TeamRuntime(self.store, complete=self.complete, host=self.host)
        for replacement in (
            patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1', 'ODYSSEUS_TEAM_MCP_ENABLED': '1'}),
            patch('src.team_mcp._server_configs', return_value={'docs': {'enabled': True, 'config_digest': 'fixture'}}),
            patch('src.team_mcp._owner_policy', return_value={'allowed': True, 'disabled_tools': []}),
            patch('src.tool_utils.get_mcp_manager', return_value=self.manager),
            patch('src.team_config.resolve', return_value={**self.selection, 'local': True, 'resource_group': 'jetson'}),
        ):
            replacement.start(); self.addCleanup(replacement.stop)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def complete(self, route, messages, tools, **kwargs):
        self.sent.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return {'message': self.responses.pop(0), 'usage': {}}

    async def host(self, *args, **kwargs):
        self.fail('MCP must never fall back to the host dispatcher')

    def grant(self):
        return self.policy.review('owner', self.name, review_catalogue(self.manager)[0]['schema_digest'],
                                 ['read_public'], ['executor'], confirmation=True)

    def tool(self, identifier='call-1'):
        return {'id': identifier, 'type': 'function', 'function': {'name': self.name,
                                                                 'arguments': json.dumps({'query': 'docs'})}}

    async def run_worker(self, calls=None):
        worker = self.store.add_worker('owner', self.task['id'], 'Research', profile={
            **self.selection, 'kind': 'worker', 'role': 'executor', 'cwd': '/project',
            'objective': 'Read docs', 'acceptance': 'Cite verified evidence'})
        self.responses = [{'role': 'assistant', 'content': '', 'tool_calls': calls or [self.tool()]},
                          {'role': 'assistant', 'content': 'Documentation evidence verified'}]
        claim = self.store.claim_worker('owner', self.task['id'], worker_id=worker['id'])
        await self.runtime.run_worker('owner', self.task['id'], claim)
        return self.store.get_worker('owner', self.task['id'], worker['id'])

    async def test_reviewed_native_schema_dispatch_and_durable_read_result(self):
        self.grant()
        result = await self.run_worker()
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['result']['successful_tools'], 1)
        self.assertIn(self.name, {s['function']['name'] for s in self.sent[0][1]})
        self.assertEqual(self.calls, [(self.name, {'query': 'docs'})])
        ledger = self.store.list_tool_intents('owner', self.task['id'])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['status'], 'done')
        self.assertFalse(ledger[0]['effectful'])
        self.assertTrue(ledger[0]['result']['untrusted_content'])
        self.assertEqual(ledger[0]['result']['source']['policy_revision'], 1)

    async def test_unknown_result_stops_batch_and_preserves_closed_checkpoint(self):
        self.grant()
        async def uncertain(name, args):
            self.calls.append((name, args))
            return {'exit_code': 1, 'outcome_unknown': True, 'retryable': False}
        self.manager.call_tool = uncertain
        worker = await self.run_worker([self.tool('first'), self.tool('second')])
        self.assertEqual(worker['status'], 'blocked')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.calls), 1)
        intents = self.store.list_tool_intents('owner', self.task['id'])
        self.assertEqual([item['status'] for item in intents], ['unknown'])
        checkpoint = self.store.load_checkpoint('owner', self.task['id'], worker['id'])
        responses = [message for message in checkpoint['payload']['messages'] if message['role'] == 'tool']
        batch = next(message['tool_calls'] for message in checkpoint['payload']['messages'] if message.get('tool_calls'))
        self.assertEqual(len(batch), 2)
        self.assertEqual([message['tool_call_id'] for message in responses], [call['id'] for call in batch])
        self.assertTrue(json.loads(responses[0]['content'])['outcome_unknown'])
        self.assertTrue(json.loads(responses[1]['content'])['not_executed'])
        self.store.resolve_tool_intent('owner', self.task['id'], intents[0]['id'],
                                       {'observation': 'human-inspected-marker'}, status='done')
        async def verify(name, args):
            self.calls.append((name, args))
            return {'exit_code': 0, 'stdout': 'fresh verification evidence'}
        self.manager.call_tool = verify
        verification = self.tool('new-verification')
        verification['function']['arguments'] = json.dumps({'query': 'verify-after-review'})
        self.responses = [{'role': 'assistant', 'content': '', 'tool_calls': [verification]},
                          {'role': 'assistant', 'content': 'Verified result'}]
        resumed = self.store.claim_worker('owner', self.task['id'], worker_id=worker['id'])
        await self.runtime.run_worker('owner', self.task['id'], resumed)
        final = self.store.get_worker('owner', self.task['id'], worker['id'])
        self.assertEqual(final['status'], 'done')
        self.assertEqual(final['result']['successful_tools'], 1, 'human reconciliation is not automatic tool success')
        self.assertEqual([args['query'] for _, args in self.calls], ['docs', 'verify-after-review'])
        self.assertIn('human-inspected-marker', json.dumps(self.sent[1][0]))

    async def test_unreviewed_native_request_denied_before_ledger_or_io(self):
        result = await self.run_worker()
        self.assertEqual(result['status'], 'waiting_approval')
        self.assertNotIn(self.name, {s['function']['name'] for s in self.sent[0][1]})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.list_tool_intents('owner', self.task['id']), [])

    async def test_paused_task_blocks_mcp_without_disabling_other_tasks_grant(self):
        self.grant()
        worker = self.store.add_worker('owner', self.task['id'], 'Research', profile={
            **self.selection, 'kind': 'worker', 'role': 'executor', 'cwd': '/project'})
        self.store.set_task_status('owner', self.task['id'], 'paused')
        with self.assertRaises(PermissionError):
            await self.runtime.execute_tool('owner', self.task['id'], worker,
                                            self.name, {}, 'paused-call', '/project')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.policy.get('owner', self.name)['enabled'])

    async def test_revoke_between_two_calls_blocks_second_despite_advertised_schema(self):
        self.grant()
        self.revoke_after_first = True
        result = await self.run_worker([self.tool(), self.tool('call-2')])
        self.assertEqual(result['status'], 'waiting_approval')
        self.assertEqual(len(self.calls), 1)
        ledger = self.store.list_tool_intents('owner', self.task['id'])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['status'], 'done')

    async def test_flag_off_preserves_builtin_surface_and_never_loads_mcp(self):
        config = {**self.config, 'trusted_host': True}
        expected = team_tools.schemas('owner', 'executor', config=config)
        with patch.dict(os.environ, {'ODYSSEUS_TEAM_MCP_ENABLED': '0'}), \
             patch('src.team_mcp.review_catalogue', side_effect=AssertionError('Flag-off discovery forbidden')):
            actual = team_tools.schemas('owner', 'executor', config=config, store=self.store)
            self.assertEqual(actual, expected)
            with self.assertRaises(PermissionError):
                team_tools.validate_action(self.name, {}, 'executor', config, owner='owner', store=self.store)

    async def test_actual_admin_database_config_and_privilege_revocation_are_fresh(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.database import McpServer
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        McpServer.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(McpServer(id='docs', name='Public docs fixture', is_enabled=True,
                            command='/fixture-server', env='{"key":"PRIVATE_CONFIG_SENTINEL"}', disabled_tools='[]'))
            db.commit()
        with patch('core.database.SessionLocal', sessions), \
             patch('src.team_mcp._server_configs', side_effect=_server_configs), \
             patch('src.team_mcp._owner_policy', side_effect=_owner_policy), \
             patch('src.tool_execution._owner_is_admin', return_value=True), \
             patch('src.tool_execution._current_agent_privileges', return_value={'can_use_agent': True}) as privileges, \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: default):
            catalogue = review_catalogue(self.manager)
            self.assertNotIn('PRIVATE_CONFIG_SENTINEL', json.dumps(catalogue))
            self.grant()
            result = await dispatch(self.store, 'owner', 'executor', lambda: self.config, self.name, {}, self.manager)
            self.assertEqual(result['exit_code'], 0)
            with sessions() as db:
                db.get(McpServer, 'docs').disabled_tools = '["search"]'
                db.commit()
            with self.assertRaises(PermissionError):
                await dispatch(self.store, 'owner', 'executor', lambda: self.config, self.name, {}, self.manager)
            with sessions() as db:
                db.get(McpServer, 'docs').disabled_tools = '[]'
                db.commit()
            digest = review_catalogue(self.manager)[0]['schema_digest']
            self.policy.review('owner', self.name, digest, ['read_public'], ['executor'],
                               expected_revision=1, confirmation=True)
            privileges.return_value = {'can_use_agent': False}
            with self.assertRaises(PermissionError):
                await dispatch(self.store, 'owner', 'executor', lambda: self.config, self.name, {}, self.manager)
            self.assertEqual(len(self.calls), 1)
