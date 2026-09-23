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


def test_context_window_change_is_new_measurement_route_not_stale_highwater(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    _publish(run, _snapshot(used_tokens=49567, context_length=65024))
    _publish(run, _snapshot(used_tokens=40100, context_length=131840))
    current = agent_runs.get_context_usage("a")
    assert current["used_tokens"] == 40100
    assert current["context_length"] == 131840
    assert current["context_revision"] == 2


def test_context_ledger_reconciles_reset_compaction_counter_on_new_attempt(monkeypatch):
    """A replacement loop must not freeze telemetry at the prior high-water."""
    import core.database as database
    from types import SimpleNamespace

    prior = SimpleNamespace(
        run_id="prior",
        context_snapshot=_snapshot(
            used_tokens=38095, compactions=11, context_revision=541,
        ),
    )

    class Query:
        def filter(self, *_args, **_kwargs): return self
        def all(self): return [prior]
        def first(self): return None

    class Db:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def query(self, *_args, **_kwargs): return Query()
        def add(self, *_args, **_kwargs): return None

    class Factory:
        def begin(self): return Db()
        def __call__(self): return Db()

    monkeypatch.setattr(database, "SessionLocal", Factory())
    run = agent_runs._Run()
    run.run_id = "current"
    run.session_id = "same-session"
    run.context_usage = _snapshot(
        used_tokens=38095, compactions=11, context_revision=541,
    )
    run.context_revision = 541
    monkeypatch.setattr(agent_runs, "_RUNS", {"same-session": run})

    # The new agent loop reports a zero-based counter, but its request is now
    # larger than the previous run. It must be accepted as generation 11.
    _publish(run, _snapshot(used_tokens=56000, compactions=0))
    current = agent_runs.get_context_usage("same-session")
    assert current["used_tokens"] == 56000
    assert current["compactions"] == 11
    assert current["context_revision"] == 542
    assert current["context_reason"] == "measurement"
    replayed = json.loads(next(
        line[6:] for line in run.buffer[-1].splitlines() if line.startswith("data: ")
    ))
    assert replayed["data"]["compactions"] == 11
    assert replayed["_replay"]["context_revision"] == 542


def test_checkpoint_generation_is_reconciled_with_seeded_context(monkeypatch):
    run = agent_runs._Run()
    run.context_usage = _snapshot(used_tokens=56000, compactions=11)
    run.context_revision = 542
    monkeypatch.setattr(agent_runs, "_RUNS", {"same-session": run})
    messages = [{"role": "user", "content": "continue"}]
    agent_runs._publish(run, "data: " + json.dumps({
        "type": "context_checkpoint", "messages": messages,
        "ledger_hash": "b" * 64, "compactions": 0,
    }) + "\n\n")
    assert run.continuation["working_checkpoint"]["compactions"] == 11
    public = json.loads(next(
        line[6:] for line in run.buffer[-1].splitlines() if line.startswith("data: ")
    ))
    assert public["compactions"] == 11


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


def test_session_high_water_survives_lower_measurement_on_new_run(monkeypatch):
    import core.database as database
    from types import SimpleNamespace
    prior = SimpleNamespace(
        run_id="prior", context_snapshot=_snapshot(
            used_tokens=100000, compactions=8, context_revision=20,
        ),
    )
    current = SimpleNamespace(
        run_id="current", context_snapshot=None, continuation=None,
        status="running", last_seq=-1, durable_seq=-1, context_revision=0,
        ledger_hash=None, terminal_at=None,
    )
    class Query:
        def filter(self, *_args, **_kwargs): return self
        def all(self): return [prior]
        def first(self): return current
    class Db:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def get_bind(self): return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def query(self, *_args, **_kwargs): return Query()
    class Factory:
        def begin(self): return Db()
    monkeypatch.setattr(database, "SessionLocal", Factory())
    run = agent_runs._Run()
    run.run_id = "current"
    run.session_id = "same-session"
    run.context_usage = _snapshot(used_tokens=40000, compactions=1)
    agent_runs._persist_run_state(run, status="stopped", durable=True)
    assert run.context_usage["used_tokens"] == 100000
    assert run.context_usage["compactions"] == 8


def test_context_read_uses_session_high_water_when_latest_run_counter_reset(monkeypatch):
    import core.database as database
    from types import SimpleNamespace
    latest = SimpleNamespace(context_snapshot=_snapshot(
        used_tokens=40000, compactions=1, context_revision=514,
        context_reason="measurement",
    ))
    previous = SimpleNamespace(context_snapshot=_snapshot(
        used_tokens=66000, compactions=8, context_revision=503,
        context_reason="measurement",
    ))
    class Query:
        def filter(self, *_args, **_kwargs): return self
        def order_by(self, *_args, **_kwargs): return self
        def all(self): return [latest, previous]
    class Db:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def query(self, *_args, **_kwargs): return Query()
    class Factory:
        def __call__(self): return Db()
    monkeypatch.setattr(database, "SessionLocal", Factory())
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    current = agent_runs.get_context_usage("same-session", include_terminal=True)
    assert current["used_tokens"] == 66000
    assert current["compactions"] == 8


def test_terminal_context_snapshot_is_available_only_when_requested(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {"a": run})
    _publish(run, _snapshot(used_tokens=90000))
    run.status = "done"
    assert agent_runs.get_context_usage("a") is None
    assert agent_runs.get_context_usage("a", include_terminal=True)["used_tokens"] == 90000


def test_terminal_context_snapshot_survives_in_memory_run_eviction(monkeypatch):
    from types import SimpleNamespace
    import core.database as database
    session_id = "durable-context-" + str(id(monkeypatch))
    saved = SimpleNamespace(
        context_snapshot=_snapshot(used_tokens=151000, compactions=3),
    )
    class Query:
        def filter(self, *_args, **_kwargs): return self
        def order_by(self, *_args, **_kwargs): return self
        def all(self): return [saved]
        def first(self): return saved
    class Db:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def query(self, *_args, **_kwargs): return Query()
    monkeypatch.setattr(database, "SessionLocal", lambda: Db())
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    current = agent_runs.get_context_usage(session_id, include_terminal=True)
    assert current["used_tokens"] == 151000
    assert current["compactions"] == 3


def _client(monkeypatch, history, run=None, owner_error=False, checkpoint=None):
    # Load collaborators before installing this route's fixed token estimator;
    # lazy imports must not capture the fixture function for later Agent tests.
    from src import context_policy_runtime  # noqa: F401
    session = SimpleNamespace(
        model="mac-qwen", endpoint_url="http://mac.test/v1", history=history,
        context_checkpoint=checkpoint, message_count=len(history),
    )
    session.get_context_messages = lambda: [
        m.to_dict() if hasattr(m, "to_dict") else m for m in history
    ]
    manager = SimpleNamespace(get_session=lambda _sid: session)
    def check_owner(*_args):
        if owner_error:
            raise HTTPException(403, "Not your session")
    monkeypatch.setattr(history_routes, "_verify_session_owner", check_owner)
    monkeypatch.setattr(model_context, "estimate_tokens", lambda _messages: 1627)
    monkeypatch.setattr(model_context, "get_context_length", lambda *_args: 262144)
    from src import endpoint_resolver
    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint", lambda _kind, owner=None, **kw:
                        (kw.get("fallback_url"), kw.get("fallback_model"), kw.get("fallback_headers") or {}))
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
    assert data["can_compact"] is False
    assert data["compaction_preview"]["reason"] == "not_enough_messages"


