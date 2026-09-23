"""Shared catalogue/policy contracts; no models, MCP servers or host calls."""
import json
import os
import copy
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.tool_capabilities import ToolCapabilities, ToolEffect, capabilities_for_tool
from src.tool_registry import (
    ToolAccess, ToolRegistry, canonical_name, mcp_tool_id, policy_names,
    tool_inventory_revision,
)


def schema(name):
    return {'type': 'function', 'function': {'name': name, 'description': 'Fixture',
            'parameters': {'type': 'object', 'properties': {}}}}


class RegistryCoreTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry.from_schemas([
            schema(name) for name in ('bash', 'python', 'read_file', 'write_file',
                                     'web_search', 'web_fetch', 'manage_settings')])

    def team(self, role='executor', **config):
        return ToolAccess.team(role, {'trusted_host': True, 'web': True, **config})

    def test_inventory_revision_hashes_schema_and_effective_policy_not_just_names(self):
        first_schema = schema('read_file')
        first = tool_inventory_revision(
            [schema('bash'), first_schema], selected_names={'bash', 'read_file'},
            disabled_names={'write_file'}, relevant_names={'read_file'},
        )
        reordered = tool_inventory_revision(
            [copy.deepcopy(first_schema), schema('bash')],
            selected_names={'read_file', 'bash'}, disabled_names={'write_file'},
            relevant_names={'read_file'},
        )
        changed_schema = schema('read_file')
        changed_schema['function']['parameters']['properties']['limit'] = {'type': 'integer'}
        changed = tool_inventory_revision(
            [schema('bash'), changed_schema], selected_names={'bash', 'read_file'},
            disabled_names={'write_file'}, relevant_names={'read_file'},
        )
        changed_policy = tool_inventory_revision(
            [schema('bash'), first_schema], selected_names={'bash', 'read_file'},
            disabled_names={'write_file', 'bash'}, relevant_names={'read_file'},
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(first, changed_policy)
        self.assertRegex(first, r'^[0-9a-f]{64}$')

    def test_inventory_revision_hash_is_content_free_and_tracks_execution_mode(self):
        description_secret = 'DO-NOT-PERSIST-PRIVATE-DESCRIPTION'
        private_schema = schema('read_file')
        private_schema['function']['description'] = description_secret
        revision = tool_inventory_revision(
            [private_schema], selected_names={'read_file'}, policy_mode='guide_only',
            block_all=True, disable_mcp=True, plan_mode=True, access_mode='ask_every_time',
        )
        self.assertNotIn(description_secret, revision)
        self.assertNotEqual(revision, tool_inventory_revision(
            [private_schema], selected_names={'read_file'}, policy_mode='normal',
        ))

    def test_shell_alias_has_one_identity_schema_and_shared_effects(self):
        for name in ('bash', 'shell', 'Shell'):
            self.assertEqual(canonical_name(name), 'bash')
            spec = self.registry.require(name, self.team())
            self.assertEqual(spec.id, 'bash')
            self.assertEqual(spec.display_name, 'Shell')
            self.assertEqual(spec.capabilities, capabilities_for_tool('bash'))
        names = [s['function']['name'] for s in self.registry.schemas(self.team())]
        self.assertEqual(names.count('bash'), 1)
        self.assertNotIn('Shell', names)

    def test_policy_aliases_are_symmetric_and_do_not_cross_mcp_namespaces(self):
        self.assertEqual(policy_names('Shell'), frozenset({'bash', 'shell', 'Shell'}))
        self.assertEqual(policy_names('bash'), policy_names('Shell'))
        self.assertIn('read_email', policy_names('mcp__email__read_email'))
        self.assertEqual(canonical_name('mcp__a__Shell'), 'mcp__a__Shell')
        self.assertNotIn('bash', policy_names('mcp__a__Shell'))
        for disabled in ('bash', 'shell', 'Shell'):
            access = ToolAccess.team('executor', {'trusted_host': True,
                                                'disabled_tools': [disabled]})
            for called in ('bash', 'shell', 'Shell'):
                with self.subTest(disabled=disabled, called=called), self.assertRaises(PermissionError):
                    self.registry.require(called, access)

    def test_namespaced_mcp_ids_preserve_server_and_exact_tool_name(self):
        self.assertNotEqual(mcp_tool_id('a', 'read'), mcp_tool_id('b', 'read'))
        self.assertEqual(mcp_tool_id('a', 'read__one'), 'mcp__a__read__one')
        for server, tool in (('a__b', 'read'), ('a', ''), ('a b', 'read'), ('a', '../read')):
            with self.subTest(server=server, tool=tool), self.assertRaises(ValueError):
                mcp_tool_id(server, tool)
        for name in (None, [], '', 'mcp____read', 'mcp__a__', 'bash\n', '../bash'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                canonical_name(name)

    def test_duplicate_or_alias_shadowing_schema_fails_closed(self):
        for values in ([schema('bash'), schema('Shell')], [schema('bash'), schema('bash')]):
            with self.assertRaises(ValueError):
                ToolRegistry.from_schemas(values)
        with self.assertRaises(ValueError):
            ToolRegistry.from_schemas([schema('mcp__a__bash')])

    def test_missing_or_unrecognized_role_config_does_not_grant_tools(self):
        for role in ('unknown', '', None):
            with self.subTest(role=role):
                self.assertEqual(self.registry.schemas(self.team(role)), [])
        for config in (None, {}, {'trusted_host': 'false', 'web': 'true'},
                       {'trusted_host': 1, 'web': 1}, {'trusted_host': True, 'disabled_tools': 'bash'}):
            with self.subTest(config=config):
                access = ToolAccess.team('executor', config)
                self.assertEqual(self.registry.schemas(access), [])

    def test_readonly_roles_cannot_execute_or_write(self):
        for role in ('reviewer', 'researcher'):
            access = self.team(role)
            self.assertEqual({s['function']['name'] for s in self.registry.schemas(access)},
                             {'read_file', 'web_search', 'web_fetch'})
            for name in ('bash', 'Shell', 'python', 'write_file'):
                with self.subTest(role=role, name=name), self.assertRaises(PermissionError):
                    self.registry.require(name, access)

    def test_team_readonly_roles_can_compare_and_verify_without_write_authority(self):
        registry = ToolRegistry.from_schemas([
            schema('compare_files'), schema('verify_hashes'), schema('inspect_toolchain'), schema('write_file')])
        for role in ('reviewer', 'researcher'):
            access = self.team(role)
            self.assertEqual({item['function']['name'] for item in registry.schemas(access)},
                             {'compare_files', 'verify_hashes', 'inspect_toolchain'})
            with self.assertRaises(PermissionError):
                registry.require('write_file', access)

    def test_web_and_host_flags_are_independent_and_fail_closed(self):
        self.assertEqual({s['function']['name'] for s in self.registry.schemas(
            ToolAccess.team('executor', {'web': True}))}, {'web_search', 'web_fetch'})
        with self.assertRaises(PermissionError):
            self.registry.require('web_fetch', self.team(web=False))
        with self.assertRaises(PermissionError):
            self.registry.require('manage_settings', self.team())

    def test_discovery_is_not_a_reusable_dispatch_grant_after_revocation(self):
        config = {'trusted_host': True, 'web': True}
        get_access = lambda: ToolAccess.team('executor', config)
        self.assertEqual(self.registry.require_current('bash', get_access).id, 'bash')
        visible = self.registry.schemas(get_access())
        config['trusted_host'] = False
        self.assertTrue(any(s['function']['name'] == 'bash' for s in visible))
        with self.assertRaises(PermissionError):
            self.registry.require_current('bash', get_access)
        self.assertEqual(self.registry.require_current('web_search', get_access).id, 'web_search')

    def test_missing_recheck_policy_and_adapter_fail_closed(self):
        with self.assertRaises(PermissionError):
            self.registry.require_current('bash', lambda: None)
        with self.assertRaises(PermissionError):
            self.registry.require('bash', ToolAccess(mode='team', role='executor',
                config={'trusted_host': True}, adapters=frozenset()))

    def test_agent_requires_explicit_effective_allowlist(self):
        self.assertEqual(self.registry.schemas(ToolAccess(mode='agent', role='agent',
            config={}, adapters=frozenset({'agent'}))), [])
        access = ToolAccess(mode='agent', role='agent', config={}, adapters=frozenset({'agent'}),
                            allowed_tools=frozenset({'bash', 'read_file'}))
        self.assertEqual({s['function']['name'] for s in self.registry.schemas(access)},
                         {'bash', 'read_file'})
        self.assertEqual(self.registry.require('Shell', access).effects,
                         self.registry.require('bash', self.team()).effects)

    def test_mcp_annotations_are_not_permissions_or_effect_classification(self):
        remote = schema('mcp__untrusted__read')
        remote['annotations'] = {'readOnlyHint': True}
        registry = ToolRegistry.from_schemas([], mcp_schemas=[remote])
        access = ToolAccess(mode='agent', role='agent', config={'mcp': True},
            adapters=frozenset({'mcp'}), enabled_mcp_servers=frozenset({'untrusted'}),
            allowed_tools=frozenset({'mcp__untrusted__read'}))
        with self.assertRaises(PermissionError):
            registry.require('mcp__untrusted__read', access)
        record = registry.public(access)[0]
        self.assertFalse(record['known_effects'])
        self.assertFalse(record['available'])
        self.assertIn('execute_code', record['effects'])

    def test_reviewed_browser_navigation_and_interaction_have_known_effects(self):
        names = (
            'mcp__builtin_browser__browser_snapshot',
            'mcp__builtin_browser__browser_navigate',
            'mcp__builtin_browser__browser_click',
            'mcp__builtin_browser__browser_type',
        )
        registry = ToolRegistry.from_schemas([], mcp_schemas=[schema(name) for name in names])
        access = ToolAccess(mode='agent', role='agent', config={'mcp': True},
                            adapters=frozenset({'mcp'}),
                            enabled_mcp_servers=frozenset({'builtin_browser'}),
                            allowed_tools=frozenset(names))
        for name in names:
            self.assertEqual(registry.require(name, access).id, name)
        self.assertIn(ToolEffect.NETWORK_EGRESS,
                      registry.require('mcp__builtin_browser__browser_navigate', access).effects)
        self.assertIn(ToolEffect.EXTERNAL_SIDE_EFFECT,
                      registry.require('mcp__builtin_browser__browser_click', access).effects)

    def test_explicit_mcp_metadata_does_not_invent_a_team_dispatcher(self):
        name = 'mcp__docs__read'
        registry = ToolRegistry.from_schemas([], mcp_schemas=[schema(name)],
            trusted_mcp_capabilities={name: ToolCapabilities(frozenset({ToolEffect.READ_PUBLIC}))})
        access = ToolAccess(mode='agent', role='agent', config={'mcp': True},
            adapters=frozenset({'mcp'}), enabled_mcp_servers=frozenset({'docs'}),
            allowed_tools=frozenset({name}))
        self.assertEqual(registry.require(name, access).id, name)
        for denied in (ToolAccess(mode='agent', role='agent', config={'mcp': False},
                           adapters=access.adapters, enabled_mcp_servers=access.enabled_mcp_servers,
                           allowed_tools=access.allowed_tools),
                       ToolAccess(mode='team', role='executor', config={'mcp': True},
                           adapters=access.adapters, enabled_mcp_servers=access.enabled_mcp_servers,
                           allowed_tools=access.allowed_tools)):
            with self.assertRaises(PermissionError):
                registry.require(name, denied)

    def test_catalogue_and_schemas_are_defensive_copies_without_config_secrets(self):
        access = ToolAccess.team('executor', {'trusted_host': True, 'api_key': 'DO-NOT-EXPOSE'})
        public = self.registry.public(access)
        self.assertNotIn('DO-NOT-EXPOSE', str(public))
        public[0]['effects'].append('injected')
        schemas = self.registry.schemas(access)
        schemas[0]['function']['parameters']['properties']['injected'] = {}
        self.assertNotIn('injected', str(self.registry.public(access)))
        self.assertNotIn('injected', str(self.registry.schemas(access)))


class TeamRegistryCoreTests(unittest.TestCase):
    def test_team_adapter_requires_config_for_discovery(self):
        from src import team_tools
        with patch.object(team_tools, '_builtin_schemas', return_value=[schema('bash'), schema('read_file')]):
            self.assertEqual(team_tools.schemas('owner', 'executor', False), [])
            with patch('src.host_execution.adapt_schemas', side_effect=lambda schemas, owner: schemas):
                result = team_tools.schemas('owner', 'executor', False, config={'trusted_host': True})
            self.assertEqual({s['function']['name'] for s in result}, {'bash', 'read_file'})

    def test_team_adapter_retains_effectful_boolean_and_command_guards(self):
        from src import team_tools
        config = {'trusted_host': True, 'web': True}
        self.assertTrue(team_tools.validate_action('Shell', {'command': 'python3 -m unittest'}, 'executor', config))
        self.assertFalse(team_tools.validate_action('read_file', {'path': '/project/a'}, 'reviewer', config))
        self.assertFalse(team_tools.validate_action('web_fetch', {'url': 'https://example.org'}, 'researcher', config))
        for role, flags, command in (('unknown', config, 'echo test'), ('executor', {'trusted_host': 'false'}, 'echo test'),
                                     ('executor', config, 'sudo id'), ('reviewer', config, 'echo test')):
            with self.subTest(role=role, command=command), self.assertRaises(PermissionError):
                team_tools.validate_action('Shell', {'command': command}, role, flags)


class AgentRegistryDispatchTests(unittest.IsolatedAsyncioTestCase):
    def test_textual_agent_does_not_receive_unrequested_mcp_schemas_via_email_aliases(self):
        from src.agent_loop import _filter_unrequested_mcp_catalog

        schemas = [schema('bash'), schema('mcp__email__list_email_accounts')]
        public = [{'id': 'bash'}, {'id': 'mcp__email__list_email_accounts'}]
        filtered_schemas, filtered_public = _filter_unrequested_mcp_catalog(
            schemas, public, 'Call the bash tool once and return the result.'
        )
        self.assertEqual([s['function']['name'] for s in filtered_schemas], ['bash'])
        self.assertEqual([item['id'] for item in filtered_public], ['bash'])

        # An actual email intent can still use explicitly discovered MCP tools.
        email_schemas, email_public = _filter_unrequested_mcp_catalog(
            schemas, public, 'List my email accounts.'
        )
        self.assertEqual(len(email_schemas), 2)
        self.assertEqual(len(email_public), 2)

    async def _finetune_run(self, *, engineering=True, native_call=False,
                            general=False, revoke=False, fallback=False):
        from src import agent_loop, tool_execution
        self.assertIs(agent_loop.execute_tool_block, tool_execution.execute_tool_block,
                      'Agent loop must use the canonical execution wrapper')
        sent = []
        disabled = []
        finetune = 'odysseus-qwen3-4b'
        command = {'action': 'list'}

        async def stream(candidates, messages, **kwargs):
            if fallback:
                request = await kwargs['candidate_request_factory'](
                    1, 'http://fallback.invalid/v1', finetune, {})
                messages, kwargs = request['messages'], request['kwargs']
                yield 'data: ' + json.dumps({'type': 'fallback', 'candidate_index': 1,
                    'answered_by': finetune}) + '\n\n'
            sent.append((copy.deepcopy(messages), copy.deepcopy(kwargs.get('tools') or [])))
            if revoke:
                disabled.append('manage_notes')
            payload = ({'type': 'tool_calls', 'calls': [{'id': 'finetune-call',
                        'name': 'manage_notes', 'arguments': json.dumps(command)}]}
                       if native_call else {'delta': '```manage_notes\n' + json.dumps(command) + '\n```'})
            yield 'data: ' + json.dumps(payload) + '\n\n'
            yield 'data: [DONE]\n\n'

        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1' if engineering else '0'}), \
             patch.object(agent_loop, 'get_setting', side_effect=lambda key, default=None: default), \
             patch.object(agent_loop, 'get_mcp_manager', return_value=None), \
             patch.object(agent_loop, 'blocked_tools_for_owner', return_value=set()), \
             patch.object(agent_loop, 'estimate_tokens', return_value=10), \
             patch.object(agent_loop, '_agent_route_tool_mode', return_value=(True, False, False)), \
             patch.object(agent_loop, 'stream_llm_with_fallback', side_effect=stream), \
             patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}), \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: disabled if key == 'disabled_tools' else default), \
             patch('src.host_execution.enabled_for', return_value=False), \
             patch.object(tool_execution, '_execute_tool_block_impl', new_callable=AsyncMock,
                          return_value=('manage_notes: listed', {'output': 'No notes found.', 'exit_code': 0})) as dispatch:
            chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
                'http://fixture.invalid/v1', 'fixture-model' if fallback else finetune,
                [{'role': 'user', 'content': 'Explain the CAP theorem.' if general else 'List my notes.'}],
                owner='owner', relevant_tools={'manage_notes'}, context_length=65536,
                fallbacks=[('http://fallback.invalid/v1', finetune, {})] if fallback else None,
                max_rounds=1, _is_teacher_run=True)]
        return sent, ''.join(chunks), dispatch

    async def test_engineering_finetune_text_catalogue_executes_fences_and_native_calls(self):
        for native_call, fallback in ((False, False), (True, False), (False, True)):
            with self.subTest(native_call=native_call, fallback=fallback):
                sent, chunks, dispatch = await self._finetune_run(native_call=native_call, fallback=fallback)
                prompt = '\n'.join(m.get('content', '') for m in sent[0][0] if m['role'] == 'system')
                names = json.loads(re.search(r'<tool_catalogue>(.*?)</tool_catalogue>', prompt).group(1))
                self.assertIn('manage_notes', names)
                self.assertIn('```manage_notes', prompt)
                self.assertEqual(sent[0][1], [])
                self.assertEqual(dispatch.await_count, 1, chunks)
                self.assertEqual(dispatch.await_args.args[0].tool_type, 'manage_notes')
                self.assertEqual(json.loads(dispatch.await_args.args[0].content), {'action': 'list'})
                events = [json.loads(line[6:]) for line in chunks.splitlines()
                          if line.startswith('data: {')]
                metrics = next(event['data'] for event in events if event.get('type') == 'metrics')
                self.assertNotIn('```manage_notes', ''.join(metrics['round_texts']))
                if fallback:
                    inventories = [event['data'] for event in events
                                   if event.get('type') == 'tool_inventory']
                    self.assertGreaterEqual(len(inventories), 2)
                    self.assertNotEqual(inventories[0]['route_revision'],
                                        inventories[1]['route_revision'])
                    context_routes = [event['data'].get('route_revision') for event in events
                                      if event.get('type') == 'context_usage']
                    self.assertIn(inventories[1]['route_revision'], context_routes)

    async def test_engineering_finetune_fences_preserve_current_permission_and_no_tool_clamps(self):
        sent, chunks, dispatch = await self._finetune_run(revoke=True)
        dispatch.assert_not_awaited()
        self.assertIn('Tool is disabled by current policy', chunks)
        sent, _, dispatch = await self._finetune_run(general=True)
        dispatch.assert_not_awaited()
        prompt = '\n'.join(m.get('content', '') for m in sent[0][0] if m['role'] == 'system')
        self.assertEqual(json.loads(re.search(r'<tool_catalogue>(.*?)</tool_catalogue>', prompt).group(1)), [])

    async def test_finetune_feature_off_preserves_existing_native_only_fence_gate(self):
        sent, _, dispatch = await self._finetune_run(engineering=False)
        dispatch.assert_not_awaited()
        prompt = '\n'.join(m.get('content', '') for m in sent[0][0] if m['role'] == 'system')
        self.assertNotIn('<tool_catalogue>', prompt)
        self.assertEqual(sent[0][1], [])

    async def _catalogue_run(self, native, *, revoke=False, rounds=1):
        from src import agent_loop, tool_execution
        state = {'disabled': [] if revoke else ['Shell']}
        sent = []
        async def stream(candidates, messages, **kwargs):
            sent.append((copy.deepcopy(messages), copy.deepcopy(kwargs.get('tools') or [])))
            if revoke:
                state['disabled'] = ['Shell']
                yield 'data: ' + json.dumps({'delta': '```bash\necho registry\n```'}) + '\n\n'
            else:
                yield 'data: ' + json.dumps({'delta': 'Observed available tools'}) + '\n\n'
            yield 'data: [DONE]\n\n'
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1'}), \
             patch.object(agent_loop, 'get_setting', side_effect=lambda key, default=None: default), \
             patch.object(agent_loop, 'get_mcp_manager', return_value=None), \
             patch.object(agent_loop, 'blocked_tools_for_owner', return_value=set()), \
             patch.object(agent_loop, 'estimate_tokens', return_value=10), \
             patch.object(agent_loop, '_agent_route_tool_mode', return_value=(native, False, native)), \
             patch.object(agent_loop, 'stream_llm_with_fallback', side_effect=stream), \
             patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}), \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: state['disabled'] if key == 'disabled_tools' else default), \
             patch('src.host_execution.enabled_for', return_value=False), \
             patch.object(tool_execution, '_execute_tool_block_impl', new_callable=AsyncMock) as dispatch:
            chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
                'http://fixture.invalid/v1', 'fixture-model', [{'role': 'user', 'content': 'Inspect requested source'}],
                owner='owner', relevant_tools={'bash', 'read_file', 'grep'}, context_length=65536,
                max_rounds=rounds, _is_teacher_run=True)]
        dispatch.assert_not_awaited()
        return sent, ''.join(chunks)

    async def test_real_native_and_text_loops_present_the_same_fresh_common_catalogue_as_team(self):
        from src import team_tools
        observed = []
        for native in (True, False):
            sent, chunks = await self._catalogue_run(native)
            prompt = '\n'.join(m.get('content', '') for m in sent[0][0] if m['role'] == 'system')
            match = re.search(r'<tool_catalogue>(.*?)</tool_catalogue>', prompt)
            self.assertIsNotNone(match)
            names = set(json.loads(match.group(1)))
            self.assertNotIn('bash', names)
            self.assertNotIn('Shell', names)
            self.assertNotIn('```bash', prompt)
            self.assertTrue({'read_file', 'grep'} <= names)
            events = [json.loads(line[6:]) for line in chunks.splitlines()
                      if line.startswith('data: {')]
            inventory = next(event['data'] for event in events if event.get('type') == 'tool_inventory')
            self.assertRegex(inventory['revision'], r'^[0-9a-f]{64}$')
            self.assertEqual(set(inventory['tools']), names,
                             {'native': native, 'inventory': inventory,
                              'catalogue': sorted(names),
                              'sent_schemas': sorted(s['function']['name'] for s in sent[0][1])})
            context_revisions = {
                (event.get('data') or {}).get('tool_inventory_revision')
                for event in events if event.get('type') == 'context_usage'
            }
            self.assertIn(inventory['revision'], context_revisions)
            self.assertNotIn('function', json.dumps(inventory))
            if native:
                self.assertEqual({s['function']['name'] for s in sent[0][1]}, names)
            observed.append(names)
        self.assertEqual(observed[0], observed[1])
        team = team_tools.schemas('owner', 'executor', config={'trusted_host': True, 'disabled_tools': ['Shell']})
        common = {'bash', 'read_file', 'grep'}
        self.assertEqual(observed[0] & common, {s['function']['name'] for s in team} & common)

    async def test_textual_call_rechecks_revocation_before_effect(self):
        sent, chunks = await self._catalogue_run(False, revoke=True, rounds=2)
        first = '\n'.join(m.get('content', '') for m in sent[0][0] if m['role'] == 'system')
        self.assertIn('bash', json.loads(re.search(r'<tool_catalogue>(.*?)</tool_catalogue>', first).group(1)))
        self.assertIn('Tool is disabled by current policy', chunks)
        if len(sent) > 1:
            later = '\n'.join(m.get('content', '') for m in sent[1][0] if m['role'] == 'system')
            self.assertNotIn('bash', json.loads(re.search(r'<tool_catalogue>(.*?)</tool_catalogue>', later).group(1)))
            self.assertNotIn('```bash', later)

    async def test_shell_alias_uses_existing_canonical_security_gates(self):
        import src.agent_tools  # Resolve the legacy facade import cycle.
        from src import tool_execution
        with patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch('src.host_execution.enabled_for', return_value=False), \
             patch.object(tool_execution, '_call_mcp_tool', new_callable=AsyncMock) as dispatch:
            for disabled in ({'bash'}, {'Shell'}, {'shell'}):
                _, result = await tool_execution.execute_tool_block(
                    SimpleNamespace(tool_type='Shell', content='echo registry'), owner='owner',
                    disabled_tools=disabled, security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT)
                self.assertEqual(result['exit_code'], 1)
                self.assertIn('disabled', result['error'])
            dispatch.assert_not_awaited()

    async def test_optional_agent_registry_rechecks_revoked_policy_before_dispatch(self):
        import src.agent_tools
        from src import tool_execution
        registry = ToolRegistry.from_schemas([schema('bash')])
        state = {'enabled': True}
        def current():
            return ToolAccess(mode='agent', role='agent', config={}, adapters=frozenset({'agent'}),
                              allowed_tools=frozenset({'bash'}) if state['enabled'] else frozenset())
        self.assertTrue(registry.schemas(current()))
        state['enabled'] = False
        with patch.object(tool_execution, '_execute_tool_block_impl', new_callable=AsyncMock) as dispatch:
            _, result = await tool_execution.execute_tool_block(
                SimpleNamespace(tool_type='Shell', content='echo registry'), owner='owner',
                registry=registry, registry_access_provider=current,
                security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT)
        self.assertEqual(result['exit_code'], 1)
        self.assertTrue(result['blocked'])
        dispatch.assert_not_awaited()

    async def test_agent_loop_applies_registry_to_real_schema_and_dispatch_paths(self):
        from src import agent_loop, tool_execution
        state = {'disabled': []}
        sent = []
        async def stream(candidates, messages, **kwargs):
            sent.append(kwargs.get('tools') or [])
            # Revoke after the model received its schema, before the emitted
            # call reaches the real execute_tool_block wrapper.
            state['disabled'] = ['Shell']
            yield 'data: ' + json.dumps({'type': 'tool_calls', 'calls': [
                {'id': 'registry-call', 'name': 'bash',
                 'arguments': '{"command":"echo registry"}'}]}) + '\n\n'
            yield 'data: [DONE]\n\n'
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1'}), \
             patch.object(agent_loop, 'get_setting', side_effect=lambda key, default=None: default), \
             patch.object(agent_loop, 'get_mcp_manager', return_value=None), \
             patch.object(agent_loop, 'blocked_tools_for_owner', return_value=set()), \
             patch.object(agent_loop, 'estimate_tokens', return_value=10), \
             patch.object(agent_loop, '_agent_route_tool_mode', return_value=(True, False, True)), \
             patch.object(agent_loop, 'stream_llm_with_fallback', side_effect=stream), \
             patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}), \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: state['disabled'] if key == 'disabled_tools' else default), \
             patch('src.host_execution.enabled_for', return_value=False), \
             patch.object(tool_execution, '_execute_tool_block_impl', new_callable=AsyncMock) as dispatch:
            chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
                'http://fixture.invalid/v1', 'fixture-model', [{'role': 'user', 'content': 'Run the requested build command'}],
                owner='owner', relevant_tools={'bash'}, context_length=65536, max_rounds=1, _is_teacher_run=True)]
        self.assertTrue(sent)
        self.assertIn('bash', {s['function']['name'] for s in sent[0]})
        self.assertIn('Tool is disabled by current policy', ''.join(chunks))
        dispatch.assert_not_awaited()

    async def test_agent_loop_feature_off_preserves_legacy_schema_path(self):
        from src import agent_loop, tool_execution
        sent = []
        async def stream(candidates, messages, **kwargs):
            sent.append(kwargs.get('tools') or [])
            yield 'data: ' + json.dumps({'delta': 'Fixture final answer'}) + '\n\n'
            yield 'data: [DONE]\n\n'
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '0'}), \
             patch.object(agent_loop, 'get_setting', side_effect=lambda key, default=None: default), \
             patch.object(agent_loop, 'get_mcp_manager', return_value=None), \
             patch.object(agent_loop, 'blocked_tools_for_owner', return_value=set()), \
             patch.object(agent_loop, 'estimate_tokens', return_value=10), \
             patch.object(agent_loop, '_agent_route_tool_mode', return_value=(True, False, True)), \
             patch.object(agent_loop, 'stream_llm_with_fallback', side_effect=stream), \
             patch.object(tool_execution, 'agent_registry_inventory') as inventory, \
             patch('src.host_execution.enabled_for', return_value=False):
            _ = [chunk async for chunk in agent_loop.stream_agent_loop(
                'http://fixture.invalid/v1', 'fixture-model', [{'role': 'user', 'content': 'Run the requested build command'}],
                owner='owner', relevant_tools={'bash'}, context_length=65536, max_rounds=1, _is_teacher_run=True)]
        self.assertTrue(sent)
        self.assertIn('bash', {s['function']['name'] for s in sent[0]})
        inventory.assert_not_called()

    async def test_live_owner_host_and_feature_revocations_deny_existing_inventory(self):
        from src import tool_execution
        registry = ToolRegistry.from_schemas([schema('bash'), schema('read_file')])
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1'}), \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: default), \
             patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}) as privileges, \
             patch('src.host_execution.enabled_for', return_value=True) as host:
            def current():
                return tool_execution.current_agent_registry_access(registry, owner='owner',
                    disabled_tools=set(), tool_policy=None, mcp_manager=None, host_bound=True)
            self.assertEqual(registry.require_current('bash', current).id, 'bash')
            host.return_value = False
            with self.assertRaises(PermissionError):
                registry.require_current('bash', current)
            host.return_value = True
            privileges.return_value = {'can_use_agent': False}
            with self.assertRaises(PermissionError):
                registry.require_current('read_file', current)
            privileges.return_value = {'can_use_agent': True}
            with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '0'}), self.assertRaises(PermissionError):
                registry.require_current('read_file', current)

    async def test_mcp_revoke_is_checked_from_real_sqlite_before_each_call(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.database import McpServer
        from src import tool_execution
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        McpServer.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(McpServer(id='email', name='Fixture email', is_enabled=True, disabled_tools='[]'))
            db.commit()
        manager = SimpleNamespace(
            get_all_tools=lambda: [{'server_id': 'email', 'name': 'read_email', 'description': 'Fixture', 'input_schema': {}}],
            is_builtin=lambda server: server == 'email',
            get_server_status=lambda server: {'status': 'connected'})
        registry = tool_execution.agent_registry_inventory([schema('read_email')], manager)
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1'}), \
             patch('core.database.SessionLocal', sessions), \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: default), \
             patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}):
            def current():
                return tool_execution.current_agent_registry_access(registry, owner='owner',
                    disabled_tools=set(), tool_policy=None, mcp_manager=manager)
            self.assertEqual(registry.require_current('mcp__email__read_email', current).id,
                             'mcp__email__read_email')
            with sessions() as db:
                row = db.get(McpServer, 'email')
                row.disabled_tools = '["read_email"]'
                db.commit()
            for name in ('read_email', 'mcp__email__read_email'):
                with self.subTest(name=name), self.assertRaises(PermissionError):
                    registry.require_current(name, current)
            with sessions() as db:
                row = db.get(McpServer, 'email')
                row.disabled_tools = '[]'
                row.is_enabled = False
                db.commit()
            with self.assertRaises(PermissionError):
                registry.require_current('mcp__email__read_email', current)

    async def test_browser_permission_is_checked_before_each_call(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.database import McpServer
        from src import tool_execution
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        McpServer.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        manager = SimpleNamespace(
            get_all_tools=lambda: [
                {'server_id': 'builtin_browser', 'name': name, 'description': 'Fixture',
                 'input_schema': {}, 'is_disabled': name == 'browser_run_code_unsafe'}
                for name in ('browser_snapshot', 'browser_navigate', 'browser_click',
                             'browser_run_code_unsafe')],
            is_builtin=lambda server: server == 'builtin_browser',
            get_server_status=lambda server: {'status': 'connected'})
        registry = tool_execution.agent_registry_inventory([], manager)
        privileges = {'can_use_agent': True, 'can_use_browser': False}
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1'}), \
             patch('core.database.SessionLocal', sessions), \
             patch('src.settings.get_setting', side_effect=lambda key, default=None: default), \
             patch.object(tool_execution, '_owner_is_admin', return_value=True), \
             patch.object(tool_execution, '_current_agent_privileges', side_effect=lambda owner: privileges):
            def current():
                return tool_execution.current_agent_registry_access(registry, owner='owner',
                    disabled_tools=set(), tool_policy=None, mcp_manager=manager)
            for name in ('browser_snapshot', 'browser_navigate', 'browser_click',
                         'browser_run_code_unsafe'):
                with self.subTest(name=name), self.assertRaises(PermissionError):
                    registry.require_current(f'mcp__builtin_browser__{name}', current)
            privileges['can_use_browser'] = True
            for name in ('browser_snapshot', 'browser_navigate', 'browser_click'):
                self.assertEqual(registry.require_current(f'mcp__builtin_browser__{name}', current).id,
                                 f'mcp__builtin_browser__{name}')
            with self.assertRaises(PermissionError):
                registry.require_current('mcp__builtin_browser__browser_run_code_unsafe', current)
