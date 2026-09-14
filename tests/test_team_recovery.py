"""Real SQLite recovery: never replay a completed effect at checkpoint gaps."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.team_runtime import TeamRuntime
from src.team_store import TeamStore


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = [100.]
        self.store = TeamStore(Path(self.directory.name) / 'teams.db', clock=lambda: self.now[0])
        self.task = self.store.create_task('owner', 'Test', metadata={
            'goal': 'Write once', 'project_path': '/tmp',
            'config': {'trusted_host': True, 'web': False}})
        self.worker = self.store.add_worker('owner', self.task['id'], 'Writer', profile={
            'role': 'executor', 'kind': 'worker', 'cwd': '/tmp', 'endpoint_id': 'local', 'model': 'mock'})
        self.claim = self.store.claim_worker('owner', self.task['id'])
        self.runtime = TeamRuntime(self.store, host=self.host)
        self.host_calls = []

    async def asyncTearDown(self):
        await self.runtime.close()
        self.directory.cleanup()

    async def host(self, *args, **kwargs):
        self.host_calls.append(args)
        raise AssertionError('Completed effect must not run again')

    def checkpoint(self):
        messages = [{'role': 'system', 'content': 'Test'}, {'role': 'user', 'content': 'Write once'},
                    {'role': 'assistant', 'content': '', 'tool_calls': [
                        {'id': 'once', 'type': 'function', 'function': {'name': 'write_file',
                         'arguments': '{"path":"/tmp/example","content":"once"}'}}]}]
        self.store.save_checkpoint('owner', self.task['id'], self.worker['id'], self.claim['lease_token'],
            {'messages': messages, 'round': 0, 'cwd': '/tmp', 'successful_tools': 0, 'failures': {}, 'compactions': 0})
        return self.store.record_tool_intent('owner', self.task['id'], self.worker['id'], self.claim['lease_token'],
            'write_file', {'path': '/tmp/example', 'content': 'once'}, effectful=True, idempotency_key='once')

    async def test_result_checkpoint_gap_replays_evidence_not_effect(self):
        intent = self.checkpoint()
        self.store.record_tool_result('owner', self.task['id'], intent['id'], self.claim['lease_token'],
                                      {'output': 'written', 'exit_code': 0})
        self.now[0] += 100
        self.store.recover('owner')
        claim = self.store.claim_worker('owner', self.task['id'])
        async def model(*args):
            messages = args[-2]
            self.assertEqual(messages[-1]['role'], 'tool')
            return {'role': 'assistant', 'content': 'Verified result'}
        self.runtime.model_call = model
        with patch('src.team_tools.schemas', return_value=[]):
            result = await self.runtime.execute_worker('owner', self.task['id'], claim, claim['lease_token'])
        self.assertEqual(result['successful_tools'], 1)
        self.assertEqual(self.host_calls, [])

    async def test_unknown_effect_blocks_new_claim_and_preserves_checkpoint(self):
        self.checkpoint()
        self.now[0] += 100
        self.store.recover('owner')
        self.assertIsNone(self.store.claim_worker('owner', self.task['id']))
        self.assertEqual(self.store.get_worker('owner', self.task['id'], self.worker['id'])['status'], 'blocked')
        self.assertIsNotNone(self.store.load_checkpoint('owner', self.task['id'], self.worker['id']))

    async def test_lease_renewal_failure_cancels_execution(self):
        execution = asyncio.create_task(asyncio.sleep(100))
        self.now[0] += 100
        original_sleep = asyncio.sleep
        async def no_delay(seconds):
            await original_sleep(0)
        with patch('src.team_runtime.asyncio.sleep', side_effect=no_delay):
            with self.assertRaises(Exception):
                await self.runtime._renew('owner', self.task['id'], self.worker['id'], self.claim['lease_token'], execution)
        await asyncio.gather(execution, return_exceptions=True)
        self.assertTrue(execution.cancelled())


if __name__ == '__main__':
    unittest.main()
