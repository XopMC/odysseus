import asyncio
import json

from src.agent_tools.verification_tools import RunVerificationTool, discover_profiles, run_profile


def test_discovers_only_named_profiles(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": "node test.js", "lint": "node lint.js", "danger": "rm -rf data"}}))
    profiles = discover_profiles(str(tmp_path))
    assert set(profiles) == {"pytest", "npm_test", "npm_lint"}
    assert profiles["npm_test"][:3] == ["npm", "run", "test"]


def test_rejects_arbitrary_profile_and_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._resolve_search_root", lambda raw: str(tmp_path))
    result = asyncio.run(RunVerificationTool("test").execute(
        json.dumps({"profile": "danger", "command": "echo unsafe"}), {}))
    assert result["code"] == "invalid_arguments"
    result = asyncio.run(RunVerificationTool("test").execute(
        json.dumps({"profile": "danger"}), {}))
    assert result["code"] == "invalid_arguments"


def test_failed_verification_retains_exact_exit_code(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": "node -e 'process.exit(7)'"}}))
    result = asyncio.run(run_profile(str(tmp_path), "npm_test", 20))
    assert result["exit_code"] == 7
    assert result["code"] == "failed"
    assert result["timed_out"] is False


def test_timeout_never_reports_pass(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": "node -e 'setTimeout(() => {}, 10000)'"}}))
    result = asyncio.run(run_profile(str(tmp_path), "npm_test", 1))
    assert result["exit_code"] == 124
    assert result["code"] == "timeout"
    assert result["timed_out"] is True


def test_failed_output_is_owner_scoped_artifact(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": "node -e 'console.log(\"failed test\"); process.exit(2)'"}}))
    monkeypatch.setattr("src.tool_execution._resolve_search_root", lambda raw: str(tmp_path))
    archived = []
    def fake_archive(owner, session_id, **kwargs):
        archived.append((owner, session_id, kwargs))
        return {"id": "opaque"}
    monkeypatch.setattr("src.observation_pack.archive", fake_archive)
    result = asyncio.run(RunVerificationTool("test").execute(
        json.dumps({"profile": "npm_test"}), {"owner": "alice", "session_id": "session-1"}))
    assert result["exit_code"] == 2
    assert result["artifact"] == {"id": "opaque"}
    assert archived[0][0:2] == ("alice", "session-1")
    assert "failed test" in archived[0][2]["text"]


def test_default_test_profile_falls_back_to_npm(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": "node -e 'console.log(\"ok\")'"}}))
    monkeypatch.setattr("src.tool_execution._resolve_search_root", lambda raw: str(tmp_path))
    result = asyncio.run(RunVerificationTool("test").execute("{}", {}))
    assert result["profile"] == "npm_test"
    assert result["exit_code"] == 0


def test_verification_tools_keep_execution_permissions():
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, plan_mode_disabled_tools
    for name in ("run_tests", "run_lint"):
        assert ToolEffect.EXECUTE_CODE in capabilities_for_tool(name).effects
        assert name in NON_ADMIN_BLOCKED_TOOLS
        assert name in plan_mode_disabled_tools()


def test_native_function_call_maps_to_verification_handler():
    import src.agent_tools as agent_tools
    from src.tool_schemas import function_call_to_tool_block
    for name in ("run_tests", "run_lint"):
        block = function_call_to_tool_block(name, "{}")
        assert block.tool_type == name
        assert name in agent_tools.TOOL_HANDLERS
