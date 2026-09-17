"""Context occupancy is per request, never accumulated agent billing usage."""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.models import ChatMessage
from routes.history import history_routes
from src import agent_runs, model_context


def _snapshot(**overrides):
    return {
        "used_tokens": 82000, "prompt_tokens": 81000,
        "context_length": 262144, "model": "mac-qwen",
        "source": "backend", "round": 12,
        "auto_compact_threshold": 85, "compactions": 1,
        **overrides,
    }


def _publish(run, data):
    agent_runs._publish(run, "data: " + json.dumps({"type": "context_usage", "data": data}) + "\n\n")


def test_live_snapshot_is_exact_run_scoped_and_copied(monkeypatch):
    old, new, other = agent_runs._Run(), agent_runs._Run(), agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": new, "b": other})
    _publish(old, _snapshot(used_tokens=99999))
    assert agent_runs.get_context_usage("a") is None


    _publish(new, _snapshot())
    result = agent_runs.get_context_usage("a")
    assert result["used_tokens"] == 82000
    result["used_tokens"] = 1
    assert agent_runs.get_context_usage("a")["used_tokens"] == 82000
    assert agent_runs.get_context_usage("b") is None
    new.status = "done"
    assert agent_runs.get_context_usage("a") is None


@pytest.mark.parametrize("threshold", [70, 70.2, 85.0])
def test_actual_compaction_threshold_may_be_fractional(monkeypatch, threshold):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    _publish(run, _snapshot(auto_compact_threshold=threshold))
    assert agent_runs.get_context_usage("a")["auto_compact_threshold"] == threshold


def test_disabled_compaction_state_survives_context_snapshot(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, '_RUNS', {'a': run})
    _publish(run, _snapshot(auto_compact_enabled=False))
    assert agent_runs.get_context_usage('a')['auto_compact_enabled'] is False
    normalized = agent_runs.normalize_context_usage(_snapshot(auto_compact_enabled='false'))
    assert 'auto_compact_enabled' not in normalized


@pytest.mark.parametrize("invalid", [None, [], {"used_tokens": 9}, _snapshot(used_tokens=True), _snapshot(context_length=0), _snapshot(source="billing"), _snapshot(source=[])])
def test_invalid_context_events_do_not_replace_valid_snapshot(monkeypatch, invalid):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    _publish(run, _snapshot())
    _publish(run, invalid)
    agent_runs._publish(run, "data: [DONE]\n\n")
    agent_runs._publish(run, "data: {broken\n\n")
    assert agent_runs.get_context_usage("a")["used_tokens"] == 82000


