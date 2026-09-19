import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from src.chat_replay_log import ReplayLog, ReplayLimitError
from src import agent_runs


def test_stream_status_keeps_public_streaming_state_after_run_metadata_merge():
    route = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(
        encoding="utf-8"
    )
    body = route.split("async def chat_stream_status", 1)[1].split(
        "async def inject_context", 1
    )[0]

    assert body.count('"status": "streaming"') == 2
    assert body.rfind('"status": "streaming"') > body.rfind("describe_run")


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = ReplayLog(self.temp.name, 'a' * 32, 'alice-chat', create=True)

    def test_restart_cursor_and_no_false_execution_recovery(self):
        for i in range(205):
            self.log.append(f'data: {i}\n\n')
        reopened = ReplayLog(self.temp.name, 'a' * 32, 'alice-chat')
        self.assertEqual(reopened.page()['status'], 'interrupted')
        self.assertEqual(reopened.page(active=True)['status'], 'running')
        cursor, seen = -1, []
        while True:
            page = reopened.page(cursor)
            seen += [event['event'] for event in page['events']]
            cursor = page['next_seq']
            if not page['has_more']:
                break
        self.assertEqual(seen, [f'data: {i}\n\n' for i in range(205)])
        self.log.checkpoint('done')
        self.assertEqual(ReplayLog(self.temp.name, 'a' * 32, 'alice-chat').page()['status'], 'done')

    def test_foreign_session_and_invalid_paths(self):
        with self.assertRaises(FileNotFoundError):
            ReplayLog(self.temp.name, 'a' * 32, 'bob-chat')
        for identity in ('../secrets', '', 'a' * 33):
            with self.assertRaises(ValueError):
                ReplayLog(self.temp.name, identity, 'alice-chat')
        for cursor in (-2, True, 100):
            with self.assertRaises(ValueError):
                self.log.page(cursor)

    def test_size_limits_fail_closed_without_indexing_partial_event(self):
        with patch('src.chat_replay_log.MAX_EVENT_BYTES', 8):
            with self.assertRaises(ReplayLimitError):
                self.log.append('a' * 9)
        with patch('src.chat_replay_log.MAX_TOTAL_BYTES', 1):
            with self.assertRaises(ReplayLimitError):
                self.log.append('x')
        self.assertEqual(len(self.log), 0)
        self.log.append('valid')
        with patch('src.chat_replay_log.MAX_RUN_BYTES', 14):
            with self.assertRaises(ReplayLimitError):
                self.log.append('xx')
        self.assertEqual(self.log[0], 'valid')

    def test_unindexed_tail_is_not_replayed(self):
        self.log.append('complete')
        with self.log.path('.events').open('ab') as file:
            file.write(b'partial')
        self.assertEqual(self.log.page()['events'], [{'seq': 0, 'event': 'complete'}])

    def test_reasoning_artifact_survives_terminal_reopen(self):
        event = {'delta': 'private reasoning', 'thinking': True,
                 '_replay': {'run_id': 'a' * 32, 'round': 3, 'created_at': 10.0}}
        self.log.append('data: ' + json.dumps(event) + '\n\n')
        self.log.checkpoint('done')
        with patch.dict('os.environ', {'ODYSSEUS_DURABLE_CHAT_REPLAY': '1'}), \
             patch.object(agent_runs, 'replay_root', return_value=self.temp.name):
            artifact = agent_runs.reasoning_artifact('alice-chat', 'a' * 32, 3)
        self.assertEqual(artifact['thinking'], 'private reasoning')
        self.assertEqual(artifact['round'], 3)
        self.assertEqual(artifact['created_at'], 10.0)


class DetachedReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict('os.environ', {'ODYSSEUS_DURABLE_CHAT_REPLAY': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = patch.object(agent_runs, 'replay_root', return_value=self.temp.name)
        self.root.start()
        self.addCleanup(self.root.stop)

    async def asyncTearDown(self):
        for run in list(agent_runs._RUNS.values()):
            if run.task and not run.task.done():
                run.task.cancel()
            if run.evict_task:
                run.evict_task.cancel()
        agent_runs._RUNS.clear()

    async def test_slow_client_queue_is_bounded_and_complete(self):
        gate = asyncio.Event()
        async def source():
            yield 'data: first\n\n'
            await gate.wait()
            for i in range(2000):
                yield f'data: {i}\n\n'
        run = agent_runs.start('chat', source())
        subscriber = agent_runs.subscribe('chat', run)
        self.assertEqual(await anext(subscriber), 'data: first\n\n')
        gate.set()
        await run.task
        self.assertTrue(all(q.qsize() <= 1 for q in run.subscribers))
        output = [event async for event in subscriber]
        self.assertEqual(output, [f'data: {i}\n\n' for i in range(2000)])
        self.assertEqual(run.buffer.metadata['status'], 'done')

    async def test_disk_failure_closes_producer_and_does_not_repeat_effect(self):
        effects, closed = [], []
        async def source():
            try:
                effects.append('once')
                yield 'data: result\n\n'
                effects.append('must not happen')
            finally:
                closed.append(True)
        with patch('src.chat_replay_log.MAX_RUN_BYTES', 1):
            run = agent_runs.start('failed', source())
            await run.task
        self.assertEqual(effects, ['once'])
        self.assertEqual(closed, [True])
        self.assertEqual(run.status, 'error')

    async def test_failed_replacement_allocation_preserves_active_run(self):
        gate = asyncio.Event()
        async def source():
            yield 'first'
            await gate.wait()
        first = agent_runs.start('same', source())
        await asyncio.sleep(0)
        replacement = source()
        with patch('src.chat_replay_log.MAX_TOTAL_BYTES', 1):
            with self.assertRaises(ReplayLimitError):
                agent_runs.start('same', replacement)
        await replacement.aclose()
        self.assertIs(agent_runs.get_active_run('same'), first)
        self.assertFalse(first.task.cancelled())
        gate.set()
        await first.task

    async def test_json_events_have_stable_ids_and_cursor_resume(self):
        async def source():
            yield 'data: ' + json.dumps({'delta': 'hello'}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'tool_start', 'tool': 'bash'}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'tool_progress', 'tool': 'bash', 'tail': 'one'}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'tool_output', 'tool': 'bash', 'output': 'ok', 'exit_code': 0}) + '\n\n'

        run = agent_runs.start('cursor-chat', source())
        await run.task
        all_events = [event async for event in agent_runs.subscribe('cursor-chat', run)]
        resumed = [event async for event in agent_runs.subscribe('cursor-chat', run, after_seq=1)]
        self.assertEqual(len(all_events), 4)
        self.assertEqual(resumed, all_events[2:])
        decoded = []
        for seq, event in enumerate(all_events):
            self.assertIn(f'id: {seq}\n', event)
            payload = json.loads(next(line[6:] for line in event.splitlines() if line.startswith('data: ')))
            self.assertEqual(payload['_replay']['run_id'], run.run_id)
            self.assertEqual(payload['_replay']['seq'], seq)
            decoded.append(payload)
        tool_ids = [item['tool_call_id'] for item in decoded[1:]]
        self.assertEqual(tool_ids, [tool_ids[0]] * 3)

    async def test_exact_stop_waits_for_generator_cleanup(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def source():
            try:
                yield 'data: {"delta":"partial"}\n\n'
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        run = agent_runs.start('stop-wait-chat', source())
        await started.wait()
        self.assertTrue(await agent_runs.stop_and_wait('stop-wait-chat', run.run_id))
        self.assertTrue(cleaned.is_set())
        self.assertEqual(run.status, 'stopped')

    async def test_terminal_snapshot_is_persisted_before_status_is_visible(self):
        observed = []

        async def source():
            yield 'data: ' + json.dumps({
                'type': 'context_usage',
                'data': {
                    'used_tokens': 40000, 'context_length': 100000,
                    'model': 'fixture', 'source': 'backend',
                },
            }) + '\n\n'

        def persist(_session_id, run, *, status=None):
            observed.append((run.status, status, run.context_usage['used_tokens']))

        with patch('src.agent_runs._persist_timeline_v2', persist):
            run = agent_runs.start('terminal-order', source())
            await run.task
        self.assertEqual(observed, [('running', 'done', 40000)])
        self.assertEqual(run.status, 'done')

    async def test_terminal_controller_runs_without_a_subscriber_but_not_after_stop(self):
        completed = asyncio.Event()

        async def source():
            yield 'data: {"delta":"done"}\n\n'

        async def controller(status):
            self.assertEqual(status, 'done')
            completed.set()

        run = agent_runs.start('goal-controller-chat', source(), on_terminal=controller)
        await asyncio.wait_for(run.task, timeout=1)
        await asyncio.wait_for(completed.wait(), timeout=1)

        stopped_callback = asyncio.Event()

        async def waiting_source():
            yield 'data: {"delta":"partial"}\n\n'
            await asyncio.Event().wait()

        stopped = agent_runs.start(
            'goal-controller-stop', waiting_source(),
            on_terminal=lambda _status: stopped_callback.set(),
        )
        await asyncio.sleep(0)
        self.assertTrue(await agent_runs.stop_and_wait('goal-controller-stop', stopped.run_id))
        await asyncio.sleep(0)
        self.assertFalse(stopped_callback.is_set())
