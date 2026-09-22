"""Run latency telemetry must be measurable without retaining event content."""

from src.run_health_telemetry import RunHealthTelemetry


def test_ttft_tool_latency_and_compaction_failures_are_content_free():
    metrics = RunHealthTelemetry(100.0)
    metrics.observe({"type": "agent_step", "round": 1}, now=101.0)
    metrics.observe({"delta": "private model text"}, now=101.25)
    metrics.observe({"delta": "more private model text"}, now=101.5)
    metrics.observe({"type": "tool_start", "tool": "read_file", "tool_call_id": "call-1",
                     "command": "private path"}, now=102.0)
    metrics.observe({"type": "tool_output", "tool": "read_file", "tool_call_id": "call-1",
                     "output": "private file contents"}, now=102.4)
    metrics.observe({"type": "context_compaction_failed", "message": "private provider error",
                     "duration_ms": 1200.5}, now=103.0)
    metrics.observe({"type": "metrics", "data": {"prefill_tps": 123.45,
                    "round_texts": ["private response"]}}, now=103.1)
    metrics.reconnect()
    result = metrics.snapshot()
    assert result["ttft_last_ms"] == 250.0
    assert result["ttft_count"] == 1
    assert result["tool_latency_count"] == 1
    assert result["tool_latency_mean_ms"] == 400.0
    assert result["compaction_failures"] == 1
    assert result["compaction_max_ms"] == 1200.5
    assert result["prefill_tps_last"] == 123.45
    assert result["sse_reconnects"] == 1
    assert "private" not in str(result)


def test_tool_latency_without_start_is_not_invented():
    metrics = RunHealthTelemetry(100.0)
    metrics.observe({"type": "tool_output", "tool": "bash", "tool_call_id": "lost"}, now=110.0)
    assert metrics.snapshot()["tool_latency_count"] == 0


def test_active_run_summary_has_only_numeric_latency(monkeypatch):
    from src import agent_runs

    run = agent_runs._Run()
    run.health_metrics.observe({"delta": "private response"}, now=run.started_at + 0.5)
    monkeypatch.setitem(agent_runs._RUNS, "health-test-chat", run)
    summary = agent_runs.active_run_health_summary()
    assert summary["measured_runs"] >= 1
    assert summary["max_ttft_ms"] >= 500
    assert "private" not in str(summary)
