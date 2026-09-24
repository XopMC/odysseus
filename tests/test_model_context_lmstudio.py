"""Serving windows must come from the selected LM Studio instance, not its model name."""
import httpx
import pytest

import src.model_context as context

BASE = 'http://192.168.50.90:1234'
MODEL = 'qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive'


@pytest.fixture(autouse=True)
def clear_context_caches_between_tests():
    context.clear_model_context_cache()


def catalog(monkeypatch, models, *, status=200):
    calls = []
    monkeypatch.setattr(context, '_configured_endpoint_kind', lambda _: 'local')

    def get(url, **kwargs):
        calls.append(url)
        payload = {'models': models} if url == BASE+'/api/v1/models' else {'data': [{'id': MODEL}]}
        return httpx.Response(status if url == BASE+'/api/v1/models' else 200,
                              json=payload, request=httpx.Request('GET', url))

    monkeypatch.setattr(context.httpx, 'get', get)
    return calls


def entry(key=MODEL, instances=None):
    return {'key': key, 'max_context_length': 262144,
            'loaded_instances': instances if instances is not None else []}


def instance(size, identifier=MODEL):
    return {'id': identifier, 'config': {'context_length': size}}


@pytest.mark.parametrize('size', [8192, 262144])
def test_loaded_instance_overrides_family_fallback(monkeypatch, size):
    calls = catalog(monkeypatch, [entry(instances=[instance(size)])])
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == size
    assert BASE+'/api/v1/models' in calls


def test_exact_instance_alias_has_priority_over_model_key(monkeypatch):
    catalog(monkeypatch, [entry(instances=[instance(32768, 'first'), instance(65536, MODEL)])])
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == 65536


def test_model_key_with_multiple_instances_uses_safe_common_window(monkeypatch):
    catalog(monkeypatch, [entry(instances=[instance(32768, 'first'), instance(65536, 'second')])])
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == 32768


def test_unrelated_loaded_model_is_not_used(monkeypatch):
    catalog(monkeypatch, [entry(key='other', instances=[instance(8192, 'other')])])
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == 131072


@pytest.mark.parametrize('bad', [True, -1, 0, '262144', None])
def test_invalid_instance_windows_do_not_override_fallback(monkeypatch, bad):
    catalog(monkeypatch, [entry(instances=[instance(bad)])])
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == 131072


def test_unloaded_maximum_is_not_a_serving_window(monkeypatch):
    catalog(monkeypatch, [entry()])
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == 131072


def test_unavailable_native_api_keeps_openai_compatibility(monkeypatch):
    catalog(monkeypatch, [], status=404)
    assert context.get_context_length(BASE+'/v1/chat/completions', MODEL) == 131072