def test_context_route_previews_native_batch_infeasibility_without_enabling_button(monkeypatch):
    history = [
        {"role": "user", "content": "latest request"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for i in range(5)
        ]},
        *[{"role": "tool", "tool_call_id": f"call-{i}", "content": "result"}
          for i in range(5)],
    ]
    data = _client(monkeypatch, history).get("/api/session/chat/context").json()
    assert data["can_compact"] is False
    assert data["compaction_preview"]["feasible"] is False
    assert data["compaction_preview"]["reason"] == "native_not_compactable"


def test_context_route_exposes_configured_summarizer_and_safe_group_counts(monkeypatch):
    history = [
        ChatMessage("user", "request one"), ChatMessage("assistant", "reply one"),
        ChatMessage("user", "request two"), ChatMessage("assistant", "reply two"),
        ChatMessage("user", "request three"), ChatMessage("assistant", "reply three"),
    ]
    data = _client(monkeypatch, history).get("/api/session/chat/context").json()
    preview = data["compaction_preview"]
    assert data["can_compact"] is True
    assert preview["feasible"] is True
    assert preview["archive_groups"] >= 2
    assert preview["summarizer_configured"] is True
    assert preview["summarizer_model"] == "mac-qwen"


def test_idle_context_uses_current_loaded_window_after_model_reload(monkeypatch):
    client = _client(monkeypatch, _history())
    monkeypatch.setattr(model_context, "get_context_length", lambda *_args: 131840)
    data = client.get("/api/session/chat/context").json()
    assert data["context_length"] == 131840
    assert data["used_tokens"] == 82000
    assert data["context_percent"] == round(82000 / 131840 * 100, 1)


