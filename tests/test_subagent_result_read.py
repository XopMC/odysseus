"""Model-facing child results must survive the tool formatter, not just REST."""
import asyncio
import json

import pytest

from src.agent_tools import model_interaction_tools as tools
from src.subagent_runtime import runtime
from src.tool_execution import format_tool_result


def test_read_result_is_not_hidden_behind_large_metrics(monkeypatch):
    row = {
        "child_id": "child", "session_id": "s", "name": "Worker",
        "status": "completed", "objective": "task" * 1000,
        "metrics": {"diagnostics": "x" * 20000},
        "guidance": [{"text": "old" * 1000}], "result": "VERIFIED: 323",
    }
    monkeypatch.setattr(runtime, "get", lambda owner, session, child: row)
    monkeypatch.setattr(runtime, "recovery_context", lambda *a: pytest.fail("not requested"))
    result = asyncio.run(tools.manage_subagents(
        '{"action":"read","child_id":"child"}', {"owner": "qa", "session_id": "s"}))
    rendered = format_tool_result("manage_subagents", result)
    assert "VERIFIED: 323" in rendered
    assert "Session created:" not in rendered
    assert len(rendered) < 2000
    assert result["result_truncated"] is False
    assert result["next_result_offset"] is None
    assert row["metrics"]["diagnostics"] == "x" * 20000


def test_read_result_pages_are_lossless_and_owner_scoped(monkeypatch):
    original = "Привет 🐈\n" * 2100 + "FINAL VERDICT"
    calls = []
    def get(owner, session, child):
        calls.append((owner, session, child))
        return {"child_id": child, "status": "completed", "result": original}
    monkeypatch.setattr(runtime, "get", get)
    offset, pages = 0, []
    while offset is not None:
        result = asyncio.run(tools.manage_subagents(json.dumps({
            "action": "read", "child_id": "child", "result_offset": offset,
            "result_limit": 2000,
        }), {"owner": "qa", "session_id": "s"}))
        pages.append(result["content"])
        assert result["result_total_chars"] == len(original)
        offset = result["next_result_offset"]
    assert "".join(pages) == original
    assert set(calls) == {("qa", "s", "child")}


@pytest.mark.parametrize("field,value", [
    ("result_offset", -1), ("result_offset", True), ("result_offset", "1"),
    ("result_limit", 0), ("result_limit", 12001), ("result_limit", 1.5),
    ("include_recovery_context", "false"),
])
def test_invalid_result_page_is_rejected(monkeypatch, field, value):
    monkeypatch.setattr(runtime, "get", lambda *a: pytest.fail("invalid read"))
    result = asyncio.run(tools.manage_subagents(json.dumps({
        "action": "read", "child_id": "child", field: value,
    }), {"owner": "qa", "session_id": "s"}))
    assert result["exit_code"] == 1


def test_read_missing_child_does_not_read_checkpoint(monkeypatch):
    monkeypatch.setattr(runtime, "get", lambda *a: None)
    monkeypatch.setattr(runtime, "recovery_context", lambda *a: pytest.fail("cross-owner read"))
    result = asyncio.run(tools.manage_subagents(
        '{"action":"read","child_id":"child","include_recovery_context":true}',
        {"owner": "other", "session_id": "s"}))
    assert result["exit_code"] == 1


def test_recovery_context_is_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(runtime, "get", lambda *a: {
        "child_id": "child", "status": "error", "result": "partial"})
    monkeypatch.setattr(runtime, "recovery_context", lambda *a: {"inspection_only": True})
    result = asyncio.run(tools.manage_subagents(
        '{"action":"read","child_id":"child","include_recovery_context":true}',
        {"owner": "qa", "session_id": "s"}))
    assert result["content"] == "partial"
    assert result["recovery_context"]["inspection_only"] is True
