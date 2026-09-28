"""Polling child status must not repeatedly return the full execution history."""
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatSubagentRun, Session
from src import subagent_runtime as children


@pytest.fixture
def runtime(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(children, "SessionLocal", factory)
    with factory.begin() as db:
        db.add(Session(id="s", name="QA", endpoint_url="http://model", model="parent", owner="qa"))
    result = children.SubagentRuntime()
    result._recovered = True
    yield result, factory
    engine.dispose()


def test_list_bounds_large_metrics_but_detail_retains_full_history(runtime):
    runner, factory = runtime
    metrics = {
        "tool_events": [{"tool": "read_file", "output": "tool evidence " * 3600}],
        "round_reasonings": ["reasoning " * 1800],
        "round_texts": ["prior answer " * 1600],
        "round_models": ["worker"] * 1000,
        "provider_payload": {"unexpected": "private history " * 1000},
        "waiting_user": {"question": "large question " * 1000},
        "thinking_chars": 18000, "output_chars": 19200,
        "provider_retries": 2, "checkpoint_messages": 42,
        "checkpoint_hash": "a" * 64, "context_compactions": 3,
        "input_tokens": 24000, "output_tokens": 5000,
        "usage_source": "real", "response_time": 1830.5,
        "working_context": {
            "used_tokens": 24000, "context_length": 131072,
            "context_percent": 18.3, "compactions": 3,
            "auto_compact_enabled": True, "source": "backend",
            "provider_payload": {"messages": ["must stay in detail " * 2000]},
        },
    }
    assert len(json.dumps(metrics)) > 80000
    with factory.begin() as db:
        db.add(ChatSubagentRun(
            id="child", parent_session_id="s", parent_run_id="parent", owner="qa",
            ordinal=1, name="Worker", objective="Verify", assigned_context="",
            model="worker", status="completed", result="Verified result",
            metrics=metrics, guidance=[{"text": "Preserve existing work"}],
        ))
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "chat_subagent_runs" in statement and statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        listed = runner.list("qa", "s", parent_run_id="parent")[0]
        first_queries = list(statements)
        statements.clear()
        assert runner.list("qa", "s", parent_run_id="parent")[0]["metrics"] == listed["metrics"]
        cached_queries = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert any("chat_subagent_runs.metrics AS" in statement for statement in first_queries)
    assert cached_queries and all("chat_subagent_runs.metrics AS" not in statement for statement in cached_queries)
    assert all("chat_subagent_runs.result AS" not in statement for statement in cached_queries)
    assert all("assigned_context" not in statement for statement in cached_queries)
    assert listed["status"] == "completed"
    assert listed["result_missing"] is False
    assert "result" not in listed and "guidance" not in listed
    assert len(json.dumps(listed["metrics"])) < 1024
    assert listed["metrics"] == {
        key: value for key, value in metrics.items()
        if key not in {"tool_events", "round_reasonings", "round_texts", "round_models",
                       "provider_payload", "waiting_user", "working_context"}
    } | {"working_context": {
        key: value for key, value in metrics["working_context"].items()
        if key != "provider_payload"
    }}
    detail = runner.get("qa", "s", "child")
    assert detail["metrics"] == metrics
    assert detail["result"] == "Verified result"
    assert detail["guidance"] == [{"text": "Preserve existing work"}]
    assert runner.list("other", "s") == []
    assert runner.list("qa", "other") == []
    assert runner.list("qa", "s", parent_run_id="other") == []
    assert runner.get("other", "s", "child") is None


def test_list_metric_allowlist_rejects_nested_and_unbounded_values():
    source = {
        "thinking_chars": {"text": "x" * 100000},
        "output_chars": ["x" * 100000],
        "provider_retries": True, "input_tokens": -1,
        "output_tokens": float("inf"), "response_time": float("nan"),
        "context_length": 10**100,
        "checkpoint_hash": "a" * 100000,
        "usage_source": "b" * 100000, "tps_source": "c" * 100000,
        "working_context": {
            "used_tokens": {"messages": ["x" * 100000]},
            "source": {"data": "x" * 100000},
            "auto_compact_enabled": "true", "context_revision": 4,
        },
    }
    assert children._list_metrics(source) == {
        "checkpoint_hash": "a" * 128, "usage_source": "b" * 32,
        "tps_source": "c" * 32, "working_context": {"context_revision": 4},
    }
    assert children._list_metrics(None) == {}
    assert children._list_metrics([source]) == {}


def test_cached_list_projection_rejects_boolean_as_numeric_and_refreshes_revision(runtime):
    runner, factory = runtime
    with factory.begin() as db:
        db.add(ChatSubagentRun(
            id="typed", parent_session_id="s", owner="qa", ordinal=1,
            name="Worker", objective="Verify", assigned_context="", model="worker",
            status="running", metrics={
                "provider_retries": True, "thinking_chars": 12,
                "working_context": {"used_tokens": False, "auto_compact_enabled": True,
                                    "source": "backend"},
            },
        ))
    assert runner.list("qa", "s")[0]["metrics"] == {
        "thinking_chars": 12,
        "working_context": {"auto_compact_enabled": True, "source": "backend"},
    }
    with factory.begin() as db:
        row = db.get(ChatSubagentRun, "typed")
        row.metrics = {"thinking_chars": 13}
        row.revision += 1
    assert runner.list("qa", "s")[0]["metrics"] == {"thinking_chars": 13}
    assert len(runner._list_metrics_cache) == 1, (
        "a frequently updated child must not evict stable historical summaries"
    )


def test_list_treats_ascii_whitespace_result_as_missing(runtime):
    runner, factory = runtime
    with factory.begin() as db:
        db.add(ChatSubagentRun(
            id="blank", parent_session_id="s", owner="qa", ordinal=1,
            name="Worker", objective="Verify", assigned_context="", model="worker",
            status="completed", result=" \n\t\r", metrics={},
        ))
    listed = runner.list("qa", "s")[0]
    assert listed["result_missing"] is True
    assert listed["error"] == "Subagent produced no visible final result"


@pytest.mark.parametrize("status", ["queued", "running", "waiting_user", "stopping",
                                        "completed", "failed", "cancelled", "interrupted"])
def test_compact_list_preserves_status_and_missing_result(runtime, status):
    runner, factory = runtime
    with factory.begin() as db:
        db.add(ChatSubagentRun(
            id="child", parent_session_id="s", owner="qa", ordinal=1,
            name="Worker", objective="Verify", assigned_context="", model="worker",
            status=status, result="", metrics={"tool_events": ["x" * 10000]},
        ))
    row = runner.list("qa", "s")[0]
    assert row["status"] == status
    assert row["result_missing"] is (status == "completed")
    assert row["metrics"] == {}
    assert row["error"] == ("Subagent produced no visible final result" if status == "completed" else "")
