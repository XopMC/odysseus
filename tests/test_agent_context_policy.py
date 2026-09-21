import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from src.context_policy_store import ContextPolicyStore
from src.team_store import TeamStore


class AgentContextPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.team = TeamStore(Path(temp.name) / 'team.db')
        self.store = ContextPolicyStore(self.team)

    def save(self, values):
        return self.store.save('owner', overrides=values,
            expected_revisions=self.store.get('owner')['revisions'])

    async def run_agent(self, messages=None, *, window=65536, fallback=False, change_before_dispatch=False, session_id=None):
        from src import agent_loop, tool_execution, team_runtime
        sent, summaries = [], []
        async def summary(*args, **kwargs):
            summaries.append((args, kwargs))
            return 'Preserve original goal and evidence. Verification is still required.'
        async def stream(candidates, messages, **kwargs):
            if change_before_dispatch:
                self.save({'output_reserve': 1024})
            index = 1 if fallback else 0
            url, model, headers = candidates[index]
            shaped = await kwargs['candidate_request_factory'](index, url, model, headers)
            sent.append(copy.deepcopy(shaped))
            yield 'data: ' + json.dumps({'delta': 'Verified reply'}) + '\n\n'
            yield 'data: [DONE]\n\n'
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1', 'ODYSSEUS_CONTEXT_POLICY_ENABLED': '1'}))
            stack.enter_context(patch.object(team_runtime, 'get_runtime', return_value=SimpleNamespace(store=self.team)))
            stack.enter_context(patch.object(agent_loop, 'get_setting', side_effect=lambda key, default=None: default))
            stack.enter_context(patch.object(agent_loop, 'get_mcp_manager', return_value=None))
            stack.enter_context(patch.object(agent_loop, 'blocked_tools_for_owner', return_value=set()))
            stack.enter_context(patch.object(agent_loop, '_agent_route_tool_mode', return_value=(True, False, True)))
            stack.enter_context(patch.object(agent_loop, 'stream_llm_with_fallback', side_effect=stream))
            stack.enter_context(patch.object(tool_execution, '_owner_is_admin', return_value=True))
            stack.enter_context(patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}))
            stack.enter_context(patch('src.settings.get_setting', side_effect=lambda key, default=None: default))
            stack.enter_context(patch('src.host_execution.enabled_for', return_value=False))
            stack.enter_context(patch('src.model_context.budget_context_for_model', return_value=window))
            stack.enter_context(patch('src.llm_core.llm_call_async', side_effect=summary))
            chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
                'http://fixture.invalid/v1', 'fixture-model', messages or [{'role':'user','content':'Reply briefly with your status'}],
                owner='owner', session_id=session_id, relevant_tools={'read_file'}, context_length=65536,
                max_tokens=4096, max_rounds=1, _is_teacher_run=True,
                fallbacks=[('http://second.invalid/v1', 'second-model', {})] if fallback else None)]
        return sent, summaries, ''.join(chunks)

    async def test_saved_owner_profile_caps_actual_primary_and_fallback_dispatch(self):
        self.save({'output_reserve': 768})
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                sent, summaries, chunks = await self.run_agent(fallback=fallback)
                self.assertEqual(sent[0]['kwargs']['max_tokens'], 768)
                self.assertFalse(summaries)
                self.assertIn('context_policy', chunks)

    async def test_chat_policy_shapes_agent_without_changing_other_chats(self):
        from contextlib import contextmanager
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from core.database import Base, Session
        from src.team_store import NotFound
        engine = create_engine('sqlite:///:memory:')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        @contextmanager
        def database():
            with factory() as db:
                yield db
        with factory() as db:
            for identity, owner in [('chat-a', 'owner'), ('chat-b', 'owner'), ('private', 'other')]:
                db.add(Session(id=identity, name=identity, owner=owner, endpoint_url='http://fixture.invalid/v1', model='fixture-model'))
            db.commit()
        with patch('core.database.get_db_session', database):
            self.save({'output_reserve': 1024})
            initial = self.store.get('owner', session_id='chat-a')
            self.store.save('owner', session_id='chat-a', overrides={'output_reserve': 512}, expected_revisions=initial['revisions'])
            with self.assertRaises(NotFound):
                self.store.get('owner', session_id='private')
            with self.assertRaises(ValueError):
                self.store.get('owner', session_id='chat-a', task_id='task')
            for identity, expected in [('chat-a', 512), ('chat-b', 1024)]:
                sent, _, _ = await self.run_agent(session_id=identity)
                self.assertEqual(sent[0]['kwargs']['max_tokens'], expected)
            self.assertEqual(self.store.get('owner')['effective']['output_reserve'], 1024)

    async def test_unknown_window_never_uses_user_requested_capacity(self):
        self.save({'requested_window': 131072})
        sent, summaries, chunks = await self.run_agent(window=0)
        self.assertFalse(sent)
        self.assertFalse(summaries)
        self.assertIn('context_compaction_failed', chunks)

    async def test_large_context_uses_configured_summary_then_reduced_request(self):
        self.save({'trigger_percent': 60, 'target_percent': 45, 'recent_groups': 1,
                   'recent_tokens': 0, 'summary_tokens': 256, 'output_reserve': 1024})
        history = [{'role':'user','content':'Original requirement: preserve data'}] + [
            {'role':'assistant','content':'Measured source evidence. ' * 1000} for _ in range(10)
        ] + [{'role':'user','content':'Continue verification'}]
        sent, summaries, chunks = await self.run_agent(history)
        self.assertEqual(len(summaries), 1)
        # Thinking-capable local models need generation headroom before their
        # bounded final summary. The compacted answer is still validated
        # against summary_tokens=256.
        self.assertEqual(summaries[0][1]['max_tokens'], 1024)
        self.assertEqual(sent[0]['kwargs']['max_tokens'], 1024)
        self.assertIn('Original requirement', str(sent[0]['messages']))
        self.assertIn('Continue verification', str(sent[0]['messages']))
        self.assertLess(len(str(sent[0]['messages'])), len(str(history)))
        self.assertIn('"compacted"', chunks)

    async def test_aggressive_target_relaxes_below_trigger_when_pins_do_not_fit(self):
        from src.context_policy import ContextPolicy
        from src.context_policy_runtime import shape_request
        messages = [{'role': 'user', 'content': 'evidence ' * 40000}]
        policy = ContextPolicy(trigger_percent=75, target_percent=15)
        record = {'effective': policy.to_dict(), 'revisions': {'owner': 1}}
        compacted = [{'role': 'system', 'content': 'safe checkpoint'}]
        with patch(
            'src.context_policy_runtime.compact_working_context',
            new=AsyncMock(side_effect=[(messages, 'uncompactable'), (compacted, 'compacted')]),
        ) as compact:
            shaped, telemetry = await shape_request(
                messages, [], record, 65536, AsyncMock(return_value='summary'),
            )
        self.assertEqual(shaped, compacted)
        self.assertEqual(compact.await_count, 2)
        first_target = compact.await_args_list[0].kwargs['target_limit']
        second_target = compact.await_args_list[1].kwargs['target_limit']
        self.assertGreater(second_target, first_target)
        self.assertLess(second_target, telemetry['trigger_messages'])
        self.assertEqual(telemetry['target_messages'], first_target)
        self.assertEqual(telemetry['effective_target_messages'], second_target)

    async def test_relaxed_target_reuses_identical_summary_prompt(self):
        from src.context_policy import ContextPolicy
        from src.context_policy_runtime import shape_request
        messages = [{'role': 'user', 'content': 'evidence ' * 40000}]
        policy = ContextPolicy(trigger_percent=75, target_percent=15)
        record = {'effective': policy.to_dict(), 'revisions': {'owner': 1}}
        compacted = [{'role': 'system', 'content': 'safe checkpoint'}]
        summarize = AsyncMock(return_value='summary')
        calls = 0

        async def fake_compact(_messages, _limit, summary_fn, **kwargs):
            nonlocal calls
            calls += 1
            await summary_fn([{'role': 'user', 'content': 'same evidence'}])
            return (_messages, 'uncompactable') if calls == 1 else (compacted, 'compacted')

        with patch('src.context_policy_runtime.compact_working_context', new=fake_compact):
            shaped, _telemetry = await shape_request(
                messages, [], record, 65536, summarize,
            )
        self.assertEqual(shaped, compacted)
        self.assertEqual(summarize.await_count, 1)

    def test_explicit_policy_skips_the_parallel_economic_compactor(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'src/agent_loop.py').read_text()
        self.assertIn(
            'if _efficiency_enabled("online_context_compact") and not _context_profile:',
            source,
        )

    async def test_oversized_optional_recent_tail_is_folded_after_safe_retry(self):
        from src.context_policy import ContextPolicy
        from src.context_policy_runtime import shape_request
        messages = [{'role': 'user', 'content': 'evidence ' * 40000}]
        policy = ContextPolicy(trigger_percent=75, target_percent=15,
                               recent_groups=4, recent_tokens=2048)
        record = {'effective': policy.to_dict(), 'revisions': {'owner': 1}}
        compacted = [{'role': 'system', 'content': 'safe checkpoint'}]
        with patch(
            'src.context_policy_runtime.compact_working_context',
            new=AsyncMock(side_effect=[
                (messages, 'uncompactable'),
                (messages, 'uncompactable'),
                (compacted, 'compacted'),
            ]),
        ) as compact:
            shaped, telemetry = await shape_request(
                messages, [], record, 65536, AsyncMock(return_value='summary'),
            )
        self.assertEqual(shaped, compacted)
        self.assertEqual(compact.await_count, 3)
        emergency_policy = compact.await_args_list[2].kwargs['policy']
        self.assertEqual(emergency_policy.recent_groups, 0)
        self.assertEqual(emergency_policy.recent_tokens, 0)
        self.assertEqual(telemetry['effective_recent_groups'], 0)
        self.assertEqual(telemetry['effective_recent_tokens'], 0)

    async def test_disabled_compaction_blocks_overflow_without_request(self):
        self.save({'auto_compact': False})
        sent, summaries, chunks = await self.run_agent([
            {'role':'user','content':'Do not lose these requirements. ' * 10000}])
        self.assertFalse(sent)
        self.assertFalse(summaries)
        self.assertIn('context_compaction_failed', chunks)

    async def test_profile_change_before_dispatch_requires_new_request(self):
        from fastapi import HTTPException
        self.save({'output_reserve': 768})
        with self.assertRaises(HTTPException) as error:
            await self.run_agent(change_before_dispatch=True)
        self.assertEqual(error.exception.status_code, 409)
