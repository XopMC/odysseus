"""Agent request shaping for explicitly saved context profiles.

No default migration, endpoint selection, provider consent or execution recovery.
Callers must persist checkpoints and recheck policy identity before dispatch.
"""
import os
import math

from src.context_policy import ContextPolicy
from src.context_policy_store import ContextPolicyStore
from src.team_store import NotFound
from src.agent_context import compact_working_context, schema_token_estimate
from src.model_context import estimate_tokens


def enabled():
    return (os.environ.get('ODYSSEUS_ENGINEERING_ENABLED') == '1'
            and os.environ.get('ODYSSEUS_CONTEXT_POLICY_ENABLED') == '1')


def owner_policy(owner, *, session_id=None):
    if not enabled() or not owner:
        return None
    from src.team_runtime import get_runtime
    try:
        record = ContextPolicyStore(get_runtime().store).get(owner, session_id=session_id or '')
    except NotFound as exc:
        raise ValueError('Chat context policy is unavailable') from exc
    if not record['configured']:
        return None
    if not record['valid']:
        raise ValueError('Stored context policy is invalid; update the profile')
    return record


async def shape_request(messages, tools, record, window, summarize, *, calibration=1., hard_input_max=None):
    policy = ContextPolicy.from_dict(record['effective'])
    if not math.isfinite(calibration) or calibration <= 0:
        raise ValueError('Invalid context token calibration')
    budget = policy.budget(window, schema_tokens=schema_token_estimate(tools), hard_input_max=hard_input_max)
    before = math.ceil(estimate_tokens(messages) * calibration)
    action = budget.action(before, auto_compact=policy.auto_compact)
    shaped, status = messages, 'unchanged'
    if action == 'blocked':
        raise ValueError('Context is full and automatic compaction is disabled')
    if action == 'compact':
        shaped, status = await compact_working_context(messages,
            max(1, math.floor(budget.trigger_messages / calibration)), summarize,
            policy=policy, target_limit=max(1, math.floor(budget.target_messages / calibration)))
        if status not in {'compacted', 'unchanged'}:
            raise ValueError('Configured context checkpoint could not preserve the required history')
    after = math.ceil(estimate_tokens(shaped) * calibration)
    if after > budget.hard_messages:
        raise ValueError('Context exceeds the configured input budget')
    return shaped, {'status': status, 'before_tokens': before + budget.schema_tokens,
        'after_tokens': after + budget.schema_tokens, 'source': 'estimated',
        'window': budget.window, 'input_budget': budget.input_tokens,
        'trigger_messages': budget.trigger_messages, 'target_messages': budget.target_messages,
        'output_reserve': budget.output_reserve, 'safety_tokens': budget.safety_tokens,
        'revisions': record['revisions']}
