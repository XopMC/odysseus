import asyncio
import json
import sys

from src.agent_tools.toolchain_diagnostics import InspectToolchainTool


def test_toolchain_inventory_is_fixed_bounded_and_does_not_probe_network_by_default(monkeypatch):
    from src.agent_tools import toolchain_diagnostics as module
    seen = []
    monkeypatch.setattr(module, "_run_version", lambda name: seen.append(name) or {"status": "unavailable"})
    result = asyncio.run(InspectToolchainTool().execute("{}", {"owner": "alice"}))
    assert result["exit_code"] == 0
    assert result["tools"]["python"]["version"] == sys.version.split()[0]
    assert result["network"]["status"] == "not_checked"
    assert set(seen) == set(module._TOOL_COMMANDS)
    assert len(result["output"]) < 6000


def test_toolchain_rejects_arbitrary_program_url_and_unknown_arguments(monkeypatch):
    from src.agent_tools.http_probe_tool import HttpProbeTool
    called = []
    async def probe(self, content, ctx):
        called.append((content, ctx))
        return {"exit_code": 0, "status": 200}
    monkeypatch.setattr(HttpProbeTool, "execute", probe)
    tool = InspectToolchainTool()
    for args in ({"url": "http://127.0.0.1/admin"}, {"program": "/tmp/evil"},
                 {"endpoint_id": "../escape"}):
        assert asyncio.run(tool.execute(json.dumps(args), {"owner": "alice"}))["exit_code"] == 1
    assert called == []
    result = asyncio.run(tool.execute('{"endpoint_id":"registered-1"}', {"owner": "alice"}))
    assert result["exit_code"] == 0
    assert result["network"]["status"] == 200
    assert called[0][1]["owner"] == "alice"
    assert json.loads(called[0][0]) == {"endpoint_id": "registered-1"}


def test_toolchain_ignores_workspace_path_shims(monkeypatch, tmp_path):
    from src.agent_tools import toolchain_diagnostics as module
    shim = tmp_path / "git"
    shim.write_text("#!/bin/sh\necho MALICIOUS\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    for candidates in module._TOOL_COMMANDS.values():
        assert all(str(tmp_path) not in candidate for candidate in candidates)
    result = asyncio.run(InspectToolchainTool().execute("{}", {"owner": "alice"}))
    assert "MALICIOUS" not in result["output"]


def test_registered_toolchain_schema_dispatches_and_host_mode_fails_closed(monkeypatch):
    from types import SimpleNamespace
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: False)
    assert any(item["function"]["name"] == "inspect_toolchain" for item in FUNCTION_TOOL_SCHEMAS)
    block = function_call_to_tool_block("inspect_toolchain", {})
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=block.tool_type, content=block.content),
        owner="alice", security_context=ToolRunSecurityContext()))
    assert result["exit_code"] == 0 and "python" in result["tools"]
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: True)
    calls = []
    async def remote(tool, content, **scope):
        calls.append((tool, content, scope))
        return {"exit_code": 0, "execution_host": "jetson", "network": {"status": "not_checked"}}
    monkeypatch.setattr(host_execution, "execute", remote)
    _, host_result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=block.tool_type, content=block.content),
        owner="alice", security_context=ToolRunSecurityContext()))
    assert host_result["execution_host"] == "jetson"
    assert calls == [("inspect_toolchain", block.content, {})]


def test_team_readonly_surface_exposes_toolchain_without_shell():
    from src import team_tools
    schemas = team_tools.schemas("alice", "reviewer", config={"trusted_host": True, "web": False})
    names = {item["function"]["name"] for item in schemas}
    assert "inspect_toolchain" in names
    assert "bash" not in names
