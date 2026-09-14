"""Bounded reconnects never replay sent/partially received model requests."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from src import team_model


class ModelReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_failure_recovers_with_same_route(self):
        expected = {'message': {'content': 'ok'}}
        transport = AsyncMock(side_effect=[httpx.ConnectError('offline'), expected])
        route = {'model': 'approved-only'}
        with patch.object(team_model, '_complete_once', transport), patch.object(team_model.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(await team_model.complete(route, [], []), expected)
        self.assertEqual(transport.call_count, 2)
        self.assertTrue(all(call.args[0] is route for call in transport.call_args_list))

    async def test_retries_are_bounded(self):
        transport = AsyncMock(side_effect=httpx.ConnectTimeout('offline'))
        with patch.object(team_model, '_complete_once', transport), patch.object(team_model.asyncio, 'sleep', AsyncMock()):
            with self.assertRaises(httpx.ConnectTimeout):
                await team_model.complete({}, [], [])
        self.assertEqual(transport.call_count, 3)

    async def test_sent_or_partial_requests_are_not_replayed(self):
        for error in (httpx.ReadTimeout('sent'), httpx.WriteError('partial'), RuntimeError('Model HTTP 503')):
            transport = AsyncMock(side_effect=error)
            with patch.object(team_model, '_complete_once', transport):
                with self.assertRaises(type(error)):
                    await team_model.complete({}, [], [])
            self.assertEqual(transport.call_count, 1)
