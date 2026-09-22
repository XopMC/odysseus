import asyncio
import hashlib
import json

from src.agent_tools.file_comparison_tools import CompareFilesTool, VerifyHashesTool
from src.tool_execution import _active_workspace


def _run(tool, args, root):
    token = _active_workspace.set(str(root))
    try:
        return asyncio.run(tool.execute(json.dumps(args), {"owner": "alice"}))
    finally:
        _active_workspace.reset(token)


def test_compare_reports_exact_hashes_and_normalized_newline_diff(tmp_path):
    before = tmp_path / "before.txt"
    after = tmp_path / "after.txt"
    before.write_bytes(b"one\r\ntwo\r\n")
    after.write_bytes(b"one\ntwo\n")
    result = _run(CompareFilesTool(), {"before": "before.txt", "after": "after.txt"}, tmp_path)
    assert result["exit_code"] == 0
    assert result["before_sha256"] == hashlib.sha256(before.read_bytes()).hexdigest()
    assert result["after_sha256"] == hashlib.sha256(after.read_bytes()).hexdigest()
    assert result["identical"] is False
    assert result["normalized_equal"] is True
    assert result["diff"] == ""


def test_compare_bounded_diff_and_binary_without_content_leak(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("one\nthree\n", encoding="utf-8")
    result = _run(CompareFilesTool(), {"before": "a.txt", "after": "b.txt"}, tmp_path)
    assert result["exit_code"] == 0 and "-two" in result["diff"] and "+three" in result["diff"]
    (tmp_path / "binary-a").write_bytes(b"secret\x00one")
    (tmp_path / "binary-b").write_bytes(b"secret\x00two")
    binary = _run(CompareFilesTool(), {"before": "binary-a", "after": "binary-b"}, tmp_path)
    assert binary["exit_code"] == 0 and binary["binary"] is True
    assert binary["diff"] == "" and "secret" not in binary["output"]


def test_verify_hashes_rejects_false_claim_and_reports_actual_digest(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"actual")
    good = hashlib.sha256(b"actual").hexdigest()
    result = _run(VerifyHashesTool(), {"files": [{"path": "a.txt", "sha256": good}]}, tmp_path)
    assert result["exit_code"] == 0 and result["verified"] is True
    wrong = _run(VerifyHashesTool(), {"files": [{"path": "a.txt", "sha256": "0" * 64}]}, tmp_path)
    assert wrong["exit_code"] == 1 and wrong["verified"] is False
    assert wrong["files"][0]["actual_sha256"] == good


def test_comparison_rejects_escape_sensitive_symlink_and_bad_schema(tmp_path):
    outside = tmp_path.parent / "outside-compare.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    for path in ("../outside-compare.txt", "link.txt", ".env"):
        result = _run(CompareFilesTool(), {"before": "safe.txt", "after": path}, tmp_path)
        assert result["exit_code"] == 1
        assert "outside-compare.txt" not in str(result)
    assert _run(VerifyHashesTool(), {"files": [{"path": "safe.txt", "sha256": "bad"}]},
                tmp_path)["exit_code"] == 1
    assert _run(VerifyHashesTool(), {"files": []}, tmp_path)["exit_code"] == 1


def test_registered_native_tools_dispatch_with_workspace_and_host_fence(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: False)
    (tmp_path / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
    names = {schema["function"]["name"] for schema in FUNCTION_TOOL_SCHEMAS}
    assert {"compare_files", "verify_hashes"} <= names
    block = function_call_to_tool_block("compare_files", {"before": "one.txt", "after": "two.txt"})
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=block.tool_type, content=block.content),
        owner="alice", workspace=str(tmp_path), security_context=ToolRunSecurityContext()))
    assert result["exit_code"] == 0 and result["identical"] is False
    verify = function_call_to_tool_block("verify_hashes", {"files": [{
        "path": "one.txt", "sha256": hashlib.sha256(b"one\n").hexdigest()}]})
    _, checked = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=verify.tool_type, content=verify.content),
        owner="alice", workspace=str(tmp_path), security_context=ToolRunSecurityContext()))
    assert checked["exit_code"] == 0 and checked["verified"] is True
    monkeypatch.setattr(host_execution, "enabled_for", lambda owner: True)
    calls = []
    async def remote(tool, content, **scope):
        calls.append((tool, content, scope))
        return {"exit_code": 0, "execution_host": "jetson", "identical": False}
    monkeypatch.setattr(host_execution, "execute", remote)
    _, remote_result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=block.tool_type, content=block.content),
        owner="alice", workspace=str(tmp_path), security_context=ToolRunSecurityContext()))
    assert remote_result["execution_host"] == "jetson"
    assert calls == [("compare_files", block.content, {})]
