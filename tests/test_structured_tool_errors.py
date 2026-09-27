import asyncio
from types import SimpleNamespace

from src.tool_errors import enrich_tool_error


def test_structured_tool_errors_cover_required_categories_without_replaying():
    cases = [
        ({"error": "missing", "code": "not_found", "exit_code": 1}, "not_found"),
        ({"error": "denied", "blocked": True, "exit_code": 1}, "permission_denied"),
        ({"error": "offline", "code": "transport_unavailable", "exit_code": 1}, "transport_unavailable"),
        ({"error": "old", "code": "stale_revision", "exit_code": 1}, "stale_revision"),
        ({"error": "reply lost", "outcome_unknown": True, "exit_code": 1}, "unknown_outcome"),
        ({"error": "slow", "timed_out": True, "exit_code": 124}, "timeout"),
        ({"error": "missing", "code": "not_found"}, "not_found"),
    ]
    for raw, category in cases:
        result = enrich_tool_error(raw)
        assert result["error_category"] == category
        assert result["next_action"] and len(result["next_action"]) < 180
        assert result["retryable"] is False
        assert result["error"] == raw["error"]
    assert enrich_tool_error({"output": "ok", "exit_code": 0}) == {"output": "ok", "exit_code": 0}
    assert enrich_tool_error({"approval_required": True, "exit_code": None}) == {
        "approval_required": True, "exit_code": None}


def test_timeout_transport_retry_hint_never_authorizes_effectful_replay():
    result = enrich_tool_error({
        "error": "host command timed out", "exit_code": 124,
        "timed_out": True, "retryable": True,
    })
    assert result["error_category"] == "timeout"
    assert result["retryable"] is False
    assert "completed" in result["next_action"]


def test_dispatch_enriches_early_policy_denial(monkeypatch):
    from src import tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    context = ToolRunSecurityContext(external_untrusted_context_seen=True)
    description, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="bash", content="echo safe"),
        security_context=context))
    assert "BLOCKED" in description
    assert result["error_category"] == "permission_denied"
    assert "request" in result["next_action"].lower()


def test_missing_read_file_is_not_found_without_exposing_host_path(monkeypatch, tmp_path):
    from src import tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: None)
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="read_file", content="missing.txt"),
        owner="alice", workspace=str(tmp_path), security_context=ToolRunSecurityContext()))
    assert result["error_category"] == "not_found"
    assert str(tmp_path) not in result["error"]


def test_background_bash_missing_workspace_is_definite_not_started(monkeypatch, tmp_path):
    from src import bg_jobs, host_execution, tool_execution

    missing = tmp_path / "removed-workspace"
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: False)

    def launch(_command, *, session_id, cwd, followup_context=None):
        assert cwd == str(missing)
        raise FileNotFoundError(2, "No such file or directory", str(missing))

    monkeypatch.setattr(bg_jobs, "launch", launch)
    description, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="bash", content="#!bg\nprintf 42"),
        owner="alice", session_id="safe-test-chat", workspace=str(missing),
        security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
    ))
    assert description == "bash (background): NOT STARTED"
    assert result["code"] == "workspace_unavailable"
    assert result["outcome_unknown"] is False
    assert result["exit_code"] == 1
    assert str(missing) not in result["error"]
