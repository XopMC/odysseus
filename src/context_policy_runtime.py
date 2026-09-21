"""Agent request shaping for explicitly saved context profiles.

No default migration, endpoint selection, provider consent or execution recovery.
Callers must persist checkpoints and recheck policy identity before dispatch.
"""
import os
import math
import hashlib
import json
from dataclasses import replace

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
    effective_target = budget.target_messages
    effective_recent_groups = policy.recent_groups
    effective_recent_tokens = policy.recent_tokens
    summary_cache = {}

    async def summarize_once(prompt):
        key = hashlib.sha256(json.dumps(
            prompt, sort_keys=True, ensure_ascii=False, default=str,
        ).encode('utf-8')).hexdigest()
        if key not in summary_cache:
            summary_cache[key] = await summarize(prompt)
        return summary_cache[key]
    if action == 'blocked':
        raise ValueError('Context is full and automatic compaction is disabled')
    if action == 'compact':
        shaped, status = await compact_working_context(messages,
            max(1, math.floor(budget.trigger_messages / calibration)), summarize_once,
            policy=policy, target_limit=max(1, math.floor(budget.target_messages / calibration)))
        if status == 'uncompactable':
            # A very aggressive configured target can be smaller than pinned
            # Goal/tool groups even though a safe checkpoint still fits below
            # the trigger. Relax only the effective target for this revision;
            # keep the configured value visible and never cross the trigger.
            relaxed = max(
                budget.target_messages,
                min(budget.trigger_messages - 1, math.floor(budget.trigger_messages * .9)),
            )
            if relaxed > budget.target_messages:
                effective_target = relaxed
                shaped, status = await compact_working_context(
                    messages,
                    max(1, math.floor(budget.trigger_messages / calibration)),
                    summarize_once,
                    policy=policy,
                    target_limit=max(1, math.floor(relaxed / calibration)),
                )
            if status == 'uncompactable':
                # The configured recent tail is optional retention. A few huge
                # recent tool groups can exceed even the relaxed safe target
                # after a valid summary was produced. Preserve every pinned
                # Goal/tool pair and the explicit checkpoint, but fold the
                # optional tail for this revision instead of restarting the
                # long Goal with exactly the same impossible shape.
                effective_recent_groups = 0
                effective_recent_tokens = 0
                minimal_policy = replace(policy, recent_groups=0, recent_tokens=0)
                shaped, status = await compact_working_context(
                    messages,
                    max(1, math.floor(budget.trigger_messages / calibration)),
                    summarize_once,
                    policy=minimal_policy,
                    target_limit=max(1, math.floor(effective_target / calibration)),
                )
        if status not in {'compacted', 'unchanged'}:
            raise ValueError(
                f'Configured context checkpoint could not preserve the required history ({status})'
            )
    after = math.ceil(estimate_tokens(shaped) * calibration)
    if after > budget.hard_messages:
        raise ValueError('Context exceeds the configured input budget')
    return shaped, {'status': status, 'before_tokens': before + budget.schema_tokens,
        'after_tokens': after + budget.schema_tokens, 'source': 'estimated',
        'window': budget.window, 'input_budget': budget.input_tokens,
        'trigger_messages': budget.trigger_messages,
        'target_messages': budget.target_messages,
        'effective_target_messages': effective_target,
        'effective_recent_groups': effective_recent_groups,
        'effective_recent_tokens': effective_recent_tokens,
        'output_reserve': budget.output_reserve, 'safety_tokens': budget.safety_tokens,
        'revisions': record['revisions']}
