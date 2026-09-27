import json

from src.run_activity_snapshot import RunActivitySnapshot
from src import agent_runs


def test_long_current_round_keeps_previous_activity_not_just_last_200_tokens():
    snapshot = RunActivitySnapshot()
    snapshot.observe({"type": "agent_step", "round": 1}, 0)
    snapshot.observe({"delta": "first reasoning", "thinking": True, "round": 1}, 1)
    snapshot.observe({"type": "tool_start", "tool": "python", "round": 1}, 2)
    snapshot.observe({"type": "tool_output", "tool": "python", "output": "42", "round": 1}, 3)
    snapshot.observe({"type": "agent_step", "round": 2}, 4)
    for seq in range(5, 25005):
        snapshot.observe({"delta": "x", "thinking": True, "round": 2}, seq)
    result = snapshot.snapshot()
    assert len(result["events"]) == 6
    assert result["events"][1]["data"]["delta"] == "first reasoning"
    assert result["events"][3]["data"]["output"] == "42"
    assert result["events"][-1]["seq"] == 25004
    assert result["events"][-1]["data"]["delta"] == "x" * 25000


def test_snapshot_is_bounded_and_marks_truncated_reasoning_for_lazy_load():
    snapshot = RunActivitySnapshot()
    for r in range(1, 1001):
        snapshot.observe({"type": "agent_step", "round": r}, r * 3)
        snapshot.observe({"delta": "x" * 100000, "thinking": True, "round": r}, r * 3 + 1)
        snapshot.observe({"type": "tool_output", "tool": "read_file", "output": "y" * 8000,
                          "round": r}, r * 3 + 2)
    result = snapshot.snapshot()
    assert snapshot.size <= snapshot.MAX_RESPONSE_CHARS
    assert len(snapshot.records) <= snapshot.MAX_RECORDS
    assert len(json.dumps(result)) < 600000
    thoughts = [e for e in result["events"] if e["data"].get("thinking")]
    assert thoughts and all(e["data"]["_replay"]["preview_truncated"] for e in thoughts)
    assert all(len(e["data"]["delta"]) == snapshot.TEXT_CHARS for e in thoughts)
    assert min(e["data"].get("round", 1000) for e in result["events"]) > 950


def test_pending_tool_name_survives_argument_only_progress():
    snapshot = RunActivitySnapshot()
    snapshot.observe({"type": "tool_call_progress", "name": "write_file", "arguments_length": 1}, 1)
    for seq in range(2, 10000):
        snapshot.observe({"type": "tool_call_progress", "name": "", "arguments_length": seq}, seq)
    event = snapshot.snapshot()["events"][0]
    assert event["seq"] == 9999
    assert event["data"]["name"] == "write_file"
    assert len(snapshot.records) == 1


def test_run_snapshot_captures_view_without_exposing_model_checkpoint(monkeypatch):
    run = agent_runs._Run()
    monkeypatch.setitem(agent_runs._RUNS, "activity-qa", run)
    monkeypatch.setattr(agent_runs, "_persist_run_state", lambda *args, **kwargs: None)
    agent_runs._publish(run, 'data: {"delta":"visible QA","thinking":true}\n\n')
    agent_runs._publish(run, 'data: {"type":"context_checkpoint","messages":[{"role":"user","content":"private ledger"}]}\n\n')
    assert "activity_snapshot" not in agent_runs.describe_run("activity-qa")
    snapshot = agent_runs.describe_run("activity-qa", include_activity=True)
    assert snapshot["last_seq"] == 1
    assert snapshot["activity_snapshot"]["events"][0]["data"]["delta"] == "visible QA"
    assert "private ledger" not in json.dumps(snapshot["activity_snapshot"])
    assert snapshot["live_rendered_units"] == 1
