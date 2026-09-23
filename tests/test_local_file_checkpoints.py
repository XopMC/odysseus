import hashlib
import json
from types import SimpleNamespace

import pytest

from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block


def _block(name, payload):
    return SimpleNamespace(tool_type=name, content=json.dumps(payload))


@pytest.mark.asyncio
async def test_agent_local_write_returns_durable_checkpoint_and_exact_rollback(tmp_path, monkeypatch):
    from src import local_file_checkpoints

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "answer.txt"
    target.write_text("before\n")
    monkeypatch.setattr(local_file_checkpoints, "DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(local_file_checkpoints, "_RUNNER", None)
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    monkeypatch.setattr("src.host_execution.enabled_for", lambda _owner: False)

    _, written = await execute_tool_block(
        _block("write_file", {"path": str(target), "content": "after\n"}),
        session_id="session-local-1", owner="alice", workspace=str(workspace),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert written["exit_code"] == 0
    checkpoint = written["file_checkpoint"]
    assert checkpoint["backend"] == "container"
    assert checkpoint["run_id"] is None
    assert target.read_text() == "after\n"
    assert checkpoint["files"][0]["before_sha256"] == hashlib.sha256(b"before\n").hexdigest()
    assert checkpoint["files"][0]["after_sha256"] == hashlib.sha256(b"after\n").hexdigest()

    # Simulate app-process restart between the mutation and user-authorized
    # rollback; checkpoint index and before-image must reload from DATA_DIR.
    monkeypatch.setattr(local_file_checkpoints, "_RUNNER", None)

    _, rolled_back = await execute_tool_block(
        _block("rollback_file_checkpoint", {
            "checkpoint_id": checkpoint["id"],
            "expected_sha256": {str(target): checkpoint["files"][0]["after_sha256"]},
        }),
        session_id="session-local-1", owner="alice", workspace=str(workspace),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert rolled_back.get("status") == "rolled_back", rolled_back
    assert target.read_text() == "before\n"


@pytest.mark.asyncio
async def test_agent_local_rollback_refuses_to_overwrite_intervening_user_edit(tmp_path, monkeypatch):
    from src import local_file_checkpoints

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "answer.txt"
    target.write_text("before\n")
    monkeypatch.setattr(local_file_checkpoints, "DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(local_file_checkpoints, "_RUNNER", None)
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    monkeypatch.setattr("src.host_execution.enabled_for", lambda _owner: False)

    _, written = await execute_tool_block(
        _block("write_file", {"path": str(target), "content": "agent edit\n"}),
        session_id="session-local-2", owner="alice", workspace=str(workspace),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    target.write_text("user edit after checkpoint\n")
    checkpoint = written["file_checkpoint"]

    _, rollback = await execute_tool_block(
        _block("rollback_file_checkpoint", {
            "checkpoint_id": checkpoint["id"],
            "expected_sha256": {str(target): checkpoint["files"][0]["after_sha256"]},
        }),
        session_id="session-local-2", owner="alice", workspace=str(workspace),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert rollback["exit_code"] == 1
    assert rollback.get("code") == "rollback_refused", rollback
    assert target.read_text() == "user edit after checkpoint\n"


@pytest.mark.asyncio
async def test_local_edit_and_apply_patch_also_receive_agent_owned_checkpoints(tmp_path, monkeypatch):
    from src import local_file_checkpoints

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "module.txt"
    before = b"first\nsecond\n"
    target.write_bytes(before)
    monkeypatch.setattr(local_file_checkpoints, "DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(local_file_checkpoints, "_RUNNER", None)
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    monkeypatch.setattr("src.host_execution.enabled_for", lambda _owner: False)

    _, edited = await execute_tool_block(
        _block("edit_file", {
            "path": str(target), "old_string": "first", "new_string": "initial",
            "expected_sha256": hashlib.sha256(before).hexdigest(),
        }),
        session_id="session-local-3", owner="alice", workspace=str(workspace),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert edited["exit_code"] == 0
    assert edited["file_checkpoint"]["files"][0]["before_sha256"] == hashlib.sha256(before).hexdigest()

    current = target.read_bytes()
    patch_text = (
        "*** Begin Patch\n*** Update File: " + str(target) +
        "\n@@\n-initial\n+verified\n*** End Patch"
    )
    _, patched = await execute_tool_block(
        _block("apply_patch", {
            "patch_text": patch_text,
            "expected_sha256_by_path": {str(target): hashlib.sha256(current).hexdigest()},
        }),
        session_id="session-local-3", owner="alice", workspace=str(workspace),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert patched["exit_code"] == 0
    assert patched["file_checkpoint"]["backend"] == "container"
    assert target.read_text() == "verified\nsecond\n"


@pytest.mark.asyncio
async def test_local_checkpoint_rollback_is_owner_and_chat_scoped(tmp_path, monkeypatch):
    from src import local_file_checkpoints

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.txt"
    target.write_text("before\n")
    monkeypatch.setattr(local_file_checkpoints, "DATA_DIR", str(tmp_path / "app-data"))
    monkeypatch.setattr(local_file_checkpoints, "_RUNNER", None)
    record = local_file_checkpoints.begin(
        "alice", "session-a", [str(target)], run_id="run-a")
    target.write_text("after\n")
    checkpoint = local_file_checkpoints.finish(record, True)
    after = {str(target): checkpoint["files"][0]["after_sha256"]}

    wrong_owner = local_file_checkpoints.rollback("bob", "session-a", checkpoint["id"], after)
    assert wrong_owner["exit_code"] == 1
    refused = local_file_checkpoints.rollback("alice", "session-b", checkpoint["id"], after)

    assert wrong_owner["code"] == "rollback_refused"
    assert refused["exit_code"] == 1
    assert refused["code"] == "rollback_refused"
    assert target.read_text() == "after\n"
