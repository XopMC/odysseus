"""Bounded native-tool model transport for persisted teams.

Credentials are supplied by the configured endpoint resolver, never stored in
task metadata. Reservations/leases are enforced by the caller before this runs.
"""
import asyncio
import json
import time

import httpx

_locks = {}


async def complete(route, messages, tools, *, max_tokens=4096, on_delta=None):
    # Retry only failures establishing a connection: no HTTP request was sent.
    # A partial stream/read timeout may have been billed and is never replayed.
    # Provider selection and the existing budget reservation remain unchanged.
    for attempt in range(3):
        try:
            return await _complete_once(route, messages, tools, max_tokens=max_tokens, on_delta=on_delta)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == 2:
                raise
            await asyncio.sleep(1 << attempt)


async def _complete_once(route, messages, tools, *, max_tokens=4096, on_delta=None):
    started = time.monotonic()
    payload = {'model': route['model'], 'messages': messages, 'temperature': .2,
               'max_tokens': max_tokens, 'stream': True,
               'stream_options': {'include_usage': True}}
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = 'auto'
    text, calls, usage, size = [], {}, {}, 0
    first, finished = None, False
    pending_delta, last_delta = [], started
    # Shared process lock is also used for normal model traffic once wired into
    # llm_core; independent host groups do not serialize each other.
    async with resource_slot(route['resource_group'] if route['local'] else None):
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False) as client:
            async with client.stream('POST', route['url'], headers=route['headers'], json=payload) as response:
                if response.status_code != 200:
                    raise RuntimeError('Model HTTP ' + str(response.status_code))
                async for line in response.aiter_lines():
                    size += len(line.encode())
                    if size > 2 * 1024 * 1024:
                        raise RuntimeError('Model response exceeded bounded stream size')
                    if not line.startswith('data:'):
                        continue
                    body = line[5:].strip()
                    if body == '[DONE]':
                        finished = True
                        break
                    try:
                        event = json.loads(body)
                    except ValueError:
                        continue
                    if event.get('error'):
                        raise RuntimeError('Model reported an error')
                    if isinstance(event.get('usage'), dict):
                        usage = event['usage']
                    for choice in event.get('choices', []):
                        delta = choice.get('delta') or {}
                        piece = delta.get('content') or ''
                        if piece:
                            first = first or time.monotonic()
                            text.append(piece)
                            if on_delta:
                                pending_delta.append(piece)
                                if time.monotonic() - last_delta >= .08:
                                    await on_delta(''.join(pending_delta))
                                    pending_delta.clear()
                                    last_delta = time.monotonic()
                        for item in delta.get('tool_calls', []):
                            index = item.get('index', 0)
                            if not isinstance(index, int) or index < 0 or index >= 16:
                                raise RuntimeError('Invalid tool-call index')
                            call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                            if item.get('id'):
                                call['id'] = item['id']
                            function = item.get('function') or {}
                            call['function']['name'] += function.get('name') or ''
                            call['function']['arguments'] += function.get('arguments') or ''
    if not finished:
        raise RuntimeError('Model stream disconnected before completion')
    if on_delta and pending_delta:
        await on_delta(''.join(pending_delta))
    if not text and not calls:
        raise RuntimeError('Model returned neither an answer nor a tool call')
    message = {'role': 'assistant', 'content': ''.join(text)}
    if calls:
        ordered = [calls[index] for index in sorted(calls)]
        if any(not call['id'] or not call['function']['name'] for call in ordered):
            raise RuntimeError('Incomplete native tool call')
        message['tool_calls'] = ordered
    elapsed = time.monotonic() - started
    return {'message': message, 'usage': usage, 'duration': elapsed,
            'ttft': first - started if first else None,
            'generation_tps': usage.get('completion_tokens', 0) / elapsed if elapsed else None}


class resource_slot:
    def __init__(self, group):
        self.group, self.lock = group, None

    async def __aenter__(self):
        if self.group:
            loop = asyncio.get_running_loop()
            self.lock = _locks.setdefault((loop, self.group), {'lock': asyncio.Lock(), 'owner': None, 'depth': 0})
            task = asyncio.current_task()
            if self.lock['owner'] is not task:
                await self.lock['lock'].acquire()
                self.lock['owner'] = task
            self.lock['depth'] += 1

    async def __aexit__(self, *args):
        if self.lock:
            self.lock['depth'] -= 1
            if not self.lock['depth']:
                self.lock['owner'] = None
                self.lock['lock'].release()
