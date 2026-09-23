"""A killed web worker must preserve replay without re-running a tool."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_script(source, data_dir):
    environment = os.environ.copy()
    environment.update({
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "DATABASE_URL": f"sqlite:///{data_dir / 'app.db'}",
        "ODYSSEUS_DURABLE_CHAT_REPLAY": "1",
        "AUTH_ENABLED": "false",
    })
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=20,
    )


def test_process_kill_after_tool_start_recovers_without_reexecution(tmp_path):
    crashed = _run_script("""
        import asyncio
        import os
        from core.database import Base, ChatMessage, ChatRunState, ChatToolIntent, ChatWorkEvent, Session, SessionLocal, engine
        from src import agent_runs
        from src.chat_effect_inbox import inbox

        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatMessage.__table__, ChatRunState.__table__, ChatToolIntent.__table__, ChatWorkEvent.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='kill-fixture', name='Safe kill fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))

        async def source():
            yield 'data: {"type":"agent_step","round":1}\\n\\n'
            yield 'data: {"type":"tool_start","tool":"read_file","tool_call_id":"call-1","round":1}\\n\\n'
            os._exit(17)

        async def main():
            run = agent_runs.start('kill-fixture', source(), owner='alice')
            inbox.record_intent('alice', 'kill-fixture', run.run_id, 'call-1', 'bash', 'safe fixture effect')
            await run.task

        asyncio.run(main())
    """, tmp_path)
    assert crashed.returncode == 17, crashed.stderr
    with sqlite3.connect(tmp_path / "app.db") as db:
        row = db.execute(
            "SELECT status, last_seq, durable_seq FROM chat_run_states WHERE session_id=?",
            ("kill-fixture",),
        ).fetchone()
    assert row == ("running", 1, 1)

    restarted = _run_script("""
        import json
        from core.database import ChatMessage, ChatWorkEvent, SessionLocal
        from src import agent_runs
        from src.chat_effect_inbox import inbox

        recovered = agent_runs.recover_durable_runs()
        snapshot = agent_runs.describe_run('kill-fixture')
        page = agent_runs.event_page('kill-fixture', after_seq=-1)
        second = agent_runs.recover_durable_runs()
        with SessionLocal() as db:
            assistant_rows = db.query(ChatMessage).filter_by(
                session_id='kill-fixture', role='assistant',
            ).count()
            effect_events = [
                {'kind': row.kind, 'revision': row.revision, 'payload': row.payload}
                for row in db.query(ChatWorkEvent).filter_by(
                    session_id='kill-fixture', kind='effect_unknown',
                ).all()
            ]
        print(json.dumps({
            'recovered': len(recovered), 'second': len(second),
            'snapshot': snapshot, 'events': page['events'],
            'assistant_rows': assistant_rows,
            'unknown_intents': [item['status'] for item in inbox.unresolved('alice', 'kill-fixture')],
            'effect_events': effect_events,
        }))
    """, tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert state["recovered"] == 1
    assert state["second"] == 0
    assert state["snapshot"]["status"] == "interrupted"
    assert state["snapshot"]["terminal_reason"] == "process_restarted"
    assert state["snapshot"]["next_seq"] == 2
    assert state["snapshot"]["durable_seq"] == 1
    assert [item["data"]["type"] for item in state["events"]] == ["agent_step", "tool_start"]
    assert state["assistant_rows"] == 1
    assert state["unknown_intents"] == ["unknown"]
    assert len(state["effect_events"]) == 1
    assert state["effect_events"][0]["revision"] == 2
    assert state["effect_events"][0]["payload"]["status"] == "unknown"


def test_process_kill_after_tool_result_keeps_receipt_and_replays_result_once(tmp_path):
    crashed = _run_script("""
        import asyncio
        import os
        from core.database import Base, ChatMessage, ChatRunState, ChatToolIntent, ChatWorkEvent, Session, SessionLocal, engine
        from src import agent_runs
        from src.chat_effect_inbox import inbox

        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatMessage.__table__, ChatRunState.__table__,
            ChatToolIntent.__table__, ChatWorkEvent.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='tool-result-fixture', name='Safe tool result fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))

        async def source():
            yield 'data: {"type":"agent_step","round":1}\\n\\n'
            yield 'data: {"type":"tool_start","tool":"write_file","tool_call_id":"call-safe","round":1}\\n\\n'
            yield 'data: {"type":"tool_output","tool":"write_file","tool_call_id":"call-safe","exit_code":0,"output":"safe-fixture-result"}\\n\\n'
            os._exit(17)

        async def main():
            run = agent_runs.start('tool-result-fixture', source(), owner='alice')
            intent = inbox.record_intent(
                'alice', 'tool-result-fixture', run.run_id, 'call-safe',
                'write_file', '{"path":"fixture.txt","content":"safe"}',
            )
            inbox.record_result('alice', 'tool-result-fixture', intent['id'], {
                'exit_code': 0, 'outcome_unknown': False,
            })
            await run.task

        asyncio.run(main())
    """, tmp_path)
    assert crashed.returncode == 17, crashed.stderr

    restarted = _run_script("""
        import json
        from core.database import ChatMessage, ChatToolIntent, SessionLocal
        from src import agent_runs
        from src.chat_effect_inbox import inbox

        recovered = agent_runs.recover_durable_runs()
        second = agent_runs.recover_durable_runs()
        page = agent_runs.event_page('tool-result-fixture', after_seq=-1)
        with SessionLocal() as db:
            intent = db.query(ChatToolIntent).filter_by(
                session_id='tool-result-fixture', tool_call_id='call-safe',
            ).one()
            assistants = db.query(ChatMessage).filter_by(
                session_id='tool-result-fixture', role='assistant',
            ).count()
        print(json.dumps({
            'recovered': len(recovered), 'second': len(second),
            'status': agent_runs.describe_run('tool-result-fixture')['status'],
            'types': [row['data']['type'] for row in page['events']],
            'intent_status': intent.status, 'intent_revision': intent.revision,
            'unresolved': [row['status'] for row in inbox.unresolved('alice', 'tool-result-fixture')],
            'assistants': assistants,
        }))
    """, tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert state['recovered'] == 1
    assert state['second'] == 0
    assert state['status'] == 'interrupted'
    assert state['types'] == ['agent_step', 'tool_start', 'tool_output']
    assert (state['intent_status'], state['intent_revision']) == ('done', 2)
    assert state['unresolved'] == []
    assert state['assistants'] == 1


def test_missing_replay_still_fences_effect_and_notifies_once(tmp_path):
    created = _run_script("""
        from core.database import Base, ChatRunState, ChatToolIntent, ChatWorkEvent, Session, SessionLocal, engine
        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatRunState.__table__, ChatToolIntent.__table__, ChatWorkEvent.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='missing-fixture', name='Safe missing replay fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))
        with SessionLocal.begin() as db:
            db.add(ChatRunState(run_id='c'*32, session_id='missing-fixture', owner='alice', status='running'))
            db.add(ChatToolIntent(id='effect-1', owner='alice', session_id='missing-fixture',
                                  run_id='c'*32, tool_call_id='call-1', tool_name='bash',
                                  action_hash='d'*64, status='intent', revision=1))
    """, tmp_path)
    assert created.returncode == 0, created.stderr
    restarted = _run_script("""
        import json
        from core.database import ChatRunState, ChatToolIntent, ChatWorkEvent, SessionLocal
        from src import agent_runs
        first = agent_runs.recover_durable_runs()
        second = agent_runs.recover_durable_runs()
        with SessionLocal() as db:
            run = db.query(ChatRunState).filter_by(run_id='c'*32).one()
            effect = db.query(ChatToolIntent).filter_by(id='effect-1').one()
            events = db.query(ChatWorkEvent).filter_by(kind='effect_unknown', entity_id='effect-1').all()
            print(json.dumps({'first': len(first), 'second': len(second),
                              'run': run.status, 'effect': effect.status,
                              'events': [row.payload for row in events]}))
    """, tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert state == {
        "first": 0, "second": 0, "run": "interrupted", "effect": "unknown",
        "events": [{"intent_id": "effect-1", "status": "unknown"}],
    }


@pytest.mark.parametrize("event_type, resource", [
    ("context_compaction_failed", None), ("agent_terminal", None),
    ("budget_exceeded", "tool_calls"), ("budget_exceeded", "model_tokens"),
    ("budget_exceeded", "model_requests"),
    ("rounds_exhausted", "model_rounds"),
])
def test_process_kill_after_failure_boundary_has_durable_cursor(tmp_path, event_type, resource):
    payload = (
        {"type": "agent_terminal", "data": {"failed": True, "failure": {"kind": "context_compaction"}}}
        if event_type == "agent_terminal" else
        {"type": "budget_exceeded", "resource": resource, "used": 1, "limit": 1}
        if event_type == "budget_exceeded" else
        {"type": "rounds_exhausted", "resource": "model_rounds", "used": 1, "limit": 1}
        if event_type == "rounds_exhausted" else
        {"type": "context_compaction_failed", "reason": "failed"}
    )
    source = f"""
        import asyncio
        import os
        from core.database import Base, ChatMessage, ChatRunState, Session, SessionLocal, engine
        from src import agent_runs

        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatMessage.__table__, ChatRunState.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='failure-fixture', name='Safe failure fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))

        async def source():
            yield 'data: ' + {json.dumps(payload)!r} + '\\n\\n'
            os._exit(17)

        async def main():
            run = agent_runs.start('failure-fixture', source(), owner='alice')
            await run.task

        asyncio.run(main())
    """
    crashed = _run_script(source, tmp_path)
    assert crashed.returncode == 17, crashed.stderr
    with sqlite3.connect(tmp_path / "app.db") as db:
        row = db.execute(
            "SELECT status, last_seq, durable_seq FROM chat_run_states WHERE session_id=?",
            ("failure-fixture",),
        ).fetchone()
    assert row == ("running", 0, 0)


@pytest.mark.parametrize("stage", ["compacted", "context_checkpoint"])
def test_process_kill_during_compaction_preserves_model_checkpoint(tmp_path, stage):
    payload = (
        {"type": "compacted", "checkpoint": {
            "summary": "fixture-summary", "compactions": 1, "ledger_hash": "a" * 64,
        }} if stage == "compacted" else
        {"type": "context_checkpoint", "messages": [
            {"role": "user", "content": "fixture-private-ledger"},
            {"role": "assistant", "content": "fixture-result"},
        ], "compactions": 1, "ledger_hash": "b" * 64}
    )
    crashed = _run_script(f"""
        import asyncio
        import os
        from core.database import Base, ChatMessage, ChatRunState, Session, SessionLocal, engine
        from src import agent_runs

        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatMessage.__table__, ChatRunState.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='compact-fixture', name='Safe compact fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))

        async def source():
            yield 'data: ' + {json.dumps(payload)!r} + '\\n\\n'
            os._exit(17)

        async def main():
            run = agent_runs.start('compact-fixture', source(), owner='alice')
            await run.task

        asyncio.run(main())
    """, tmp_path)
    assert crashed.returncode == 17, crashed.stderr
    restarted = _run_script("""
        import json
        from src import agent_runs
        recovered = agent_runs.recover_durable_runs()
        checkpoint = agent_runs.continuation_for_session('compact-fixture').get('working_checkpoint')
        page = agent_runs.event_page('compact-fixture', after_seq=-1)
        print(json.dumps({'recovered': len(recovered), 'checkpoint': checkpoint,
                          'events': page['events']}))
    """, tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert state["recovered"] == 1
    assert state["checkpoint"]["compactions"] == 1
    if stage == "compacted":
        assert state["checkpoint"]["summary"] == "fixture-summary"
    else:
        assert state["checkpoint"]["messages"][0]["content"] == "fixture-private-ledger"
        assert "fixture-private-ledger" not in json.dumps(state["events"])


def test_process_kill_at_approval_wait_preserves_wait_state_without_reexecution(tmp_path):
    crashed = _run_script("""
        import asyncio
        import os
        from core.database import Base, ChatMessage, ChatRunState, Session, SessionLocal, engine
        from src import agent_runs

        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatMessage.__table__, ChatRunState.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='approval-fixture', name='Safe approval fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))

        async def source():
            yield 'data: {"type":"ask_user","data":{"kind":"tool_approval","approval_id":"approval-1"}}\\n\\n'
            os._exit(17)

        async def main():
            run = agent_runs.start('approval-fixture', source(), owner='alice')
            await run.task

        asyncio.run(main())
    """, tmp_path)
    assert crashed.returncode == 17, crashed.stderr
    restarted = _run_script("""
        import json
        from core.database import ChatMessage, SessionLocal
        from src import agent_runs
        first = agent_runs.recover_durable_runs()
        second = agent_runs.recover_durable_runs()
        snapshot = agent_runs.describe_run('approval-fixture')
        page = agent_runs.event_page('approval-fixture', after_seq=-1)
        with SessionLocal() as db:
            assistants = db.query(ChatMessage).filter_by(session_id='approval-fixture', role='assistant').count()
        print(json.dumps({'first': len(first), 'second': len(second), 'snapshot': snapshot,
                          'events': page['events'], 'assistants': assistants}))
    """, tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert (state["first"], state["second"]) == (1, 0)
    assert state["snapshot"]["status"] == "interrupted"
    assert state["snapshot"]["wait_state"]["phase"] == "reconnect"
    assert state["snapshot"]["durable_seq"] == 0
    assert state["events"][0]["data"]["type"] == "ask_user"
    assert state["assistants"] == 1


def test_process_kill_after_child_terminal_commit_preserves_join_result_and_event(tmp_path):
    crashed = _run_script("""
        import os
        from datetime import datetime, timezone
        from core.database import Base, Session, SessionLocal, engine
        from src.database import ChatSubagentEvent, ChatSubagentRun
        from src.subagent_runtime import SubagentRuntime

        Base.metadata.create_all(bind=engine, tables=[
            Session.__table__, ChatSubagentRun.__table__, ChatSubagentEvent.__table__,
        ])
        with SessionLocal.begin() as db:
            db.add(Session(id='child-join-fixture', name='Child join fixture', owner='alice',
                           endpoint_url='http://fixture.invalid/v1', model='fixture'))
            db.flush()
            db.add(ChatSubagentRun(
                id='d'*32, parent_session_id='child-join-fixture', parent_run_id='p'*32,
                owner='alice', ordinal=1, name='Fixture child', objective='safe objective',
                assigned_context='', model='fixture', endpoint_id='fixture-endpoint',
                status='running', worker_id='old-worker',
            ))
        runtime = SubagentRuntime()
        runtime._update_with_event(
            'd'*32, 'alice', 'child-join-fixture', 'status',
            {'status': 'completed', 'result': 'safe-result'},
            status='completed', result='safe-result', error='', slot=None,
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        os._exit(17)
    """, tmp_path)
    assert crashed.returncode == 17, crashed.stderr

    restarted = _run_script("""
        import asyncio, json
        from src.subagent_runtime import SubagentRuntime

        runtime = SubagentRuntime()
        recovered = runtime.recover_stale()
        joined = asyncio.run(runtime.wait(
            'alice', 'child-join-fixture', ['d'*32], timeout_seconds=0, wait_for='all',
        ))
        events = runtime.events('alice', 'child-join-fixture', child_id='d'*32)
        print(json.dumps({'recovered': recovered, 'joined': joined,
                          'terminal_events': [e for e in events if e['kind'] == 'status'
                                              and e['payload'].get('status') == 'completed']}))
    """, tmp_path)
    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert state['recovered'] == 0
    assert state['joined']['completed'] is True
    assert state['joined']['subagents'][0]['status'] == 'completed'
    assert state['joined']['subagents'][0]['result'] == 'safe-result'
    assert len(state['terminal_events']) == 1
