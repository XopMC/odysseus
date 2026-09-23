import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.chat_replay_log import ReplayLog, ReplayLimitError
from src import agent_runs


def test_long_run_replay_defaults_leave_operational_headroom():
    import src.chat_replay_log as replay
    assert replay.MAX_RUN_BYTES >= 1024 * 1024 * 1024
    assert replay.MAX_TOTAL_BYTES >= 16 * 1024 * 1024 * 1024
    assert replay.MAX_TOTAL_BYTES >= replay.MAX_RUN_BYTES


def test_stream_status_keeps_public_streaming_state_after_run_metadata_merge():
    route = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(
        encoding="utf-8"
    )
    body = route.split("async def chat_stream_status", 1)[1].split(
        "async def inject_context", 1
    )[0]

    assert body.count('"status": "streaming"') == 2
    assert body.rfind('"status": "streaming"') > body.rfind("describe_run")


def test_process_restart_reason_is_persisted_for_success_and_missing_artifact_paths():
    source = (Path(__file__).resolve().parents[1] / "src/agent_runs.py").read_text(
        encoding="utf-8"
    )
    recovery = source.split("def recover_durable_runs", 1)[1].split(
        "def event_page", 1,
    )[0]
    assert recovery.count('continuation["terminal_reason"] = "process_restarted"') == 2


def test_live_backward_event_page_has_stable_exclusive_cursor():
    run = SimpleNamespace(buffer=[f'data: {json.dumps({"delta": str(i)})}\n\n' for i in range(7)])
    with patch.dict(agent_runs._RUNS, {'fixture-chat': run}), \
         patch.object(agent_runs, 'describe_run', return_value={'run_id': 'a' * 32, 'status': 'running'}):
        page = agent_runs.event_page_before('fixture-chat', before_seq=7, limit=3)
        assert [item['seq'] for item in page['events']] == [4, 5, 6]
        assert [item['data']['delta'] for item in page['events']] == ['4', '5', '6']
        assert page['previous_cursor'] == 4 and page['has_more_before']
        retry = agent_runs.event_page_before('fixture-chat', before_seq=7, limit=3)
        assert retry['events'] == page['events']
        older = agent_runs.event_page_before('fixture-chat', before_seq=4, limit=3)
        assert [item['seq'] for item in older['events']] == [1, 2, 3]
        with pytest.raises(ValueError):
            agent_runs.event_page_before('fixture-chat', before_seq=8, limit=3)


