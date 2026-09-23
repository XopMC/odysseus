"""File edits must reject stale revisions and syntax errors before writing."""
import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest

from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_schemas import function_call_to_tool_block


def _block(tool, payload):
    return SimpleNamespace(tool_type=tool, content=json.dumps(payload, ensure_ascii=False))


def test_native_apply_patch_keeps_hash_and_syntax_preconditions():
    payload = {
        "patch_text": "*** Begin Patch\n*** Update File: x.py\n@@\n-old\n+new\n*** End Patch",
        "expected_sha256_by_path": {"x.py": "a" * 64},
        "validate_syntax": True,
    }
    block = function_call_to_tool_block("apply_patch", json.dumps(payload))

    assert block is not None
    assert json.loads(block.content) == payload


@pytest.mark.asyncio
async def test_edit_file_rejects_stale_sha_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "module.py"
    original = b"def f():\n    return 1\n"
    path.write_bytes(original)

    _desc, result = await execute_tool_block(
        _block("edit_file", {
            "path": str(path), "old_string": "return 1", "new_string": "return 2",
            "expected_sha256": "0" * 64,
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 1
    assert result["code"] == "stale_revision"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_edit_file_requires_hash_precondition(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "module.txt"
    original = b"before\n"
    path.write_bytes(original)
    _desc, result = await execute_tool_block(
        _block("edit_file", {"path": str(path), "old_string": "before", "new_string": "after"}),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert result["code"] == "precondition_required"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_edit_file_syntax_gate_preserves_original_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "module.py"
    original = b"def f():\n    return 1\n"
    path.write_bytes(original)
    path.chmod(0o640)
    digest = hashlib.sha256(original).hexdigest()

    _desc, result = await execute_tool_block(
        _block("edit_file", {
            "path": str(path), "old_string": "return 1", "new_string": "return )",
            "expected_sha256": digest,
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 1
    assert result["code"] == "syntax_error"
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_edit_file_success_keeps_mode_and_reports_hash_precondition(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "module.py"
    original = b"def f():\n    return 1\n"
    path.write_bytes(original)
    path.chmod(0o640)
    original_stat = path.stat()

    _desc, result = await execute_tool_block(
        _block("edit_file", {
            "path": str(path), "old_string": "return 1", "new_string": "return 2",
            "expected_sha256": hashlib.sha256(original).hexdigest(),
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 0
    assert result["hash_precondition"] == "matched"
    assert result["syntax_check"] == {"status": "passed", "parser": "python_ast"}
    assert path.read_text() == "def f():\n    return 2\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert path.stat().st_uid == original_stat.st_uid
    assert path.stat().st_gid == original_stat.st_gid


@pytest.mark.asyncio
async def test_edit_file_javascript_syntax_gate_is_non_executing(tmp_path, monkeypatch):
    if not shutil.which("node"):
        pytest.skip("node syntax parser is unavailable")
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "module.js"
    original = b"export function answer() { return 42; }\n"
    path.write_bytes(original)

    _desc, result = await execute_tool_block(
        _block("edit_file", {
            "path": str(path), "old_string": "return 42;", "new_string": "return );",
            "expected_sha256": hashlib.sha256(original).hexdigest(),
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 1
    assert result["code"] == "syntax_error"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_edit_file_cannot_disable_required_syntax_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "module.py"
    original = b"def f():\n    return 1\n"
    path.write_bytes(original)
    _desc, result = await execute_tool_block(
        _block("edit_file", {
            "path": str(path), "old_string": "return 1", "new_string": "return )",
            "expected_sha256": hashlib.sha256(original).hexdigest(), "validate_syntax": False,
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert result["code"] == "validation_required"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_edit_file_refuses_hardlinks_without_changing_either_name(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path, alias = tmp_path / "source.py", tmp_path / "alias.py"
    original = b"def f():\n    return 1\n"
    path.write_bytes(original)
    alias.hardlink_to(path)
    _desc, result = await execute_tool_block(
        _block("edit_file", {
            "path": str(path), "old_string": "return 1", "new_string": "return 2",
            "expected_sha256": hashlib.sha256(original).hexdigest(),
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert result["exit_code"] == 1
    assert "hard-linked file" in result["error"]
    assert path.read_bytes() == alias.read_bytes() == original


@pytest.mark.asyncio
async def test_apply_patch_preflights_all_syntax_before_mutating_any_file(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    first = tmp_path / "first.txt"
    second = tmp_path / "settings.json"
    first_raw = b"old\n"
    second_raw = b'{"ok": true}\n'
    first.write_bytes(first_raw)
    second.write_bytes(second_raw)
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {first}\n@@\n-old\n+new\n"
        f"*** Update File: {second}\n@@\n-{{\"ok\": true}}\n+{{\"ok\":\n"
        "*** End Patch"
    )

    _desc, result = await execute_tool_block(
        _block("apply_patch", {
            "patch_text": patch_text,
            "expected_sha256_by_path": {
                str(first): hashlib.sha256(first_raw).hexdigest(),
                str(second): hashlib.sha256(second_raw).hexdigest(),
            },
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 1
    assert result["code"] == "syntax_error"
    assert first.read_bytes() == first_raw
    assert second.read_bytes() == second_raw


@pytest.mark.asyncio
async def test_apply_patch_rejects_stale_hash_without_touching_files(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "settings.json"
    original = b'{"ok": true}\n'
    path.write_bytes(original)
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n@@\n-{{\"ok\": true}}\n+{{\"ok\": false}}\n"
        "*** End Patch"
    )

    _desc, result = await execute_tool_block(
        _block("apply_patch", {
            "patch_text": patch_text,
            "expected_sha256_by_path": {str(path): "f" * 64},
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 1
    assert result["code"] == "stale_revision"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_apply_patch_requires_hash_for_each_path(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    path = tmp_path / "settings.json"
    original = b'{"ok": true}\n'
    path.write_bytes(original)
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n@@\n-{{\"ok\": true}}\n+{{\"ok\": false}}\n"
        "*** End Patch"
    )
    _desc, result = await execute_tool_block(
        _block("apply_patch", {"patch_text": patch_text}),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert result["code"] == "precondition_required"
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_apply_patch_io_failure_rolls_back_prior_file(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda _owner: True)
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first_raw, second_raw = b"old-a\n", b"old-b\n"
    first.write_bytes(first_raw)
    second.write_bytes(second_raw)
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {first}\n@@\n-old-a\n+new-a\n"
        f"*** Update File: {second}\n@@\n-old-b\n+new-b\n"
        "*** End Patch"
    )
    import core.atomic_io as atomic_io
    real_write = atomic_io.atomic_write_text
    calls = 0

    def fail_second_write(path, text, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", fail_second_write)
    _desc, result = await execute_tool_block(
        _block("apply_patch", {
            "patch_text": patch_text,
            "expected_sha256_by_path": {
                str(first): hashlib.sha256(first_raw).hexdigest(),
                str(second): hashlib.sha256(second_raw).hexdigest(),
            },
        }),
        workspace=str(tmp_path), security_context=NO_TOOL_SECURITY_CONTEXT,
    )

    assert result["exit_code"] == 1
    assert result["code"] == "patch_commit_failed"
    assert first.read_bytes() == first_raw
    assert second.read_bytes() == second_raw
