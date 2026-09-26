import copy
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx
from fastapi import HTTPException

from src.context_policy_store import ContextPolicyStore
from src.team_store import TeamStore


def test_checkpoint_summarizer_error_codes_are_stable_and_redacted():
    from src.agent_loop import _checkpoint_summarizer_error_code

    assert _checkpoint_summarizer_error_code(
        HTTPException(429, "private quota response")) == "summarizer_rate_limited"
    assert _checkpoint_summarizer_error_code(
        HTTPException(404, "private model name")) == "summarizer_model_unavailable"
    assert _checkpoint_summarizer_error_code(
        HTTPException(502, "Model returned reasoning but no answer content")) == "summarizer_no_answer"
    assert _checkpoint_summarizer_error_code(
        HTTPException(503, "Cannot reach 192.168.50.4:1234: No route to host")) == "summarizer_transport_unavailable"
    assert _checkpoint_summarizer_error_code(
        HTTPException(503, "Provider maintenance")) == "summarizer_provider_error"
    assert _checkpoint_summarizer_error_code(
        httpx.ConnectError("private internal host")) == "summarizer_transport_unavailable"
    assert _checkpoint_summarizer_error_code(
        httpx.ReadTimeout("private internal host")) == "summarizer_timeout"


class AgentContextPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.team = TeamStore(Path(temp.name) / 'team.db')
        self.store = ContextPolicyStore(self.team)

    def save(self, values):
        return self.store.save('owner', overrides=values,
            expected_revisions=self.store.get('owner')['revisions'])

    async def run_agent(self, messages=None, *, window=65536, fallback=False, change_before_dispatch=False, session_id=None,
                        summary_impl=None, utility_route=None, window_error=None, request_max_tokens=4096,
                        agent_output_setting=4096):
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
            sent_request = copy.deepcopy(shaped)
            sent_request['_stream_max_tokens'] = kwargs.get('max_tokens')
            sent.append(sent_request)
            yield 'data: ' + json.dumps({'delta': 'Verified reply'}) + '\n\n'
            yield 'data: [DONE]\n\n'
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'ODYSSEUS_ENGINEERING_ENABLED': '1', 'ODYSSEUS_CONTEXT_POLICY_ENABLED': '1'}))
            stack.enter_context(patch.object(team_runtime, 'get_runtime', return_value=SimpleNamespace(store=self.team)))
            stack.enter_context(patch.object(agent_loop, 'get_setting', side_effect=lambda key, default=None:
                                      agent_output_setting if key == 'agent_output_token_budget' else default))
            stack.enter_context(patch.object(agent_loop, 'get_mcp_manager', return_value=None))
            stack.enter_context(patch.object(agent_loop, 'blocked_tools_for_owner', return_value=set()))
            stack.enter_context(patch.object(agent_loop, '_agent_route_tool_mode', return_value=(True, False, True)))
            stack.enter_context(patch.object(agent_loop, 'stream_llm_with_fallback', side_effect=stream))
            stack.enter_context(patch.object(tool_execution, '_owner_is_admin', return_value=True))
            stack.enter_context(patch.object(tool_execution, '_current_agent_privileges', return_value={'can_use_agent': True}))
            stack.enter_context(patch('src.settings.get_setting', side_effect=lambda key, default=None: default))
            stack.enter_context(patch('src.host_execution.enabled_for', return_value=False))
            stack.enter_context(patch('src.model_context.budget_context_for_model',
                                      side_effect=window_error if window_error else None,
                                      return_value=window))
            stack.enter_context(patch('src.llm_core.llm_call_async', side_effect=summary_impl or summary))
            if utility_route is not None:
                stack.enter_context(patch('src.endpoint_resolver.resolve_endpoint', return_value=utility_route))
                stack.enter_context(patch('src.endpoint_resolver.resolve_utility_fallback_candidates', return_value=[]))
            chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
                'http://fixture.invalid/v1', 'fixture-model', messages or [{'role':'user','content':'Reply briefly with your status'}],
                owner='owner', session_id=session_id, relevant_tools={'read_file'}, context_length=window,
                max_tokens=request_max_tokens, max_rounds=1, _is_teacher_run=True,
                fallbacks=[('http://second.invalid/v1', 'second-model', {})] if fallback else None)]
        return sent, summaries, ''.join(chunks)

    async def test_saved_owner_profile_enforces_minimum_generation_budget_on_primary_and_fallback(self):
        self.save({'output_reserve': 768})
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                sent, summaries, chunks = await self.run_agent(fallback=fallback)
                self.assertEqual(sent[0]['kwargs']['max_tokens'], 4096)
                self.assertEqual(sent[0]['_stream_max_tokens'], 4096)
                self.assertFalse(summaries)
                self.assertIn('context_policy', chunks)

    async def test_unconfigured_agent_does_not_inherit_small_or_implicit_provider_limit(self):
        sent, _summaries, chunks = await self.run_agent(request_max_tokens=0)
        self.assertEqual(sent[0]['_stream_max_tokens'], 4096)
        self.assertIn('"generation_budget_tokens": 4096', chunks)

    async def test_agent_output_setting_controls_budget_above_minimum_with_profile_reserve(self):
        self.save({'output_reserve': 8192})
        sent, _summaries, _chunks = await self.run_agent(
            request_max_tokens=4096, agent_output_setting=6000)
        self.assertEqual(sent[0]['kwargs']['max_tokens'], 6000)
        self.assertEqual(sent[0]['_stream_max_tokens'], 6000)

    async def test_default_agent_output_setting_is_32k_on_large_model(self):
        self.save({'output_reserve': 4096})
        sent, _summaries, chunks = await self.run_agent(
            window=131840, agent_output_setting=32768)
        self.assertEqual(sent[0]['kwargs']['max_tokens'], 32768)
        self.assertEqual(sent[0]['_stream_max_tokens'], 32768)
        self.assertIn('"generation_budget_tokens": 32768', chunks)

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
            self.save({'output_reserve': 8192})
            initial = self.store.get('owner', session_id='chat-a')
            self.store.save('owner', session_id='chat-a', overrides={'output_reserve': 512}, expected_revisions=initial['revisions'])
            with self.assertRaises(NotFound):
                self.store.get('owner', session_id='private')
            with self.assertRaises(ValueError):
                self.store.get('owner', session_id='chat-a', task_id='task')
            for identity in ('chat-a', 'chat-b'):
                sent, _, _ = await self.run_agent(session_id=identity, request_max_tokens=0)
                # Agent output has its own setting; chat policy still shapes
                # input/reserves, but no longer changes completion length.
                self.assertEqual(sent[0]['kwargs']['max_tokens'], 4096)
            self.assertEqual(self.store.get('owner')['effective']['output_reserve'], 8192)

    async def test_unknown_window_never_uses_user_requested_capacity(self):
        self.save({'requested_window': 131072})
        sent, summaries, chunks = await self.run_agent(window=0)
        self.assertFalse(sent)
        self.assertFalse(summaries)
        self.assertIn('context_compaction_failed', chunks)

    async def test_context_window_probe_timeout_fails_closed_with_diagnostic(self):
        self.save({'output_reserve': 1024})
        sent, _summaries, chunks = await self.run_agent(window_error=TimeoutError('private endpoint'))
        events = [json.loads(line[6:]) for line in chunks.splitlines()
                  if line.startswith('data: {')]
        failed = [event for event in events if event.get('type') == 'context_compaction_failed']
        self.assertFalse(sent)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['detail'], 'context_window_unavailable')
        self.assertNotIn('private endpoint', chunks)

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
        self.assertEqual(summaries[0][1]['max_tokens'], 1280)
        self.assertEqual(sent[0]['kwargs']['max_tokens'], 4096)
        self.assertIn('Original requirement', str(sent[0]['messages']))
        self.assertIn('Continue verification', str(sent[0]['messages']))
        self.assertLess(len(str(sent[0]['messages'])), len(str(history)))
        self.assertIn('"compacted"', chunks)

    async def test_stalled_utility_summary_leaves_time_for_selected_model(self):
        self.save({'trigger_percent': 60, 'target_percent': 45,
                   'recent_groups': 1, 'recent_tokens': 0,
                   'summary_tokens': 256, 'summary_timeout_seconds': 5,
                   'output_reserve': 1024})
        history = [{'role': 'user', 'content': 'Preserve a harmless goal'}] + [
            {'role': 'assistant', 'content': 'Measured harmless evidence. ' * 1000}
            for _ in range(10)
        ] + [{'role': 'user', 'content': 'Continue harmless verification'}]
        attempted = []

        async def summary(url, *_args, **_kwargs):
            attempted.append(url)
            if url == 'http://stalled.invalid/v1':
                await asyncio.sleep(30)
            return 'Preserve the harmless goal and measured evidence.'

        from src.context_policy import ContextPolicy
        # Exercise the fallback deadline without waiting for the production
        # minimum ten-minute compaction timeout.
        with patch.object(ContextPolicy, 'effective_summary_timeout_seconds',
                          property(lambda policy: policy.summary_timeout_seconds)):
            sent, _summaries, chunks = await self.run_agent(
                history, summary_impl=summary,
                utility_route=('http://stalled.invalid/v1', 'stalled-model', {}),
            )
        self.assertIn('http://stalled.invalid/v1', attempted)
        self.assertIn('http://fixture.invalid/v1', attempted)
        self.assertEqual(len(sent), 1)
        self.assertIn('"compacted"', chunks)

    async def test_single_slow_summary_keeps_most_of_its_deadline(self):
        self.save({'trigger_percent': 60, 'target_percent': 45,
                   'recent_groups': 1, 'recent_tokens': 0,
                   'summary_tokens': 256, 'summary_timeout_seconds': 5,
                   'output_reserve': 1024})
        history = [{'role': 'user', 'content': 'Preserve a harmless goal'}] + [
            {'role': 'assistant', 'content': 'Measured harmless evidence. ' * 1000}
            for _ in range(10)
        ] + [{'role': 'user', 'content': 'Continue harmless verification'}]

        async def summary(_url, *_args, **_kwargs):
            await asyncio.sleep(3)
            return 'Preserve the harmless goal and measured evidence.'

        sent, _summaries, chunks = await self.run_agent(history, summary_impl=summary)
        self.assertEqual(len(sent), 1)
        self.assertIn('"compacted"', chunks)

    async def test_summary_failure_emits_safe_diagnostic_code_not_provider_body(self):
        self.save({'trigger_percent': 60, 'target_percent': 45,
                   'recent_groups': 1, 'recent_tokens': 0,
                   'summary_tokens': 256, 'output_reserve': 1024})
        history = [{'role': 'user', 'content': 'Preserve a harmless goal'}] + [
            {'role': 'assistant', 'content': 'Measured harmless evidence. ' * 1000}
            for _ in range(10)
        ] + [{'role': 'user', 'content': 'Continue harmless verification'}]

        async def failed_summary(*_args, **_kwargs):
            raise RuntimeError('private provider body token: never echo')

        sent, _summaries, chunks = await self.run_agent(history, summary_impl=failed_summary)
        events = [json.loads(line[6:]) for line in chunks.splitlines()
                  if line.startswith('data: {')]
        failed = [event for event in events if event.get('type') == 'context_compaction_failed']
        self.assertFalse(sent)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['detail'], 'summarizer_error')
        self.assertNotIn('private provider body', chunks)

        async def reasoning_only(*_args, **_kwargs):
            raise HTTPException(502, 'Model returned reasoning but no answer content')

        sent, _summaries, chunks = await self.run_agent(history, summary_impl=reasoning_only)
        events = [json.loads(line[6:]) for line in chunks.splitlines()
                  if line.startswith('data: {')]
        failed = [event for event in events if event.get('type') == 'context_compaction_failed']
        self.assertFalse(sent)
        self.assertEqual(failed[0]['detail'], 'summarizer_no_answer')

        async def unreachable(*_args, **_kwargs):
            raise HTTPException(503, 'Cannot reach private-host: No route to host')

        sent, _summaries, chunks = await self.run_agent(history, summary_impl=unreachable)
        events = [json.loads(line[6:]) for line in chunks.splitlines()
                  if line.startswith('data: {')]
        failed = [event for event in events if event.get('type') == 'context_compaction_failed']
        self.assertFalse(sent)
        self.assertEqual(failed[0]['detail'], 'summarizer_transport_unavailable')
        self.assertNotIn('private-host', chunks)

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

    async def test_triggered_noop_compaction_does_not_dispatch_or_retry_forever(self):
        from src.context_policy import ContextPolicy
        from src.context_policy_runtime import shape_request
        messages = [{'role': 'user', 'content': 'evidence ' * 21000}]
        record = {'effective': ContextPolicy().to_dict(), 'revisions': {'owner': 1}}
        summarize = AsyncMock(return_value='summary')
        with patch(
            'src.context_policy_runtime.compact_working_context',
            new=AsyncMock(return_value=(messages, 'unchanged')),
        ) as compact:
            with self.assertRaisesRegex(ValueError, 'no reduction'):
                await shape_request(messages, [], record, 65536, summarize)
        self.assertEqual(compact.await_count, 1)
        self.assertEqual(summarize.await_count, 0)

    async def test_nominal_compaction_still_above_trigger_stops_instead_of_looping(self):
        from src.context_policy import ContextPolicy
        from src.context_policy_runtime import shape_request
        messages = [{'role': 'user', 'content': 'evidence ' * 21000}]
        still_full = [{'role': 'user', 'content': 'evidence ' * 20999}]
        record = {'effective': ContextPolicy().to_dict(), 'revisions': {'owner': 1}}
        summarize = AsyncMock(return_value='summary')
        with patch(
            'src.context_policy_runtime.compact_working_context',
            new=AsyncMock(return_value=(still_full, 'compacted')),
        ) as compact:
            with self.assertRaisesRegex(ValueError, 'still above trigger'):
                await shape_request(messages, [], record, 65536, summarize)
        self.assertEqual(compact.await_count, 1)
        self.assertEqual(summarize.await_count, 0)

    async def test_agent_does_not_dispatch_nominal_compaction_above_trigger(self):
        self.save({'trigger_percent': 75, 'target_percent': 50})
        history = [{'role': 'user', 'content': 'evidence ' * 21000}]
        still_full = [{'role': 'user', 'content': 'evidence ' * 20999}]
        with patch(
            'src.context_policy_runtime.compact_working_context',
            new=AsyncMock(return_value=(still_full, 'compacted')),
        ) as compact:
            sent, _, chunks = await self.run_agent(history)
        self.assertFalse(sent)
        self.assertEqual(compact.await_count, 1)
        events = [json.loads(line[6:]) for line in chunks.splitlines() if line.startswith('data: {')]
        failed = [event for event in events if event.get('type') == 'context_compaction_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['detail'], 'context_no_reduction')
        self.assertNotIn('"type": "compacted"', chunks)

    async def test_legacy_nominal_compaction_above_input_limit_stops_once(self):
        from src import agent_loop
        history = [{'role': 'user', 'content': 'evidence ' * 26000}]
        still_full = [{'role': 'user', 'content': 'evidence ' * 25999}]
        with patch.object(
            agent_loop, 'compact_working_context',
            new=AsyncMock(return_value=(still_full, 'compacted')),
        ) as compact:
            sent, _, chunks = await self.run_agent(history)
        events = [json.loads(line[6:]) for line in chunks.splitlines() if line.startswith('data: {')]
        failed = [event for event in events if event.get('type') == 'context_compaction_failed']
        self.assertEqual(compact.await_count, 1)
        self.assertFalse(sent)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['detail'], 'context_no_reduction')
        self.assertNotIn('"type": "compacted"', chunks)

    async def test_agent_reports_noop_compaction_without_model_dispatch(self):
        self.save({'trigger_percent': 75, 'target_percent': 50})
        history = [{'role': 'user', 'content': 'evidence ' * 21000}]
        with patch(
            'src.context_policy_runtime.compact_working_context',
            new=AsyncMock(return_value=(history, 'unchanged')),
        ):
            sent, summaries, chunks = await self.run_agent(history)
        self.assertFalse(sent)
        self.assertFalse(summaries)
        self.assertIn('context_compaction_failed', chunks)
        self.assertNotIn('"type": "compacted"', chunks)

    async def test_economic_noop_at_safety_limit_is_not_retried(self):
        from dataclasses import replace
        from src.context_compaction_economics import decide
        from src import agent_loop
        decision = replace(decide(
            at_boundary=True, used_tokens=60000, input_budget=65536,
            completed_boundaries=4, tokens_since_boundary=20000,
            cache_write_read_ratio=1,
        ), compact=True, target_tokens=10000)
        history = [{'role': 'user', 'content': 'evidence ' * 26000}]
        with patch.object(agent_loop, '_efficiency_enabled', side_effect=lambda key: key == 'online_context_compact'), \
             patch('src.context_compaction_economics.decide', return_value=decision), \
             patch('src.agent_context.working_context_compactable', return_value=True), \
             patch.object(agent_loop, 'compact_working_context', new=AsyncMock(return_value=(history, 'unchanged'))) as compact:
            sent, _, chunks = await self.run_agent(history)
        self.assertEqual(compact.await_count, 1)
        self.assertFalse(sent)
        self.assertIn('context_compaction_failed', chunks)

    async def test_economic_noop_below_safety_limit_defers_without_failure(self):
        from dataclasses import replace
        from src.context_compaction_economics import decide
        from src import agent_loop
        decision = replace(decide(
            at_boundary=True, used_tokens=12000, input_budget=65536,
            completed_boundaries=4, tokens_since_boundary=20000,
            cache_write_read_ratio=1,
        ), compact=True, target_tokens=2000)
        history = [{'role': 'user', 'content': 'evidence ' * 5000}]
        with patch.object(agent_loop, '_efficiency_enabled', side_effect=lambda key: key == 'online_context_compact'), \
             patch('src.context_compaction_economics.decide', return_value=decision), \
             patch('src.agent_context.working_context_compactable', return_value=True), \
             patch.object(agent_loop, 'compact_working_context', new=AsyncMock(return_value=(history, 'unchanged'))) as compact:
            sent, _, chunks = await self.run_agent(history)
        self.assertEqual(compact.await_count, 1)
        self.assertEqual(len(sent), 1)
        self.assertNotIn('context_compaction_failed', chunks)
        self.assertIn('native_no_reduction', chunks)

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