def test_durable_event_pages_accept_client_200_limit_and_reject_stale_cursor(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from core.database import Base, ChatRunState, Session as DbSession

    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DbSession.__table__, ChatRunState.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(DbSession(id='fixture-chat', name='Safe fixture', model='local',
                         endpoint_url='http://local/v1', owner='alice'))
        db.flush()
        db.add(ChatRunState(run_id='a' * 32, session_id='fixture-chat', owner='alice', status='interrupted'))
        db.commit()
    log = ReplayLog(tmp_path, 'a' * 32, 'fixture-chat', create=True)
    for seq in range(405):
        log.append(f'data: {json.dumps({"delta": str(seq)})}\n\n')
    with patch.dict(agent_runs._RUNS, {}, clear=True), \
         patch('core.database.SessionLocal', factory), \
         patch.object(agent_runs, 'replay_root', return_value=tmp_path):
        forward = agent_runs.event_page('fixture-chat', after_seq=-1, limit=200)
        assert [item['seq'] for item in forward['events']] == list(range(200))
        older = agent_runs.event_page_before('fixture-chat', before_seq=405, limit=200)
        assert [item['seq'] for item in older['events']] == list(range(205, 405))
        assert older['previous_cursor'] == 205 and older['has_more_before']
        with pytest.raises(ValueError):
            agent_runs.event_page_before('fixture-chat', before_seq=406, limit=200)
    engine.dispose()


def test_backward_replay_route_enforces_owner_and_cursor(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from core.database import Base, ChatRunState, Session as DbSession
    from routes import chat_routes, session_routes

    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[DbSession.__table__, ChatRunState.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(DbSession(id='fixture-chat', name='Safe fixture', model='local',
                         endpoint_url='http://local/v1', owner='alice'))
        db.flush()
        db.add(ChatRunState(run_id='a' * 32, session_id='fixture-chat', owner='alice', status='interrupted'))
        db.commit()
    log = ReplayLog(tmp_path, 'a' * 32, 'fixture-chat', create=True)
    for seq in range(3):
        log.append(f'data: {json.dumps({"delta": str(seq)})}\n\n')
    monkeypatch.setattr(session_routes, 'SessionLocal', factory)
    monkeypatch.setattr(agent_runs, 'replay_root', lambda: tmp_path)
    monkeypatch.setattr(__import__('core.database', fromlist=['SessionLocal']), 'SessionLocal', factory)
    app = FastAPI()

    @app.middleware('http')
    async def owner(request, call_next):
        request.state.current_user = request.headers.get('X-Test-User', 'alice')
        return await call_next(request)

    app.include_router(chat_routes.setup_chat_routes(*[SimpleNamespace() for _ in range(6)]))
    path = '/api/chat/run/fixture-chat/events/older?before_seq=3&limit=2'
    with TestClient(app) as client:
        allowed = client.get(path)
        assert allowed.status_code == 200
        assert [item['seq'] for item in allowed.json()['events']] == [1, 2]
        assert client.get(path, headers={'X-Test-User': 'bob'}).status_code == 404
        assert client.get(path.replace('before_seq=3', 'before_seq=4')).status_code == 400
    engine.dispose()


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

    def test_two_hundred_event_page_and_backward_cursor_after_restart(self):
        for i in range(405):
            self.log.append(f'data: {i}\n\n')
        reopened = ReplayLog(self.temp.name, 'a' * 32, 'alice-chat')
        forward = reopened.page(-1, 200)
        assert [item['seq'] for item in forward['events']] == list(range(200))
        last = reopened.page_before(405, 200)
        assert [item['seq'] for item in last['events']] == list(range(205, 405))
        assert last['previous_cursor'] == 205 and last['has_more_before']
        previous = reopened.page_before(last['previous_cursor'], 200)
        assert [item['seq'] for item in previous['events']] == list(range(5, 205))
        first = reopened.page_before(previous['previous_cursor'], 200)
        assert [item['seq'] for item in first['events']] == list(range(5))
        assert first['previous_cursor'] == 0 and not first['has_more_before']
        assert reopened.page_before(0, 200)['events'] == []
        for cursor in (-1, True, 406):
            with self.assertRaises(ValueError):
                reopened.page_before(cursor, 200)
        with self.assertRaises(ValueError):
            reopened.page_before(405, 201)

    def test_read_only_reopen_does_not_scan_all_replay_artifacts(self):
        self.log.append('complete')
        with patch.object(Path, 'iterdir', side_effect=AssertionError('global replay scan')):
            reopened = ReplayLog(self.temp.name, 'a' * 32, 'alice-chat')
            self.assertEqual(reopened.page()['events'][0]['event'], 'complete')

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

    def test_terminal_reasoning_round_uses_durable_sequence_index(self):
        for seq in range(120):
            event = {
                'delta': f'thought-{seq}', 'thinking': True,
                '_replay': {'round': 2 if seq % 3 == 0 else 3, 'created_at': float(seq)},
            }
            self.log.append('data: ' + json.dumps(event) + '\n\n')
        self.log.checkpoint('done')
        reopened = ReplayLog(self.temp.name, 'a' * 32, 'alice-chat')
        expected = list(range(0, 120, 3))
        self.assertEqual(reopened.reasoning_sequences(2), expected)
        self.assertEqual(reopened.reasoning_sequences(3), [seq for seq in range(120) if seq % 3])
        self.assertTrue(reopened.path('.reasoning-index').exists())
        with patch.dict('os.environ', {'ODYSSEUS_DURABLE_CHAT_REPLAY': '1'}), \
             patch.object(agent_runs, 'replay_root', return_value=self.temp.name):
            artifact = agent_runs.reasoning_artifact('alice-chat', 'a' * 32, 2)
        self.assertEqual(artifact['thinking'], ''.join(f'thought-{seq}' for seq in expected))


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
        self.assertTrue(await agent_runs.stop_and_wait(
            'stop-wait-chat', run.run_id, reason='goal_paused',
        ))
        self.assertTrue(cleaned.is_set())
        self.assertEqual(run.status, 'stopped')
        self.assertEqual(run.terminal_reason, 'goal_paused')
        self.assertEqual(agent_runs.describe_run('stop-wait-chat')['terminal_reason'], 'goal_paused')

    async def test_live_rendered_units_track_rounds_until_message_is_saved(self):
        async def source():
            yield 'data: ' + json.dumps({'type': 'agent_step', 'round': 1}) + '\n\n'
            yield 'data: ' + json.dumps({'delta': 'first', 'thinking': True, 'round': 1}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'agent_step', 'round': 2}) + '\n\n'
            yield 'data: ' + json.dumps({'type': 'tool_start', 'tool': 'bash', 'round': 2}) + '\n\n'

        run = agent_runs.start('live-units-chat', source())
        await run.task
        self.assertEqual(len(run.rendered_rounds), 2)
        # Terminal snapshots report zero live additions to prevent double
        # counting after the canonical assistant row becomes visible.
        self.assertEqual(agent_runs.describe_run('live-units-chat')['live_rendered_units'], 2)

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
