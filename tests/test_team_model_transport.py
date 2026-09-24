"""Real loopback HTTP/SSE transport and SQLite costs; no paid provider is used.

The fixture is deliberately flagged external by the server-owned resolver so
the real runtime reservation path is exercised without any external transfer.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import team_model
from src.team_runtime import TeamRuntime
from src.team_store import TeamStore, BudgetError


def sse(value):
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ('data: ' + body + '\n\n').encode()


class TeamModelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # Do not let developer proxy settings route a loopback fixture remotely.
        environment = patch.dict(os.environ, {'HTTP_PROXY': '', 'HTTPS_PROXY': '',
            'ALL_PROXY': '', 'NO_PROXY': '127.0.0.1,localhost'})
        environment.start()
        self.addCleanup(environment.stop)
        self.requests, self.handlers = [], set()
        self.status = 200
        self.response = self.success_stream()
        self.hold = None
        self.received = asyncio.Event()
        self.server = await asyncio.start_server(self.serve, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.route = {'endpoint_id': 'external-fixture', 'model': 'fixture-coder',
            'url': f'http://127.0.0.1:{port}/v1/chat/completions',
            'headers': {'Content-Type': 'application/json'},
            'local': False, 'resource_group': 'external-fixture'}
        self.store = TeamStore(Path(self.directory.name) / 'teams.db')
        self.runtime = TeamRuntime(self.store, complete=team_model.complete, host=self.unexpected_host)
        resolver = patch('src.team_config.resolve', side_effect=lambda *_: self.route)
        resolver.start()
        self.addCleanup(resolver.stop)
        self.messages = [{'role': 'user', 'content': 'Inspect fixture code'}]

    async def asyncTearDown(self):
        await self.runtime.close()
        if self.hold:
            self.hold.set()
        self.server.close()
        await self.server.wait_closed()
        for handler in list(self.handlers):
            handler.cancel()
        await asyncio.gather(*list(self.handlers), return_exceptions=True)

    async def unexpected_host(self, *args, **kwargs):
        raise AssertionError('Transport tests must never execute host tools')

    async def serve(self, reader, writer):
        handler = asyncio.current_task()
        self.handlers.add(handler)
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            lines = headers.decode('latin1').split('\r\n')
            fields = dict(line.split(': ', 1) for line in lines[1:] if ': ' in line)
            length = next(int(value) for key, value in fields.items() if key.lower() == 'content-length')
            body = json.loads(await reader.readexactly(length))
            self.requests.append({'line': lines[0], 'body': body})
            self.received.set()
            if self.hold:
                await self.hold.wait()
            writer.write(f'HTTP/1.1 {self.status} Fixture\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'.encode())
            await writer.drain()
            for part in self.response:
                writer.write(part)
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

    @staticmethod
    def success_stream(*, usage=True):
        chunks = [sse({'choices': [{'delta': {'content': 'Verified fixture'}}]})]
        if usage:
            chunks.append(sse({'choices': [], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}}))
        return chunks + [sse('[DONE]')]

    def paid_worker(self, *, task=None, budget=10_000, approve=True, output_only=False,
                    kind='worker', data_scope='assigned_context'):
        if task is None:
            task = self.store.create_task('owner', 'Transport fixture', budget_microusd=budget,
                metadata={'config': {'external': True, 'trusted_host': False},
                          'external_data_scopes': {self.route['endpoint_id']: data_scope} if data_scope else {}})
        worker = self.store.add_worker('owner', task['id'], 'Fixture', profile={
            'endpoint_id': self.route['endpoint_id'], 'model': self.route['model'], 'kind': kind})
        claim = self.store.claim_worker('owner', task['id'], worker_id=worker['id'])
        if approve:
            self.store.approve_endpoint('owner', task['id'], self.route['endpoint_id'], budget,
                                       0 if output_only else 1_000_000, 1_000_000)
        return task, claim

    async def call_runtime(self, task, claim):
        return await self.runtime.model_call('owner', task['id'], claim, claim['lease_token'], self.messages, [])

    async def test_streamed_content_native_tool_fragments_and_usage_reach_caller(self):
        self.response = [
            b': keepalive\n\n',
            sse({'choices': [{'delta': {'content': 'Привет ', 'tool_calls': [
                {'index': 1, 'id': 'second', 'function': {'name': 'read_', 'arguments': '{"path":"'}},
                {'index': 0, 'id': 'first', 'function': {'name': 'grep', 'arguments': '{"pattern":"TODO"}'}}]}}]}),
            sse({'choices': [{'delta': {'content': 'мир', 'tool_calls': [
                {'index': 1, 'function': {'name': 'file', 'arguments': '/project/a.py"}'}}]}}]}),
            sse({'choices': [], 'usage': {'prompt_tokens': 123, 'completion_tokens': 45}}),
            sse('[DONE]')]
        deltas = []
        async def on_delta(text):
            deltas.append(text)
        tools = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}]
        result = await team_model.complete(self.route, self.messages, tools, max_tokens=128, on_delta=on_delta)
        self.assertEqual(result['message']['content'], 'Привет мир')
        self.assertEqual(''.join(deltas), 'Привет мир')
        calls = result['message']['tool_calls']
        self.assertEqual([call['id'] for call in calls], ['first', 'second'])
        self.assertEqual(calls[1]['function']['name'], 'read_file')
        self.assertEqual(json.loads(calls[1]['function']['arguments']), {'path': '/project/a.py'})
        self.assertEqual(result['usage'], {'prompt_tokens': 123, 'completion_tokens': 45})
        self.assertGreater(result['duration'], 0)
        self.assertIsNotNone(result['ttft'])
        request = self.requests[0]
        self.assertEqual(request['line'], 'POST /v1/chat/completions HTTP/1.1')
        self.assertEqual(request['body']['max_tokens'], 128)
        self.assertIs(request['body']['stream'], True)
        self.assertEqual(request['body']['tools'], tools)
        self.assertEqual(request['body']['stream_options'], {'include_usage': True})

    async def test_qwen_text_tool_markup_is_adapted_only_for_advertised_tools(self):
        self.response = [
            sse({'choices': [{'delta': {'content': '<tool_call><function=python>'
                '<parameter=code>print(99991 * 317)</parameter></function></tool_call>'}}]}),
            sse('[DONE]'),
        ]
        tools = [{'type': 'function', 'function': {'name': 'python', 'parameters': {
            'type': 'object', 'properties': {'code': {'type': 'string'}}}}}]

        result = await team_model.complete(self.route, self.messages, tools)

        self.assertEqual(len(result['message']['tool_calls']), 1)
        call = result['message']['tool_calls'][0]
        self.assertEqual(call['function']['name'], 'python')
        self.assertEqual(json.loads(call['function']['arguments']), {'code': 'print(99991 * 317)'})

    async def test_qwen_text_markup_never_calls_a_tool_not_advertised(self):
        self.response = [
            sse({'choices': [{'delta': {'content': '<tool_call><function=python>'
                '<parameter=code>print(1)</parameter></function></tool_call>'}}]}),
            sse('[DONE]'),
        ]
        tools = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {
            'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
        result = await team_model.complete(self.route, self.messages, tools)
        self.assertNotIn('tool_calls', result['message'])
        self.assertIn('<tool_call>', result['message']['content'])

    async def test_truncated_stream_never_becomes_success(self):
        self.response = [sse({'choices': [{'delta': {'content': 'Only partial'}}]})]
        with self.assertRaisesRegex(RuntimeError, 'disconnected before completion'):
            await team_model.complete(self.route, self.messages, [])
        self.assertEqual(len(self.requests), 1)

    async def test_http_error_does_not_copy_sensitive_response_body(self):
        self.status = 503
        marker = 'fixture-private-response-not-for-logs'
        self.response = [marker.encode()]
        with self.assertRaises(RuntimeError) as caught:
            await team_model.complete(self.route, self.messages, [])
        self.assertEqual(str(caught.exception), 'Model HTTP 503')
        self.assertNotIn(marker, repr(caught.exception))

    async def test_sse_error_does_not_copy_provider_error_text(self):
        marker = 'fixture-provider-diagnostic-private'
        self.response = [sse({'error': {'message': marker}}), sse('[DONE]')]
        with self.assertRaises(RuntimeError) as caught:
            await team_model.complete(self.route, self.messages, [])
        self.assertEqual(str(caught.exception), 'Model reported an error')
        self.assertNotIn(marker, repr(caught.exception))

    async def test_stream_size_limit_includes_non_data_lines(self):
        self.response = [b': ' + b'x' * (3 * 1024 * 1024) + b'\n\n', sse('[DONE]')]
        with self.assertRaisesRegex(RuntimeError, 'bounded stream size'):
            await team_model.complete(self.route, self.messages, [])

    async def test_missing_native_tool_id_is_rejected_before_dispatch(self):
        self.response = [sse({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': 'read_file', 'arguments': '{}'}}]}}]}), sse('[DONE]')]
        with self.assertRaisesRegex(RuntimeError, 'Incomplete native tool call'):
            await team_model.complete(self.route, self.messages, [])

    async def test_unapproved_external_fixture_receives_zero_http_calls(self):
        task, claim = self.paid_worker(approve=False)
        with self.assertRaises(BudgetError):
            await self.call_runtime(task, claim)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.list_reservations('owner', task['id']), [])

    async def test_goal_only_consent_cannot_send_worker_context(self):
        task, claim = self.paid_worker(data_scope='goal_only')
        self.messages = [{'role': 'user', 'content': 'Private assigned file excerpt'}]
        with self.assertRaisesRegex(PermissionError, 'consent for the assigned context'):
            await self.call_runtime(task, claim)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.list_reservations('owner', task['id']), [])

    async def test_goal_only_consent_allows_planner_request(self):
        task, claim = self.paid_worker(kind='planner', data_scope='goal_only')
        self.messages = [{'role': 'user', 'content': 'Plan the explicitly approved goal'}]
        result = await self.call_runtime(task, claim)
        self.assertEqual(result['content'], 'Verified fixture')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['body']['messages'], self.messages)
        self.assertEqual(self.store.list_reservations('owner', task['id'])[0]['status'], 'settled')

    async def test_money_approval_alone_never_authorizes_data_transfer(self):
        task, claim = self.paid_worker(data_scope=None)
        with self.assertRaises(PermissionError):
            await self.call_runtime(task, claim)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.list_reservations('owner', task['id']), [])

    async def test_approved_real_http_usage_settles_and_refunds_reservation(self):
        task, claim = self.paid_worker()
        result = await self.call_runtime(task, claim)
        self.assertEqual(result['content'], 'Verified fixture')
        self.assertEqual(len(self.requests), 1)
        status = self.store.budget_status('owner', task['id'])
        self.assertEqual(status['spent_microusd'], 18)
        self.assertEqual(status['reserved_microusd'], 0)
        self.assertEqual(status['remaining_microusd'], 9982)
        self.assertEqual(status['reservations'][0]['status'], 'settled')
        events = self.store.events('owner', task['id'])
        started = next(event for event in events if event['type'] == 'worker_message_started')
        completed = next(event for event in events if event['type'] == 'worker_message_completed')
        delta = next(event for event in events if event['type'] == 'worker_delta')
        self.assertEqual(started['payload']['message_id'], delta['payload']['message_id'])
        self.assertEqual(started['payload']['message_id'], completed['payload']['message_id'])
        self.assertEqual(completed['payload']['content'], 'Verified fixture')

    async def test_concurrent_cloud_calls_cannot_exceed_shared_task_reservation(self):
        task, first = self.paid_worker(budget=4096, output_only=True)
        _, second = self.paid_worker(task=task, budget=4096, approve=False)
        self.hold = asyncio.Event()
        pending = asyncio.create_task(self.call_runtime(task, first))
        try:
            await asyncio.wait_for(self.received.wait(), 2)
            with self.assertRaises(BudgetError):
                await self.call_runtime(task, second)
            self.assertEqual(len(self.requests), 1)
            before = self.store.budget_status('owner', task['id'])
            self.assertEqual(before['reserved_microusd'], 4096)
            self.assertEqual(before['spent_microusd'], 0)
            self.hold.set()
            await pending
            after = self.store.budget_status('owner', task['id'])
            self.assertEqual(after['spent_microusd'], 7)
            self.assertEqual(after['reserved_microusd'], 0)
        finally:
            self.hold.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_truncated_cloud_response_charges_ceiling_and_blocks_overspend(self):
        task, claim = self.paid_worker(budget=4096, output_only=True)
        self.response = [sse({'choices': [{'delta': {'content': 'partial'}}]})]
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            await self.call_runtime(task, claim)
        reservation = self.store.list_reservations('owner', task['id'])[0]
        self.assertEqual(reservation['status'], 'settled_unknown')
        self.assertEqual(reservation['charged_microusd'], 4096)
        self.store.settle_unknown('owner', task['id'], reservation['id'])
        with self.assertRaises(BudgetError):
            await self.call_runtime(task, claim)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.store.budget_status('owner', task['id'])['spent_microusd'], 4096)

    async def test_cancel_after_http_dispatch_keeps_sent_cost_reserved_ceiling(self):
        task, claim = self.paid_worker(budget=4096, output_only=True)
        self.hold = asyncio.Event()
        pending = asyncio.create_task(self.call_runtime(task, claim))
        try:
            await asyncio.wait_for(self.received.wait(), 2)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            status = self.store.budget_status('owner', task['id'])
            self.assertEqual(status['spent_microusd'], 4096)
            self.assertEqual(status['reserved_microusd'], 0)
            self.assertEqual(status['reservations'][0]['status'], 'settled_unknown')
        finally:
            self.hold.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
