import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import src.model_context as context
import src.model_request_gate as gate
from src.model_context import ServingCapacity

BASE = 'http://192.168.50.4:1234'
URL = BASE + '/v1/chat/completions'


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    context.clear_model_context_cache()
    monkeypatch.setattr(context, '_configured_endpoint_kind', lambda _: 'local')
    monkeypatch.setenv('ODYSSEUS_LOCAL_MODEL_GATE', 'true')
    yield
    context.clear_model_context_cache()


def native(monkeypatch, config, *, instance_id='worker', key='worker', other=None):
    calls = []
    items = [{'key': key, 'loaded_instances': [{'id': instance_id, 'config': config}]}]
    if other:
        items.append(other)

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={'models': items}, request=httpx.Request('GET', url))

    monkeypatch.setattr(context.httpx, 'get', get)
    return calls


def test_two_parallel_requests_do_not_each_budget_the_entire_128k_pool(monkeypatch):
    native(monkeypatch, {'context_length': 131072, 'parallel': 2})
    assert context.budget_context_for_model(URL, 'worker') == 65536
    cap = context.get_serving_capacity(URL, 'worker')
    assert (cap.parallel, cap.loaded_context_length, cap.context_length) == (2, 131072, 65536)


def test_explicit_per_slot_window_is_not_divided_twice(monkeypatch):
    native(monkeypatch, {'context_length': 131072, 'parallel': 2, 'context_length_per_slot': 131072})
    assert context.get_context_length(URL, 'worker') == 131072


def test_instance_and_its_single_model_key_share_one_gate_identity(monkeypatch):
    calls = native(monkeypatch, {'context_length': 131072, 'parallel': 2}, instance_id='worker:3')
    a = context.get_serving_capacity(URL, 'worker')
    b = context.get_serving_capacity(URL, 'worker:3')
    assert a == b
    assert calls == [BASE + '/api/v1/models']


def test_explicit_refresh_changes_both_window_and_permits(monkeypatch):
    native(monkeypatch, {'context_length': 131072, 'parallel': 2})
    assert context.get_context_length(URL, 'worker') == 65536
    native(monkeypatch, {'context_length': 8192, 'parallel': 1})
    context.clear_model_context_cache(BASE + '/v1')
    assert context.get_serving_capacity(URL, 'worker').context_length == 8192
    assert context.get_serving_capacity(URL, 'worker').parallel == 1