def test_effective_trigger_uses_usable_budget_not_full_window(monkeypatch):
    from src import context_policy_runtime
    from src.context_policy import ContextPolicy
    policy = ContextPolicy(trigger_percent=75)
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', lambda owner, **scope: {
        'effective': policy.to_dict(), 'revisions': {'owner': 1}})
    data = _client(monkeypatch, _history(snapshot=False)).get('/api/session/chat/context').json()
    expected = policy.budget(262144).trigger_messages
    assert data['effective_auto_compact_trigger_tokens'] == expected
    assert data['effective_auto_compact_threshold'] == round(100 * expected / 262144, 1)
    assert data['should_compact'] is False


def test_legacy_short_compaction_timeout_is_raised_to_ten_minutes():
    from src.context_policy import ContextPolicy
    assert ContextPolicy().effective_summary_timeout_seconds == 600
    assert ContextPolicy(summary_timeout_seconds=45).effective_summary_timeout_seconds == 600
    assert ContextPolicy(summary_timeout_seconds=900).effective_summary_timeout_seconds == 900


def test_newer_manual_checkpoint_replaces_stale_terminal_measurement(monkeypatch):
    run = agent_runs._Run()
    _publish(run, _snapshot(used_tokens=262143, context_revision=44, compactions=7))
    run.status = "done"
    checkpoint = ChatMessage("system", "[Conversation summary]\ncompact", {
        "timestamp": "2099-01-01T00:00:00+00:00",
        "context_reason": "manual_compaction",
        "compaction_revision": 2,
        "ledger_hash": "a" * 64,
    })

    data = _client(monkeypatch, _history(), run, checkpoint=checkpoint).get(
        "/api/session/chat/context"
    ).json()

    assert data["context_status"] == "working_checkpoint"
    assert data["used_tokens"] == 1627
    assert data["context_percent"] == 0.6
    assert data["source"] == "estimated"
    assert data["backend_measurement"]["used_tokens"] == 262143
    assert data["working_checkpoint"]["compaction_revision"] == 2
    assert data["context_revision"] == data["backend_measurement"]["context_revision"] + 1


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


def test_idle_legacy_context_uses_current_default_trigger_not_old_85(monkeypatch):
    from src import context_policy_runtime
    monkeypatch.setattr(context_policy_runtime, 'owner_policy', lambda owner, **scope: None)
    monkeypatch.setattr(context_policy_runtime, 'enabled', lambda: False)

    data = _client(monkeypatch, _history(snapshot=False)).get('/api/session/chat/context').json()

    assert data['auto_compact_threshold'] == 75
    assert data['configured_auto_compact_threshold'] == 75


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
