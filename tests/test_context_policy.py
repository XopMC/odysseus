import asyncio
import unittest

from src.context_policy import ContextPolicy


class ContextPolicyTests(unittest.TestCase):
    def test_full_request_budget_and_hysteresis(self):
        policy = ContextPolicy(output_reserve=2000, safety_tokens=1000,
                               safety_percent=10, trigger_percent=75, target_percent=50)
        budget = policy.budget(20000, schema_tokens=500)
        self.assertEqual(budget.input_tokens, 15000)
        self.assertEqual(budget.trigger_messages, 10750)
        self.assertEqual(budget.target_messages, 7000)
        self.assertEqual(budget.hard_messages, 14500)
        self.assertEqual(budget.action(10749), 'continue')
        self.assertEqual(budget.action(10750), 'compact')
        self.assertEqual(budget.action(14501, auto_compact=False), 'blocked')
        self.assertEqual(budget.action(14500, auto_compact=False), 'continue')

    def test_user_window_cannot_expand_backend_capacity(self):
        policy = ContextPolicy(requested_window=131072)
        self.assertEqual(policy.budget(8192).window, 8192)
        self.assertEqual(ContextPolicy(requested_window=8192).budget(131072).window, 8192)
        self.assertEqual(policy.budget(131072, hard_input_max=10000).input_tokens, 10000)

    def test_target_below_tool_schemas_does_not_block_a_request_with_headroom(self):
        policy = ContextPolicy(output_reserve=4096, safety_tokens=0,
                               safety_percent=0, trigger_percent=75,
                               target_percent=15)
        budget = policy.budget(100000, schema_tokens=20000)
        self.assertEqual(budget.input_tokens, 95904)
        self.assertEqual(budget.trigger_messages, 51928)
        self.assertEqual(budget.target_messages, 1)
        self.assertEqual(budget.hard_messages, 75904)
        self.assertEqual(budget.action(8000), 'continue')

    def test_invalid_policies_and_no_space_fail_closed(self):
        for values in ({'auto_compact': 1}, {'trigger_percent': 50, 'target_percent': 50},
                       {'safety_percent': -1}, {'summary_tokens': True},
                       {'unapproved_provider': 'outside'}, {'recent_groups': 101}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ContextPolicy.from_dict(values)
        with self.assertRaises(ValueError):
            ContextPolicy().budget(2048)
        with self.assertRaises(ValueError):
            ContextPolicy().budget(8192, schema_tokens=6000)

    def test_roundtrip(self):
        policy = ContextPolicy(trigger_percent=65, target_percent=40, summary_tokens=300)
        self.assertEqual(ContextPolicy.from_dict(policy.to_dict()), policy)


class ConfiguredCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.agent_context import compact_working_context
        from src.model_context import estimate_tokens
        self.compact = compact_working_context
        self.estimate = estimate_tokens
        self.messages = [
            {'role': 'system', 'content': 'Never discard the acceptance criteria.'},
            {'role': 'user', 'content': 'Original goal: preserve the database.'},
            *[{'role': 'assistant', 'content': f'Step {n}: ' + 'completed investigation ' * 250} for n in range(8)],
            {'role': 'user', 'content': 'Now verify the result.'},
        ]
        self.prompts = []
        async def summarize(prompt):
            self.prompts.append(prompt)
            return 'Verified investigation; database must be preserved. Verification remains pending.'
        self.summarize = summarize

    async def test_policy_controls_actual_retention_and_preserves_goals(self):
        policy = ContextPolicy(recent_groups=1, recent_tokens=0, summary_tokens=256)
        output, status = await self.compact(self.messages, 6000, self.summarize,
                                           policy=policy, target_limit=3000)
        self.assertEqual(status, 'compacted')
        self.assertIn(self.messages[0], output)
        self.assertIn(self.messages[1], output)
        self.assertIn(self.messages[-1], output)
        self.assertIn(self.messages[-2], output)
        self.assertNotIn(self.messages[2], output)
        self.assertIn('under 256 tokens', self.prompts[0][0]['content'])
        self.assertLessEqual(self.estimate(output), 3000)

    async def test_disabled_and_impossible_target_preserve_original(self):
        output, status = await self.compact(self.messages, 6000, self.summarize,
            policy=ContextPolicy(auto_compact=False), target_limit=3000)
        self.assertEqual(status, 'disabled')
        self.assertIs(output, self.messages)
        self.assertFalse(self.prompts)
        output, status = await self.compact(self.messages, 6000, self.summarize,
            policy=ContextPolicy(recent_groups=7), target_limit=1000)
        self.assertEqual(status, 'uncompactable')
        self.assertIs(output, self.messages)

    async def test_zero_recent_budget_keeps_goals_but_no_optional_history(self):
        output, status = await self.compact(self.messages, 6000, self.summarize,
            policy=ContextPolicy(recent_groups=0, recent_tokens=0), target_limit=500)
        self.assertEqual(status, 'compacted')
        for message in (self.messages[0], self.messages[1], self.messages[-1]):
            self.assertIn(message, output)
        for message in self.messages[2:-1]:
            self.assertNotIn(message, output)
        self.assertIn('Step 7:', self.prompts[0][1]['content'])

    async def test_optional_group_cannot_exceed_recent_token_budget(self):
        output, status = await self.compact(self.messages, 6000, self.summarize,
            policy=ContextPolicy(recent_groups=0, recent_tokens=10), target_limit=500)
        self.assertEqual(status, 'compacted')
        self.assertNotIn(self.messages[-2], output)

    async def test_protected_content_over_target_never_calls_summarizer(self):
        messages = self.messages + [{'role': 'user', 'content': 'Mandatory evidence ' * 1000,
                                     '_context_pinned': True}]
        output, status = await self.compact(messages, 6000, self.summarize,
            policy=ContextPolicy(recent_groups=0, recent_tokens=0), target_limit=500)
        self.assertEqual(status, 'uncompactable')
        self.assertIs(output, messages)
        self.assertFalse(self.prompts, 'An impossible target must not spend an inference request')

    async def test_pinned_native_exchange_remains_atomic(self):
        exchange = [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'tool1', 'type': 'function', 'function': {'name': 'check', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tool1', 'content': 'verified evidence', '_context_pinned': True}]
        messages = self.messages[:2] + exchange + self.messages[2:]
        output, status = await self.compact(messages, 6000, self.summarize,
            policy=ContextPolicy(recent_groups=1, recent_tokens=0), target_limit=3000)
        self.assertEqual(status, 'compacted')
        self.assertIn(exchange[0], output)
        self.assertIn(exchange[1], output)

    async def test_oversized_summary_is_bounded_with_explicit_marker(self):
        async def huge(prompt):
            return 'Unsupported claim. ' * 500
        output, status = await self.compact(self.messages, 6000, huge,
            policy=ContextPolicy(summary_tokens=128, recent_groups=0, recent_tokens=0),
            target_limit=3000)
        self.assertEqual(status, 'compacted')
        checkpoint = next(m for m in output if m.get('_agent_working_summary'))
        self.assertIn('summary exceeded its configured budget', checkpoint['content'])
