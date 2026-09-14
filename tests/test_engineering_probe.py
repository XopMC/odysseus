"""Capability probes exercise real loopback HTTP/SSE, never a model or shell."""
import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

from src import engineering_probe
from src.team_store import Conflict
from src.team_config import resolve as configured_resolve


def sse(value):
    return ('data: ' + (value if isinstance(value, str) else json.dumps(value)) + '\n\n').encode()


class EngineeringProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests, self.handlers = [], set()
        self.mode = 'success'
        self.after_first = None
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.route = {'endpoint_id': 'fixture-local', 'model': 'exact-model:quant',
            'url': f'http://127.0.0.1:{port}/v1/chat/completions',
            'headers': {'Authorization': 'Bearer PRIVATE_AUTH_SENTINEL', 'Content-Type': 'application/json'},
            'local': True, 'resource_group': 'probe-fixture'}
        for replacement in (
            patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1', 'HTTP_PROXY': '',
                'HTTPS_PROXY': '', 'ALL_PROXY': '', 'NO_PROXY': '127.0.0.1,localhost'}),
            patch('src.team_config.resolve', side_effect=lambda owner, endpoint, model: copy.deepcopy(self.route)),
            patch('src.engineering_probe._owner_allowed', side_effect=lambda owner: owner == 'alice'),
        ):
            replacement.start(); self.addCleanup(replacement.stop)

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        for handler in list(self.handlers):
            handler.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)

    async def serve(self, reader, writer):
        handler = asyncio.current_task()
        self.handlers.add(handler)
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            fields = [line.split(':', 1) for line in headers.decode().split('\r\n') if ':' in line]
            length = next(int(value) for key, value in fields if key.lower() == 'content-length')
            body = json.loads(await reader.readexactly(length))
            self.requests.append(body)
            if self.mode == 'timeout':
                await asyncio.sleep(10)
                return
            status = 503 if self.mode == 'http_error' else 200
            writer.write(f'HTTP/1.1 {status} Fixture\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'.encode())
            if status != 200:
                writer.write(b'PRIVATE_ERROR_SENTINEL')
            elif len(self.requests) == 1:
                if self.mode == 'text_only':
                    writer.write(sse({'choices': [{'delta': {'content': 'No native tool support'}}]}))
                else:
                    tool = body['tools'][0]['function']
                    # The challenge is part of the synthetic user JSON only.
                    challenge = json.loads(body['messages'][-1]['content'])['challenge']
                    name = 'bash' if self.mode == 'wrong_tool' else tool['name']
                    arguments = json.dumps({'challenge': challenge if self.mode != 'wrong_args' else 'wrong'})
                    writer.write(sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'probe-call',
                        'function': {'name': name, 'arguments': arguments[:8]}}]}}]}))
                    writer.write(sse({'choices': [{'delta': {'tool_calls': [{'index': 0,
                        'function': {'arguments': arguments[8:]}}]}}]}))
                if self.after_first:
                    self.after_first()
            else:
                output = json.loads(body['messages'][-1]['content'])['result']
                output = 'wrong' if self.mode == 'wrong_result' else output
                writer.write(sse({'choices': [{'delta': {'content': output[:7]}}]}))
                await writer.drain()
                writer.write(sse({'choices': [{'delta': {'content': output[7:]}}]}))
            if status == 200:
                if self.mode != 'no_usage':
                    usage = {'prompt_tokens': 18, 'completion_tokens': 9}
                    if self.mode == 'invalid_usage':
                        usage['prompt_tokens'] = True
                    writer.write(sse({'choices': [], 'usage': usage}))
                if self.mode != 'truncated':
                    writer.write(sse('[DONE]'))
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.handlers.discard(handler)

    def describe(self):
        return engineering_probe.describe('alice', self.route['endpoint_id'], self.route['model'])

    async def run_probe(self, **kwargs):
        scope = self.describe()['scope']
        return await engineering_probe.probe('alice', self.route['endpoint_id'], self.route['model'],
            confirmation=True, expected_config_digest=scope['config_digest'], **kwargs)

    async def test_real_native_tool_roundtrip_stream_usage_and_exact_model(self):
        result = await self.run_probe()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['capabilities'], {
            'streaming': True, 'native_tools': True, 'tool_roundtrip': True, 'usage': True})
        self.assertEqual(result['measurements']['requests'], 2)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(body['model'] == 'exact-model:quant' and body['stream'] is True for body in self.requests))
        self.assertTrue(all(body['max_tokens'] == 256 for body in self.requests))
        self.assertEqual(self.requests[1]['messages'][-1]['role'], 'tool')
        self.assertEqual(self.requests[1]['messages'][-1]['tool_call_id'], 'probe-call')
        self.assertGreater(result['measurements']['content_callbacks'], 0)
        self.assertNotIn('PRIVATE_AUTH_SENTINEL', json.dumps(result))

    async def test_external_is_explicitly_unsupported_and_never_contacts_provider(self):
        self.route['local'] = False
        metadata = self.describe()
        self.assertFalse(metadata['supported'])
        result = await self.run_probe()
        self.assertEqual(result['status'], 'unsupported')
        self.assertIn('budget', result['reason'].lower())
        self.assertEqual(self.requests, [])

    async def test_feature_owner_and_confirmation_gates_send_nothing(self):
        scope = self.describe()['scope']
        for owner, confirmation in [('bob', True), ('alice', False), ('alice', 1)]:
            with self.subTest(owner=owner, confirmation=confirmation), self.assertRaises(PermissionError):
                await engineering_probe.probe(owner, self.route['endpoint_id'], self.route['model'],
                    confirmation=confirmation, expected_config_digest=scope['config_digest'])
        with patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '0'}), self.assertRaises(PermissionError):
            await self.run_probe()
        self.assertEqual(self.requests, [])

    async def test_changed_endpoint_config_requires_fresh_confirmation(self):
        original = self.describe()['scope']['config_digest']
        self.route['headers']['Authorization'] = 'Bearer CHANGED_PRIVATE_AUTH'
        self.assertNotEqual(self.describe()['scope']['config_digest'], original)
        with self.assertRaises(Conflict):
            await engineering_probe.probe('alice', self.route['endpoint_id'], self.route['model'],
                confirmation=True, expected_config_digest=original)
        self.assertEqual(self.requests, [])

    async def test_change_or_revoke_between_rounds_never_sends_second_request(self):
        self.after_first = lambda: self.route.update(local=False)
        result = await self.run_probe()
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(result['capabilities']['tool_roundtrip'])

    async def test_owner_revocation_during_first_response_prevents_second_request(self):
        with patch('src.engineering_probe._owner_allowed', return_value=True) as allowed:
            self.after_first = lambda: setattr(allowed, 'return_value', False)
            result = await self.run_probe()
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(len(self.requests), 1)

    async def test_endpoint_disable_during_response_is_stale_not_a_network_failure(self):
        def current(*args):
            if self.requests:
                raise ValueError('Model is unavailable or not owned by this account')
            return copy.deepcopy(self.route)
        with patch('src.team_config.resolve', side_effect=current):
            result = await self.run_probe()
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(len(self.requests), 1)

    async def test_real_endpoint_store_ownership_exact_model_and_disable_are_enforced(self):
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
        except ImportError:
            self.skipTest('SQLAlchemy application runtime required')
        from core.database import ModelEndpoint
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        ModelEndpoint.__table__.create(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(ModelEndpoint(id=self.route['endpoint_id'], name='Private fixture', owner='alice',
                base_url=self.route['url'], endpoint_kind='local', is_enabled=True,
                cached_models=json.dumps([self.route['model']])))
            db.commit()
        with patch('core.database.SessionLocal', sessions), \
             patch('src.team_config.resolve', side_effect=configured_resolve), \
             patch('src.engineering_probe._owner_allowed', return_value=True):
            description = self.describe()
            with self.assertRaises(ValueError):
                engineering_probe.describe('bob', self.route['endpoint_id'], self.route['model'])
            with self.assertRaises(ValueError):
                engineering_probe.describe('alice', self.route['endpoint_id'], 'guessed-model')
            with sessions() as db:
                db.get(ModelEndpoint, self.route['endpoint_id']).endpoint_kind = 'proxy'
                db.commit()
            # A configured API proxy stays external even on a loopback IP.
            external = self.describe()
            self.assertFalse(external['supported'])
            denied = await engineering_probe.probe('alice', self.route['endpoint_id'], self.route['model'],
                confirmation=True, expected_config_digest=external['scope']['config_digest'])
            self.assertEqual(denied['status'], 'unsupported')
            with sessions() as db:
                db.get(ModelEndpoint, self.route['endpoint_id']).is_enabled = False
                db.commit()
            with self.assertRaises(ValueError):
                await engineering_probe.probe('alice', self.route['endpoint_id'], self.route['model'],
                    confirmation=True, expected_config_digest=description['scope']['config_digest'])
        self.assertEqual(self.requests, [])

    async def test_probe_schema_version_and_limits_are_part_of_config_identity(self):
        original = self.describe()['scope']
        with patch('src.engineering_probe.PROBE_VERSION', 'future-probe-version'):
            self.assertNotEqual(self.describe()['scope']['config_digest'], original['config_digest'])
        with patch.dict(engineering_probe._TOOL['function'], {'description': 'Changed probe schema'}):
            changed = self.describe()['scope']
            self.assertNotEqual(changed['schema_digest'], original['schema_digest'])
            self.assertNotEqual(changed['config_digest'], original['config_digest'])

    async def test_text_only_model_reports_observed_limits_not_fake_native_success(self):
        self.mode = 'text_only'
        result = await self.run_probe()
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['capabilities']['native_tools'])
        self.assertIsNone(result['capabilities']['tool_roundtrip'])
        self.assertTrue(result['capabilities']['streaming'])
        self.assertEqual(len(self.requests), 1)

    async def test_unknown_requested_tool_is_never_executed(self):
        self.mode = 'wrong_tool'
        result = await self.run_probe()
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['capabilities']['native_tools'])
        self.assertEqual(len(self.requests), 1)

    async def test_wrong_native_arguments_fail_the_synthetic_contract(self):
        self.mode = 'wrong_args'
        result = await self.run_probe()
        self.assertFalse(result['capabilities']['native_tools'])
        self.assertEqual(len(self.requests), 1)

    async def test_model_must_read_tool_result_not_merely_emit_a_tool_call(self):
        self.mode = 'wrong_result'
        result = await self.run_probe()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['capabilities']['native_tools'])
        self.assertFalse(result['capabilities']['tool_roundtrip'])

    async def test_usage_must_be_present_and_well_typed(self):
        self.mode = 'invalid_usage'
        result = await self.run_probe()
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['capabilities']['usage'])

    async def test_truncated_stream_and_http_error_are_not_capability_passes(self):
        self.mode = 'truncated'
        result = await self.run_probe()
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['capabilities']['streaming'])
        self.assertNotIn('PRIVATE_ERROR_SENTINEL', json.dumps(result))

    async def test_http_error_body_is_never_exposed(self):
        self.mode = 'http_error'
        result = await self.run_probe()
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('PRIVATE_ERROR_SENTINEL', json.dumps(result))

    async def test_probe_deadline_is_bounded_without_background_continuation(self):
        self.mode = 'timeout'
        with patch('src.engineering_probe.ENGINEERING_PROBE_TIMEOUT_SECONDS', .05):
            result = await self.run_probe()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.requests), 1)
        self.assertIn('deadline', result['reason'])

    async def test_probe_uses_existing_physical_backend_slot(self):
        from src.team_model import resource_slot
        async with resource_slot(self.route['resource_group']):
            pending = asyncio.create_task(self.run_probe())
            await asyncio.sleep(.03)
            self.assertEqual(self.requests, [])
        result = await pending
        self.assertEqual(result['status'], 'passed')

    async def test_cancelled_operation_is_checked_after_waiting_for_backend_slot(self):
        from src.team_model import resource_slot
        active = True
        async def check_active():
            return active
        async with resource_slot(self.route['resource_group']):
            pending = asyncio.create_task(self.run_probe(check_active=check_active))
            await asyncio.sleep(.03)
            self.assertEqual(self.requests, [])
            active = False
        result = await pending
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(self.requests, [])

    async def test_operation_fence_is_rechecked_after_response(self):
        active = True
        def check_active():
            if not active:
                raise PermissionError('Durable operation cancelled')
        def cancel():
            nonlocal active
            active = False
        self.after_first = cancel
        result = await self.run_probe(check_active=check_active)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(len(self.requests), 1)