def test_failed_native_probe_is_cached_briefly_not_once_per_child(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError('offline test')
    monkeypatch.setattr(context.httpx, 'get', get)
    assert context._lmstudio_loaded_context(BASE, 'a') is None
    assert context._lmstudio_loaded_context(BASE, 'b') is None
    assert calls == [BASE + '/api/v1/models']


def test_offline_discovery_does_not_lock_a_different_endpoint_cache(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    healthy = 'http://192.168.50.90:1234'
    context._local_context_cache[(healthy + '/v1', 'ready')] = (
        context.time.monotonic(), 300, (8192, True))
    def get(url, **kwargs):
        entered.set()
        assert release.wait(2)
        raise httpx.ConnectError('offline test')
    monkeypatch.setattr(context.httpx, 'get', get)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(context._lmstudio_loaded_context, BASE, 'worker')
        try:
            assert entered.wait(1)
            ready = executor.submit(context.get_context_length, healthy + '/v1', 'ready')
            assert ready.result(timeout=.5) == 8192
        finally:
            release.set()
        assert pending.result(timeout=1) is None


def test_llama_slots_are_per_sequence_and_aliases_share_the_pool(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        payload = [{'n_ctx': 65536}, {'n_ctx': 32768}] if url.endswith('/slots') else {}
        return httpx.Response(200, json=payload, request=httpx.Request('GET', url))

    monkeypatch.setattr(context.httpx, 'get', get)
    cap = context.get_serving_capacity(URL, 'worker')
    assert (cap.context_length, cap.parallel, cap.instance_id) == (32768, 2, '@llama_slots')


def test_remote_model_window_is_not_divided(monkeypatch):
    monkeypatch.setattr(context, '_configured_endpoint_kind', lambda _: 'api')
    assert context.get_context_length('https://api.openai.com/v1', 'gpt-4o') == 128000


@pytest.mark.parametrize('parallel', [None, True, -2, 0, '2'])
def test_invalid_parallelism_never_creates_extra_permits(monkeypatch, parallel):
    native(monkeypatch, {'context_length': 131072, 'parallel': parallel})
    assert context.get_serving_capacity(URL, 'worker').parallel == 1


@pytest.mark.asyncio
async def test_parent_and_children_share_two_real_permits(monkeypatch):
    import src.llm_core as llm
    monkeypatch.setenv('ODYSSEUS_TEAM_ENABLED', 'true')
    monkeypatch.setattr(gate, 'get_serving_capacity',
                        lambda *_: ServingCapacity('worker', 65536, 131072, 2, 'test'))
    release = asyncio.Event()
    two = asyncio.Event()
    active = maximum = entered = 0

    async def request(kind):
        nonlocal active, maximum, entered
        async with llm._local_model_slot(URL, 'worker', kind):
            active += 1
            entered += 1
            maximum = max(maximum, active)
            if entered == 2:
                two.set()
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(request(kind)) for kind in ('foreground', 'subagent', 'subagent')]
    try:
        await asyncio.wait_for(two.wait(), 1)
        await asyncio.sleep(0)
        assert active == entered == 2
        release.set()
        await asyncio.gather(*tasks)
        assert maximum == 2 and entered == 3
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_single_slot_waiter_cancel_does_not_leak_permit(monkeypatch):
    monkeypatch.setattr(gate, 'get_serving_capacity',
                        lambda *_: ServingCapacity('worker', 8192, 8192, 1, 'test'))
    entered = asyncio.Event()

    async def waiting():
        async with gate.model_request_slot(URL, 'worker'):
            entered.set()

    async with gate.model_request_slot(URL, 'worker'):
        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(.01)
        assert not entered.is_set()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    await asyncio.wait_for(waiting(), 1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_independent_instances_and_ports_remain_parallel(monkeypatch):
    monkeypatch.setattr(gate, 'get_serving_capacity',
                        lambda _url, model: ServingCapacity(model, 8192, 8192, 1, 'test'))
    # Nested acquisition in the same task is reentrant; independent identities
    # also enter while the first instance holds its one permit.
    async with gate.model_request_slot(URL, 'worker'):
        async with gate.model_request_slot(URL, 'worker'):
            async with gate.model_request_slot(URL, 'worker:3'):
                async with gate.model_request_slot(URL.replace(':1234', ':49285'), 'worker'):
                    pass


@pytest.mark.asyncio
async def test_decreasing_reported_slots_does_not_replace_an_owned_pool(monkeypatch):
    parallel = 2
    monkeypatch.setattr(gate, 'get_serving_capacity',
                        lambda *_: ServingCapacity('worker', 8192, 16384, parallel, 'test'))
    acquired = asyncio.Event()
    async def waiting():
        async with gate.model_request_slot(URL, 'worker'):
            acquired.set()
    async with gate.model_request_slot(URL, 'worker'):
        parallel = 1
        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(.02)
        assert not acquired.is_set()
    await asyncio.wait_for(waiter, 1)
    assert acquired.is_set()


def test_default_agent_provider_idle_timeout_allows_ten_minute_prefill():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS['agent_stream_timeout_seconds'] == 600


def test_legacy_local_idle_timeout_is_ten_minutes_but_longer_choices_survive(monkeypatch):
    from src import agent_loop
    monkeypatch.setattr(agent_loop, 'is_local_endpoint', lambda url: url == URL)
    monkeypatch.setattr(agent_loop, 'get_setting', lambda *_: 300)
    assert agent_loop._agent_provider_idle_timeout(URL) == 600
    assert agent_loop._agent_provider_idle_timeout('https://cloud.example/v1') == 300
    monkeypatch.setattr(agent_loop, 'get_setting', lambda *_: 1200)
    assert agent_loop._agent_provider_idle_timeout(URL) == 1200