def test_context_ledger_rejects_lower_measurement_without_compaction(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    _publish(run, _snapshot(used_tokens=100000, compactions=2))
    _publish(run, _snapshot(used_tokens=7000, compactions=2))
    current = agent_runs.get_context_usage("a")
    assert current["used_tokens"] == 100000
    assert current["context_revision"] == 1

    _publish(run, _snapshot(used_tokens=12000, compactions=3))
    current = agent_runs.get_context_usage("a")
    assert current["used_tokens"] == 12000
    assert current["context_revision"] == 2
    assert current["context_reason"] == "compaction"


def test_model_checkpoint_is_durable_but_removed_from_public_replay(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    messages = [
        {"role": "user", "content": "finish the goal"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "verified output"},
    ]
    agent_runs._publish(run, "data: " + json.dumps({
        "type": "context_checkpoint", "messages": messages,
        "ledger_hash": "a" * 64, "compactions": 2,
    }) + "\n\n")
    checkpoint = run.continuation["working_checkpoint"]
    assert checkpoint["messages"] == messages
    assert checkpoint["compactions"] == 2
    assert "verified output" not in run.buffer[-1]
    assert '"message_count":3' in run.buffer[-1]


def test_terminal_context_snapshot_is_available_only_when_requested(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    _publish(run, _snapshot(used_tokens=90000))
    run.status = "done"
    assert agent_runs.get_context_usage("a") is None
    assert agent_runs.get_context_usage("a", include_terminal=True)["used_tokens"] == 90000


def _client(monkeypatch, history, run=None, owner_error=False):
    # Load collaborators before installing this route's fixed token estimator;
    # lazy imports must not capture the fixture function for later Agent tests.
    from src import context_policy_runtime  # noqa: F401
    session = SimpleNamespace(model="mac-qwen", endpoint_url="http://mac.test/v1", history=history)
    session.get_context_messages = lambda: [m.to_dict() for m in history]
    manager = SimpleNamespace(get_session=lambda _sid: session)
    def check_owner(*_args):
        if owner_error:
            raise HTTPException(403, "Not your session")
    monkeypatch.setattr(history_routes, "_verify_session_owner", check_owner)
    monkeypatch.setattr(model_context, "estimate_tokens", lambda _messages: 1627)
    monkeypatch.setattr(model_context, "get_context_length", lambda *_args: 262144)
    monkeypatch.setattr(agent_runs, "_RUNS", {"chat": run} if run else {})
    app = FastAPI()
    app.include_router(history_routes.setup_history_routes(manager))
    return TestClient(app)


def _history(snapshot=True):
    metadata = {"input_tokens": 900000, "context_percent": 99, "timestamp": "2026-09-11T01:00:00+00:00"}
    if snapshot:
        metadata["working_context"] = _snapshot()
    return [ChatMessage("user", "research"), ChatMessage("assistant", "result", metadata)]


def test_route_prefers_live_request_over_short_persisted_history(monkeypatch):
    run = agent_runs._Run()
    _publish(run, _snapshot(context_percent=1.2))
    response = _client(monkeypatch, _history(), run).get("/api/session/chat/context")
    assert response.status_code == 200
    data = response.json()
    assert data["used_tokens"] == 82000
    assert data["prompt_tokens"] == 81000
    assert data["context_length"] == 262144
    assert data["context_percent"] == 31.3
    assert data["source"] == "backend"
    assert data["context_status"] == "active_request"
    assert data["stored_chat_tokens"] == 1627
    assert data["messages"] == 2
    assert data["can_compact"] is False


def test_completed_snapshot_is_explicitly_last_request(monkeypatch):
    data = _client(monkeypatch, _history()).get("/api/session/chat/context").json()
    assert data["used_tokens"] == 82000
    assert data["context_status"] == "last_request"
    assert data["source"] == "backend"
    assert data["can_compact"] is True


@pytest.mark.parametrize('live', [False, True])
def test_same_model_different_endpoint_does_not_reuse_snapshot(monkeypatch, live):
    from src.agent_context import context_endpoint_key
    history = _history()
    history[-1].metadata['working_context']['endpoint_key'] = context_endpoint_key('http://jetson.test/v1')
    run = agent_runs._Run() if live else None
    if run:
        _publish(run, history[-1].metadata['working_context'])
    data = _client(monkeypatch, history, run).get('/api/session/chat/context').json()
    assert data['used_tokens'] == 1627
    assert data['context_status'] == 'stored_chat'
    history[-1].metadata['working_context']['endpoint_key'] = context_endpoint_key('http://mac.test/v1')
    if run:
        _publish(run, history[-1].metadata['working_context'])
    data = _client(monkeypatch, history, run).get('/api/session/chat/context').json()
    assert data['used_tokens'] == 82000


def test_endpoint_key_is_opaque_and_strictly_validated():
    from src.agent_context import context_snapshot
    data = context_snapshot(model='same', context_length=8192, prompt_tokens=10,
        source='estimated', round_num=1, limit=6000, compactions=0,
        endpoint_url='http://user:password@example.test/v1')
    assert 'password' not in json.dumps(data)
    assert len(data['endpoint_key']) == 64
    assert agent_runs.normalize_context_usage(data)['endpoint_key'] == data['endpoint_key']
    assert agent_runs.normalize_context_usage({**data, 'endpoint_key': 'bad'}) is None


def test_saved_policy_does_not_rewrite_observed_request(monkeypatch):
    from src import context_policy_runtime
    record = {'effective': {'auto_compact': False, 'trigger_percent': 60, 'target_percent': 40},
              'revisions': {'owner': 1, 'session:chat': 2}}
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', lambda owner, **scope: record)
    data = _client(monkeypatch, _history()).get('/api/session/chat/context').json()
    assert data['auto_compact_threshold'] == 60
    assert data['observed_auto_compact_threshold'] == 85
    assert data['auto_compact_enabled'] is False
    assert data['used_tokens'] == 82000
    assert data['saved_context_policy']['auto_compact'] is False
    assert data['saved_context_policy']['trigger_percent'] == 60
    assert data['saved_context_policy']['threshold_basis'] == 'input_budget'
    assert data['context_policy_error'] is False


def test_live_request_keeps_observed_threshold_over_saved_next_request(monkeypatch):
    from src import context_policy_runtime
    record = {'effective': {'auto_compact': False, 'trigger_percent': 60, 'target_percent': 40},
              'revisions': {'owner': 1, 'session:chat': 2}}
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', lambda owner, **scope: record)
    run = agent_runs._Run()
    _publish(run, _snapshot(auto_compact_threshold=72, auto_compact_enabled=True))

    data = _client(monkeypatch, _history(), run=run).get('/api/session/chat/context').json()

    assert data['auto_compact_threshold'] == 72
    assert data['observed_auto_compact_threshold'] == 72
    assert data['auto_compact_enabled'] is True


def test_active_plain_chat_without_snapshot_uses_saved_policy(monkeypatch):
    from src import context_policy_runtime
    record = {'effective': {'auto_compact': True, 'trigger_percent': 63, 'target_percent': 40},
              'revisions': {'owner': 1, 'session:chat': 2}}
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', lambda owner, **scope: record)

    data = _client(monkeypatch, _history(snapshot=False), run=agent_runs._Run()).get(
        '/api/session/chat/context'
    ).json()

    assert data['active_run'] is True
    assert data['auto_compact_threshold'] == 63
    assert data['auto_compact_enabled'] is True
    assert data['observed_auto_compact_threshold'] is None


def test_enabled_policy_runtime_uses_validated_defaults_not_legacy_85(monkeypatch):
    from src import context_policy_runtime
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', lambda owner, **scope: None)
    monkeypatch.setattr(context_policy_runtime, 'enabled', lambda: True)

    data = _client(monkeypatch, _history(snapshot=False)).get('/api/session/chat/context').json()

    assert data['auto_compact_threshold'] == 75
    assert data['auto_compact_enabled'] is True
    assert data['effective_context_policy']['target_percent'] == 50
    assert data['saved_context_policy'] is None


def test_invalid_saved_policy_keeps_observed_usage_available(monkeypatch):
    from src import context_policy_runtime
    def invalid(*args, **kwargs):
        raise ValueError('invalid')
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', invalid)
    response = _client(monkeypatch, _history()).get('/api/session/chat/context')
    assert response.status_code == 200
    assert response.json()['context_policy_error'] is True
    assert response.json()['saved_context_policy'] is None
    assert response.json()['used_tokens'] == 82000


@pytest.mark.parametrize("change", ["new_user", "different_model", "new_summary", "summary_without_timestamp", "no_snapshot"])
def test_stale_or_absent_snapshot_falls_back_to_labeled_chat_estimate(monkeypatch, change):
    history = _history(snapshot=change != "no_snapshot")
    if change == "new_user":
        history.append(ChatMessage("user", "continue"))
    elif change == "different_model":
        history[-1].metadata["working_context"]["model"] = "other"
    elif change in {"new_summary", "summary_without_timestamp"}:
        meta = {"compacted": True, "hidden": True}
        if change == "new_summary":
            meta["timestamp"] = "2026-09-11T02:00:00+00:00"
        history.insert(0, ChatMessage("system", "new summary", meta))
    data = _client(monkeypatch, history).get("/api/session/chat/context").json()
    assert data["used_tokens"] == 1627
    assert data["source"] == "estimated"
    assert data["context_status"] == "stored_chat"


def test_older_summary_does_not_hide_new_completed_request(monkeypatch):
    history = _history()
    history.insert(0, ChatMessage("system", "summary", {"compacted": True, "hidden": True, "timestamp": "2026-09-10T23:00:00+00:00"}))
    data = _client(monkeypatch, history).get("/api/session/chat/context").json()
    assert data["used_tokens"] == 82000
    assert data["messages"] == 2


def test_context_snapshot_keeps_owner_gate(monkeypatch):
    response = _client(monkeypatch, _history(), owner_error=True).get("/api/session/chat/context")
    assert response.status_code == 403
