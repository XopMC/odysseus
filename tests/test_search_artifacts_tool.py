import asyncio
import json
from types import SimpleNamespace

from src import observation_pack
from src.agent_tools.efficiency_tools import ReadToolArtifactTool, SearchArtifactsTool
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block


def test_search_artifacts_tool_uses_exact_owned_run_and_bounded_output(monkeypatch, tmp_path):
    monkeypatch.setattr(observation_pack, "DATA_DIR", str(tmp_path))
    run_id = "a" * 32
    artifact = observation_pack.archive("alice", "chat-a", tool_name="bash",
                                        tool_call_id="call-a", text="private prefix\nneedle here\n" + "x" * 20_000,
                                        force=True, run_id=run_id)
    observation_pack.archive("alice", "chat-a", tool_name="bash", tool_call_id="call-b",
                             text="needle from other run", force=True, run_id="b" * 32)
    tool = SearchArtifactsTool()
    args = json.dumps({"query": "needle"})
    result = asyncio.run(tool.execute(args, {"owner": "alice", "session_id": "chat-a",
                                             "parent_run_id": run_id}))
    assert result["exit_code"] == 0
    assert result["matches"] == [{"id": artifact["id"], "tool": "bash", "line": 2,
                                   "snippet": "needle here", "bytes": 20_027}]
    assert len(result["output"]) < 1000
    for ctx in ({"owner": "bob", "session_id": "chat-a", "parent_run_id": run_id},
                {"owner": "alice", "session_id": "chat-b", "parent_run_id": run_id},
                {"owner": "alice", "session_id": "chat-a", "parent_run_id": "b" * 32}):
        other = asyncio.run(tool.execute(args, ctx))
        assert other["exit_code"] == 0
        assert all(item["id"] != artifact["id"] for item in other["matches"])


def test_search_artifacts_requires_live_run_and_valid_arguments():
    tool = SearchArtifactsTool()
    for content, ctx in ((json.dumps({"query": "needle"}), {"session_id": "chat"}),
                         (json.dumps({"query": ""}), {"session_id": "chat", "parent_run_id": "a" * 32}),
                         (json.dumps({"query": "needle", "limit": 999}),
                          {"session_id": "chat", "parent_run_id": "a" * 32}),
                         (json.dumps({"query": "needle", "owner": "bob"}),
                          {"session_id": "chat", "parent_run_id": "a" * 32})):
        assert asyncio.run(tool.execute(content, ctx))["exit_code"] == 1


def test_search_artifacts_native_schema_preserves_query_cursor_and_limit():
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "search_artifacts")
    assert schema["function"]["parameters"]["required"] == ["query"]
    args = {"query": "needle", "limit": 2, "cursor": "obs_" + "a" * 24}
    block = function_call_to_tool_block("search_artifacts", args)
    assert json.loads(block.content) == args


def test_agent_dispatch_passes_server_owned_run_id_to_artifact_search(monkeypatch, tmp_path):
    from src import tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(observation_pack, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    run_id = "c" * 32
    artifact = observation_pack.archive("alice", "chat", tool_name="bash",
                                        tool_call_id="call", text="needle\n",
                                        force=True, run_id=run_id)
    block = SimpleNamespace(tool_type="search_artifacts", content='{"query":"needle"}')
    _, result = asyncio.run(tool_execution.execute_tool_block(
        block, owner="alice", session_id="chat",
        security_context=ToolRunSecurityContext(run_id=run_id)))
    assert result["exit_code"] == 0
    assert result["matches"][0]["id"] == artifact["id"]


def test_read_tool_artifact_rejects_other_run_after_search(monkeypatch, tmp_path):
    monkeypatch.setattr(observation_pack, "DATA_DIR", str(tmp_path))
    run_a, run_b = "a" * 32, "b" * 32
    meta = observation_pack.archive("alice", "chat", tool_name="bash", tool_call_id="call",
                                    text="private needle\n", force=True, run_id=run_a)
    tool = ReadToolArtifactTool()
    content = json.dumps({"id": meta["id"]})
    own = asyncio.run(tool.execute(content, {"owner": "alice", "session_id": "chat",
                                             "parent_run_id": run_a}))
    assert own["exit_code"] == 0 and own["text"] == "private needle\n"
    for ctx in ({"owner": "alice", "session_id": "chat", "parent_run_id": run_b},
                {"owner": "alice", "session_id": "chat"},
                {"owner": "bob", "session_id": "chat", "parent_run_id": run_a}):
        denied = asyncio.run(tool.execute(content, ctx))
        assert denied["exit_code"] == 1
        assert "private needle" not in json.dumps(denied)


def test_mcp_fallback_read_file_indexes_artifact_for_same_run(monkeypatch, tmp_path):
    from src import tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(observation_pack, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    source = tmp_path / "large.txt"
    source.write_text("needle from this run\n" + "x" * 30_000, encoding="utf-8")
    security = ToolRunSecurityContext(run_id="a" * 32)
    _, read = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="read_file", content=json.dumps({"path": str(source)})),
        owner="alice", session_id="chat", security_context=security, workspace=str(tmp_path)))
    assert read["exit_code"] == 0 and read["artifact_id"].startswith("obs_")
    _, found = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type="search_artifacts", content='{"query":"needle from this run"}'),
        owner="alice", session_id="chat", security_context=security, workspace=str(tmp_path)))
    assert found["exit_code"] == 0
    assert [item["id"] for item in found["matches"]] == [read["artifact_id"]]
