import json
import asyncio
from types import SimpleNamespace

import pytest

from src.tool_capabilities import ToolEffect, capabilities_for_action
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_schemas import function_call_to_tool_block
from src.action_fusion import mutation_paths, remote_fingerprint_command, remote_fenced_verify_command
import subprocess


def _write(path, text, command="test -s sample.txt"):
    return SimpleNamespace(
        tool_type="write_file",
        content=json.dumps({
            "path": str(path),
            "content": text,
            "verify": {"command": command, "timeout_seconds": 10},
        }),
    )


@pytest.mark.asyncio
async def test_fused_write_runs_verification(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    block = _write(tmp_path / "sample.txt", "hello", "grep -q hello sample.txt")
    desc, result = await execute_tool_block(
        block, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT
    )
    assert desc == "write_file+verify"
    assert result["exit_code"] == 0
    assert result["fused"] is True
    assert (tmp_path / "sample.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_fused_failure_preserves_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    block = _write(tmp_path / "sample.txt", "kept", "false")
    _, result = await execute_tool_block(
        block, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT
    )
    assert result["exit_code"] != 0
    assert result["verification"]["exit_code"] != 0
    assert (tmp_path / "sample.txt").read_text() == "kept"


@pytest.mark.asyncio
async def test_disabled_verification_prevents_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    block = _write(tmp_path / "sample.txt", "never")
    _, result = await execute_tool_block(
        block,
        workspace=str(tmp_path),
        disabled_tools={"bash"},
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert result["exit_code"] != 0
    assert not (tmp_path / "sample.txt").exists()


def test_native_conversion_and_capability_preserve_verify():
    block = function_call_to_tool_block("write_file", json.dumps({
        "path": "x", "content": "y", "verify": {"command": "true"}
    }))
    assert json.loads(block.content)["verify"]["command"] == "true"
    capabilities = capabilities_for_action(block.tool_type, block.content)
    assert ToolEffect.WRITE_WORKSPACE in capabilities.effects
    assert ToolEffect.EXECUTE_CODE in capabilities.effects


@pytest.mark.asyncio
async def test_mutation_failure_skips_verification(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    marker = tmp_path / "must-not-exist"
    block = SimpleNamespace(tool_type="edit_file", content=json.dumps({
        "path": str(tmp_path / "missing.txt"), "old_string": "a", "new_string": "b",
        "verify": {"command": f"touch {marker}"},
    }))
    _, result = await execute_tool_block(
        block, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT
    )
    assert result["verification_skipped"] is True
    assert not marker.exists()


@pytest.mark.asyncio
async def test_same_path_fusions_are_serialized(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    first = _write(tmp_path / "sample.txt", "first", "sleep 0.05; grep -q first sample.txt")
    second = _write(tmp_path / "sample.txt", "second", "grep -q second sample.txt")
    results = await asyncio.gather(*(
        execute_tool_block(block, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT)
        for block in (first, second)
    ))
    assert [result[1]["exit_code"] for result in results] == [0, 0]
    assert (tmp_path / "sample.txt").read_text() == "second"


@pytest.mark.asyncio
async def test_ordinary_write_cannot_interleave_before_fused_verification(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    path = tmp_path / "sample.txt"
    fused = _write(path, "first", "sleep 0.05; grep -qx first sample.txt")
    ordinary = SimpleNamespace(tool_type="write_file", content=json.dumps({
        "path": str(path), "content": "second",
    }))
    first, second = await asyncio.gather(
        execute_tool_block(fused, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT),
        execute_tool_block(ordinary, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT),
    )
    assert first[1]["exit_code"] == 0
    assert second[1]["exit_code"] == 0
    assert path.read_text() == "second"


def test_legacy_mutation_payloads_use_the_same_path_keys(tmp_path):
    path = str(tmp_path / "sample.txt")
    assert mutation_paths("write_file", f"{path}\nnew bytes") == [path]
    patch = f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch"
    assert mutation_paths("apply_patch", patch) == [path]
    assert mutation_paths("write_file", '{"unusual":"path"}\nbody') == ['{"unusual":"path"}']


@pytest.mark.asyncio
async def test_legacy_write_cannot_interleave_before_fused_verification(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    path = tmp_path / "sample.txt"
    fused = _write(path, "first", "sleep 0.05; grep -qx first sample.txt")
    legacy = SimpleNamespace(tool_type="write_file", content=f"{path}\nsecond")
    first, second = await asyncio.gather(
        execute_tool_block(fused, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT),
        execute_tool_block(legacy, workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT),
    )
    assert first[1]["exit_code"] == 0
    assert second[1]["exit_code"] == 0
    assert path.read_text() == "second"


def test_remote_fence_rejects_changed_file(tmp_path):
    path = tmp_path / "remote.txt"
    path.write_text("first")
    probe = subprocess.run(remote_fingerprint_command([str(path)], None), shell=True,
                           capture_output=True, text=True, check=True)
    expected = json.loads(probe.stdout)
    path.write_text("changed")
    guarded = subprocess.run(remote_fenced_verify_command(expected, "touch should-not-exist"),
                             shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert guarded.returncode == 73
    assert not (tmp_path / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_host_fusion_fingerprints_then_runs_guarded_verify(tmp_path, monkeypatch):
    monkeypatch.setattr("src.harness_efficiency.get_setting", lambda *a: "performance")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: True)
    monkeypatch.setattr("src.host_execution.enabled_for", lambda owner: owner == "alice")
    calls = []
    path = str(tmp_path / "sample.txt")
    async def fake_execute(tool, content):
        calls.append((tool, content))
        if tool == "bash" and "print(json.dumps(out" in content:
            return {"output": json.dumps({path: "abc"}), "exit_code": 0}
        return {"output": "ok", "exit_code": 0}
    monkeypatch.setattr("src.host_execution.execute", fake_execute)
    _, result = await execute_tool_block(
        _write(path, "hello", "test -s sample.txt"), owner="alice",
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert result["exit_code"] == 0 and len(calls) == 3
    assert calls[0][0] == "write_file"
    assert calls[1][0] == "bash" and "print(json.dumps(out" in calls[1][1]
    assert calls[2][0] == "bash" and "Action Fusion fence mismatch" in calls[2][1]
