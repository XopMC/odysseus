"""Reviewed Team MCP: real durable grants, fake only the remote MCP boundary."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.team_store import TeamStore, Conflict, NotFound
from src.team_mcp import TeamMCPStore, review_catalogue, surface, dispatch, assert_review_access


class Manager:
    def __init__(self):
        self.calls = []
        self.tools = [{'server_id': 'docs', 'name': 'search', 'description': 'Read public docs',
                       'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}]
        self.status = {'status': 'connected', 'transport': 'stdio', 'version': '1'}

    def get_all_tools(self):
        return copy.deepcopy(self.tools)

    def get_server_status(self, server):
        return dict(self.status)

    def is_builtin(self, server):
        return False

    async def call_tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return {'stdout': 'Document evidence', 'exit_code': 0}


class TeamMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TeamStore(Path(directory.name) / 'teams.db')
        self.policy = TeamMCPStore(self.store)
        self.manager = Manager()
        self.configs = {'docs': {'enabled': True, 'config_digest': 'fixture-config-v1'}}
        configured = patch('src.team_mcp._server_configs', side_effect=lambda: copy.deepcopy(self.configs))
        configured.start(); self.addCleanup(configured.stop)
        account = patch('src.team_mcp._owner_policy', return_value={'allowed': True, 'disabled_tools': []})
        self.account = account.start(); self.addCleanup(account.stop)
        flags = patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1', 'ODYSSEUS_TEAM_MCP_ENABLED': '1'})
        flags.start(); self.addCleanup(flags.stop)
        self.config = {'trusted_host': False, 'web': False, 'mcp': True}
        self.name = 'mcp__docs__search'

    def grant(self, owner='alice', **overrides):
        item = review_catalogue(self.manager)[0]
        return self.policy.review(owner, self.name, item['schema_digest'],
            overrides.get('effects', ['read_public']), overrides.get('roles', ['executor', 'reviewer']),
            expected_revision=overrides.get('expected_revision', 0), confirmation=True)

    async def test_reviewed_read_has_same_spec_in_discovery_and_actual_dispatch(self):
        self.grant()
        registry, access = surface(self.store, 'alice', 'executor', self.config, [], self.manager)
        spec = registry.require(self.name, access)
        self.assertEqual(registry.schemas(access)[0]['function']['name'], spec.id)
        result = await dispatch(self.store, 'alice', 'executor', lambda: self.config,
                                self.name, {'query': 'docs'}, self.manager)
        self.assertEqual(self.manager.calls, [(spec.id, {'query': 'docs'})])
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['untrusted_content'])
        self.assertEqual(result['source']['tool_id'], spec.id)

    async def test_unknown_hint_and_model_metadata_cannot_authorize(self):
        self.manager.tools[0]['annotations'] = {'readOnlyHint': True}
        malicious = {**self.config, 'trusted_mcp_capabilities': {self.name: ['read_public']},
                     'mcp_grants': {self.name: {'enabled': True}}}
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: malicious, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_unknown_outcome_survives_team_normalization(self):
        self.grant()
        async def uncertain(name, args):
            self.manager.calls.append((name, args))
            return {'exit_code': 0, 'outcome_unknown': True, 'retryable': True,
                    'stdout': 'unverified success', 'error': 'secret remote detail'}
        self.manager.call_tool = uncertain
        result = await dispatch(self.store, 'alice', 'executor', lambda: self.config,
                                self.name, {}, self.manager)
        self.assertEqual(result['exit_code'], 1)
        self.assertTrue(result['outcome_unknown'])
        self.assertFalse(result['retryable'])
        self.assertNotIn('stdout', result)
        self.assertNotIn('secret', result['stderr'])
        self.assertEqual(len(self.manager.calls), 1)
        self.assertEqual(result['source']['tool_id'], self.name)

    async def test_transport_timeout_is_unknown_not_permission_to_retry(self):
        self.grant()
        async def lost(name, args):
            self.manager.calls.append((name, args))
            raise TimeoutError('unknown remote outcome')
        self.manager.call_tool = lost
        result = await dispatch(self.store, 'alice', 'executor', lambda: self.config,
                                self.name, {}, self.manager)
        self.assertTrue(result['outcome_unknown'])
        self.assertFalse(result['retryable'])
        self.assertEqual(len(self.manager.calls), 1)

    async def test_grant_is_owner_scoped_and_not_reused_for_another_role(self):
        self.grant()
        for owner, role in (('bob', 'executor'), ('alice', 'researcher'), ('alice', 'unknown')):
            with self.subTest(owner=owner, role=role), self.assertRaises(PermissionError):
                await dispatch(self.store, owner, role, lambda: self.config, self.name, {}, self.manager)
        with self.assertRaises(NotFound):
            self.policy.get('bob', self.name)
        with self.assertRaises(NotFound):
            self.policy.revoke('bob', self.name, 1)
        self.assertEqual(self.policy.list('bob'), [])
        self.assertEqual(self.manager.calls, [])

    async def test_revoke_is_durable_and_blocks_stale_discovery(self):
        grant = self.grant()
        registry, access = surface(self.store, 'alice', 'executor', self.config, [], self.manager)
        self.assertTrue(registry.schemas(access))
        self.policy.revoke('alice', self.name, grant['revision'])
        reopened = TeamMCPStore(TeamStore(self.store.path))
        self.assertFalse(reopened.get('alice', self.name)['enabled'])
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_schema_server_config_and_version_changes_need_new_review(self):
        self.grant()
        self.manager.tools[0]['input_schema']['properties']['new'] = {'type': 'string'}
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.grant(expected_revision=1)
        self.configs['docs']['config_digest'] = 'changed-command-or-endpoint'
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.grant(expected_revision=2)
        self.manager.status['version'] = '2'
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_task_server_and_feature_revocation_all_fail_closed(self):
        self.grant()
        for change in ('task', 'server', 'feature'):
            with self.subTest(change=change):
                config = {**self.config, 'mcp': change != 'task'}
                self.configs['docs']['enabled'] = change != 'server'
                with patch.dict(os.environ, {'ODYSSEUS_TEAM_MCP_ENABLED': '0' if change == 'feature' else '1'}):
                    with self.assertRaises(PermissionError):
                        await dispatch(self.store, 'alice', 'executor', lambda: config, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_mutation_effects_cannot_be_reviewed_or_downgraded(self):
        for effects in (['execute_code'], ['read_public', 'external_side_effect'], [], ['unknown']):
            with self.subTest(effects=effects), self.assertRaises(ValueError):
                self.grant(effects=effects)
        with self.assertRaises(PermissionError):
            self.policy.review('alice', 'mcp__email__send_email', 'a' * 64,
                               ['read_public'], ['executor'], confirmation=True)

    async def test_collision_is_not_last_writer_wins(self):
        self.manager.tools.append(copy.deepcopy(self.manager.tools[0]))
        with self.assertRaises(ValueError):
            review_catalogue(self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_grant_revision_and_confirmation_are_mandatory(self):
        item = review_catalogue(self.manager)[0]
        with self.assertRaises(PermissionError):
            self.policy.review('alice', self.name, item['schema_digest'], ['read_public'], ['executor'])
        self.grant()
        with self.assertRaises(Conflict):
            self.grant()
        with self.assertRaises(Conflict):
            self.policy.revoke('alice', self.name, 0)

    async def test_concurrent_reviews_are_compare_and_swap_not_last_writer_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        def attempt():
            try:
                return self.grant()['revision']
            except Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, [1, 'conflict'])

    async def test_equal_remote_names_keep_distinct_server_namespace(self):
        self.grant()
        self.manager.tools.append({**copy.deepcopy(self.manager.tools[0]), 'server_id': 'other'})
        self.configs['other'] = {'enabled': True, 'config_digest': 'other-fixture'}
        registry, access = surface(self.store, 'alice', 'executor', self.config, [], self.manager)
        self.assertEqual(registry.schemas(access)[0]['function']['name'], self.name)
        self.assertEqual(len(registry.schemas(access)), 1)
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config,
                           'mcp__other__search', {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_owner_argument_injection_and_invalid_result_fail_closed(self):
        self.grant()
        with self.assertRaises(ValueError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name,
                           {'_odysseus_owner': 'bob'}, self.manager)
        async def bad(*args):
            return {'exit_code': False, 'stdout': 'not a verified result'}
        self.manager.call_tool = bad
        result = await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(self.manager.calls, [])
        self.assertTrue(result['outcome_unknown'])
        self.assertFalse(result['retryable'])

    async def test_account_and_global_tool_revocation_override_review(self):
        self.grant()
        for policy in ({'allowed': False, 'disabled_tools': []},
                       {'allowed': True, 'disabled_tools': [self.name]}):
            self.account.return_value = policy
            with self.subTest(policy=policy), self.assertRaises(PermissionError):
                await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_review_api_gate_needs_current_feature_and_account_permission(self):
        assert_review_access('alice')
        self.account.return_value = {'allowed': False}
        with self.assertRaises(PermissionError):
            assert_review_access('alice')
        self.account.return_value = {'allowed': True}
        with patch.dict(os.environ, {'ODYSSEUS_TEAM_MCP_ENABLED': '0'}):
            with self.assertRaises(PermissionError):
                assert_review_access('alice')

    async def test_server_tool_disable_and_disconnect_override_review(self):
        self.grant()
        self.configs['docs']['disabled_tools'] = ['search']
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.configs['docs'].pop('disabled_tools')
        self.manager.status['status'] = 'disconnected'
        with self.assertRaises(PermissionError):
            await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_dispatch_rechecks_review_after_surface_snapshot(self):
        self.grant()
        original = surface
        def revoke_after_discovery(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            self.policy.revoke('alice', self.name, 1)
            return snapshot
        with patch('src.team_mcp.surface', side_effect=revoke_after_discovery):
            with self.assertRaises(PermissionError):
                await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(self.manager.calls, [])

    async def test_transport_exception_hides_secrets_and_output_is_bounded(self):
        self.grant()
        async def fail(*args):
            raise RuntimeError('secret credential fixture')
        self.manager.call_tool = fail
        result = await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(result['exit_code'], 1)
        self.assertNotIn('credential fixture', str(result))
        async def large(*args):
            return {'exit_code': 0, 'stdout': 'a' * 100000, 'stderr': 'b' * 100000}
        self.manager.call_tool = large
        result = await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
        self.assertEqual(len(result['stdout']), 60000)
        self.assertEqual(len(result['stderr']), 60000)

    async def test_manager_errors_and_unsupported_images_never_become_silent_success(self):
        self.grant()
        for value in ({'exit_code': 1, 'error': 'PRIVATE_EXCEPTION_SENTINEL'},
                      {'exit_code': 0, 'images': [{'data': 'PRIVATE_IMAGE_SENTINEL'}]}):
            async def respond(*args):
                return value
            self.manager.call_tool = respond
            result = await dispatch(self.store, 'alice', 'executor', lambda: self.config, self.name, {}, self.manager)
            self.assertEqual(result['exit_code'], 1)
            self.assertTrue(result['stderr'])
            self.assertNotIn('PRIVATE_', str(result))
