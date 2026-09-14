"""Configuration-scoped evidence from two small synthetic local model requests.

The public API must authenticate an interactive owner before calling ``probe``.
This module never accepts arbitrary prompts, URLs, credentials, shell commands,
or real tools. The sole tool is a pure in-memory challenge/response. It uses the
existing Team SSE transport and physical backend slot, not a parallel client.

External probes are explicitly unsupported: the existing budget ledger needs a
live task-worker lease. Discovery/confirmation cannot manufacture that authority.
No paid request is sent, even if a caller has some unrelated task approval.

Results are observations for the exact resolved configuration and probe version,
not global model capability guarantees, speed benchmarks or persistent grants.
No endpoint settings, model defaults, histories or task workers are modified.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import os
import secrets
import time

from src.constants import ENGINEERING_PROBE_MAX_TOKENS, ENGINEERING_PROBE_TIMEOUT_SECONDS
from src import team_config, team_model
from src.team_store import Conflict, _text


PROBE_VERSION = 'engineering-local-model-v1'
_TOOL = {'type': 'function', 'function': {'name': 'engineering_probe_echo',
    'description': 'Synthetic capability test only. Echo the supplied challenge; no external effects.',
    'parameters': {'type': 'object', 'properties': {'challenge': {'type': 'string'}},
                   'required': ['challenge'], 'additionalProperties': False}}}
_EXTERNAL_REASON = ('External model probes are unsupported until an explicit task consent and '
                    'budgeted probe lease are integrated; no provider request was sent.')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _owner_allowed(owner):
    from src.tool_execution import _current_agent_privileges
    privileges = _current_agent_privileges(owner)
    return isinstance(privileges, dict) and privileges.get('can_use_agent') is True


def _resolve(owner, endpoint_id, model):
    _text(owner, 'owner'); _text(endpoint_id, 'endpoint id'); _text(model, 'model')
    if os.environ.get('ODYSSEUS_ENGINEERING_ENABLED') != '1' or not _owner_allowed(owner):
        raise PermissionError('Model capability probes are disabled for this owner')
    route = team_config.resolve(owner, endpoint_id, model)
    # Resolve is server-owned; do not silently probe a fallback/renamed model.
    if (route.get('endpoint_id') != endpoint_id or route.get('model') != model
            or type(route.get('local')) is not bool
            or not isinstance(route.get('url'), str)
            or not isinstance(route.get('resource_group'), str) or not route['resource_group']
            or not isinstance(route.get('headers'), dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in route['headers'].items())):
        raise ValueError('Configured model route is invalid or does not match the selected model')
    return route


def _scope(owner, route):
    schema_digest = _digest(_TOOL)
    identity = {key: route[key] for key in ('endpoint_id', 'model', 'url', 'headers', 'local', 'resource_group')}
    # Secret-bearing configured headers are hashed in memory, never returned,
    # persisted or logged. Credential rotation invalidates prior observations.
    fingerprint = _digest({'owner': owner, 'route': identity, 'schema': schema_digest,
        'probe_version': PROBE_VERSION, 'max_tokens': ENGINEERING_PROBE_MAX_TOKENS,
        'timeout_seconds': ENGINEERING_PROBE_TIMEOUT_SECONDS, 'temperature': .2,
        'transport': 'team_model.complete:sse-native-v1'})
    return {'endpoint_id': route['endpoint_id'], 'model': route['model'],
            'config_digest': fingerprint, 'schema_digest': schema_digest, 'probe_version': PROBE_VERSION}


def describe(owner, endpoint_id, model):
    """Read-only API metadata: exact configuration digest for explicit confirmation.

    Uses only existing configured/discovered models. No inference HTTP request,
    model discovery refresh, task mutation or credential is returned.
    """
    route = _resolve(owner, endpoint_id, model)
    return {'scope': _scope(owner, route), 'supported': route['local'],
            'reason': 'Two synthetic local streaming requests; no real tools.' if route['local'] else _EXTERNAL_REASON,
            'max_requests': 2, 'max_output_tokens_per_request': ENGINEERING_PROBE_MAX_TOKENS,
            'deadline_seconds': ENGINEERING_PROBE_TIMEOUT_SECONDS}


def _valid_usage(value):
    return (isinstance(value, dict)
            and all(type(value.get(key)) is int and value[key] >= 0 for key in ('prompt_tokens', 'completion_tokens'))
            and value['completion_tokens'] <= ENGINEERING_PROBE_MAX_TOKENS)


def _native_call(message, challenge):
    calls = message.get('tool_calls')
    if not isinstance(calls, list) or len(calls) != 1:
        return None
    call = calls[0]
    if (not isinstance(call, dict) or call.get('type') != 'function'
            or not isinstance(call.get('id'), str) or not 0 < len(call['id']) <= 128
            or not isinstance(call.get('function'), dict)
            or call['function'].get('name') != _TOOL['function']['name']):
        return None
    try:
        arguments = json.loads(call['function']['arguments'])
    except (ValueError, TypeError, KeyError):
        return None
    return call if arguments == {'challenge': challenge} else None


async def probe(owner, endpoint_id, model, *, confirmation=False, expected_config_digest=None, check_active=None):
    """Run a confirmed synthetic local probe; return scoped, non-secret evidence.

    ``expected_config_digest`` comes from ``describe`` and is mandatory. The
    endpoint, owner and feature gates are re-resolved before each transport.
    Initial stale configuration raises Conflict; mid-probe revocation/config
    change returns ``status=stale`` and never sends the next request.

    Capability values: True means observed in this exact bounded probe, False
    means its required behavior was not observed, None means not tested. No
    automatic fallback, feature enabling or capability persistence occurs.
    ``check_active`` is an optional server-owned synchronous/asynchronous
    cancellation fence. It runs after acquiring the physical backend slot,
    before transport and after response; False or an exception stops progress.
    """
    if confirmation is not True:
        raise PermissionError('Explicit confirmation is required for model inference probes')
    if check_active is not None and not callable(check_active):
        raise ValueError('check_active must be a server-owned callable')
    route = _resolve(owner, endpoint_id, model)
    scope = _scope(owner, route)
    if not isinstance(expected_config_digest, str) or expected_config_digest != scope['config_digest']:
        raise Conflict('Model configuration changed; refresh and confirm the current probe')
    measurements = {'requests': 0, 'completed_requests': 0, 'content_callbacks': 0,
                    'rounds': [], 'elapsed_seconds': 0.0}
    result = {'scope': scope, 'status': 'unsupported', 'reason': _EXTERNAL_REASON,
              'capabilities': {'streaming': None, 'native_tools': None, 'tool_roundtrip': None, 'usage': None},
              'measurements': measurements}
    if not route['local']:
        return result
    capabilities = result['capabilities']
    started = time.monotonic()

    async def on_delta(text):
        # Observability only. No model-generated content is exposed in results.
        measurements['content_callbacks'] += 1

    def current_route():
        try:
            fresh = _resolve(owner, endpoint_id, model)
        except ValueError as exc:
            raise Conflict('Selected model is no longer configured or available') from exc
        if _scope(owner, fresh) != scope or fresh['local'] is not True:
            raise Conflict('Model configuration changed during the probe')
        return fresh

    async def call(messages, tools):
        async def active():
            if check_active is not None:
                allowed = check_active()
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if allowed is False:
                    raise PermissionError('Probe operation was cancelled')
        # The transport's slot is reentrant for this task. Fence again after a
        # queued wait so a cancelled durable operation cannot dispatch later.
        async with team_model.resource_slot(current_route()['resource_group']):
            await active()
            fresh = current_route()
            measurements['requests'] += 1
            observed = await team_model.complete(fresh, messages, tools,
                max_tokens=ENGINEERING_PROBE_MAX_TOKENS, on_delta=on_delta)
            await active()
        measurements['completed_requests'] += 1
        current_route()
        capabilities['streaming'] = True  # Existing transport requires complete SSE [DONE].
        usage_ok = _valid_usage(observed.get('usage'))
        capabilities['usage'] = usage_ok if capabilities['usage'] is None else capabilities['usage'] and usage_ok
        detail = {'usage_valid': usage_ok}
        if usage_ok:
            detail['usage'] = {key: observed['usage'][key] for key in ('prompt_tokens', 'completion_tokens')}
        for field in ('duration', 'ttft'):
            value = observed.get(field)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                detail[field + '_seconds'] = value
        measurements['rounds'].append(detail)
        return observed['message']

    try:
        async with asyncio.timeout(ENGINEERING_PROBE_TIMEOUT_SECONDS):
            challenge, answer = secrets.token_hex(16), 'PROBE_RESULT_' + secrets.token_hex(16)
            messages = [{'role': 'system', 'content':
                'This is a synthetic protocol capability test. Call engineering_probe_echo exactly once with '
                'the supplied challenge. Do not call any other tool. After its tool result, respond with '
                'only the result string, without quotes, explanation or additional tools.'},
                {'role': 'user', 'content': json.dumps({'challenge': challenge})}]
            first = await call(messages, [copy.deepcopy(_TOOL)])
            native = _native_call(first, challenge)
            capabilities['native_tools'] = native is not None
            if native is None:
                result.update(status='partial', reason='The exact safe native tool call was not observed; no tool was executed.')
                return result
            # A fresh result value was never in the first prompt. Successful
            # echo demonstrates consumption of a real native tool-result turn.
            messages += [first, {'role': 'tool', 'tool_call_id': native['id'],
                                 'content': json.dumps({'result': answer})}]
            second = await call(messages, [])
            capabilities['tool_roundtrip'] = (not second.get('tool_calls') and
                isinstance(second.get('content'), str) and second['content'].strip() == answer)
            if all(value is True for value in capabilities.values()):
                result.update(status='passed', reason='Synthetic native tool roundtrip, completed SSE and valid usage observed.')
            else:
                result.update(status='partial', reason='Tool-result echo or valid usage was not observed; inspect the individual checks.')
    except (Conflict, PermissionError):
        result.update(status='stale', reason='Configuration or permission changed during the probe; remaining requests were not sent.')
    except TimeoutError:
        result.update(status='failed', reason='Probe deadline exceeded; capability checks remain unverified where incomplete.')
    except Exception:
        # Do not expose provider error bodies, URL credentials or library reprs.
        result.update(status='failed', reason='Configured model transport failed; incomplete checks are not capability passes.')
    finally:
        measurements['elapsed_seconds'] = time.monotonic() - started
    return result
