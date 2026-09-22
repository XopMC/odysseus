"""Typed Git status must stay read-only, scoped, and machine-readable."""

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from src.tool_execution import _direct_fallback
from src.tool_schemas import function_call_to_tool_block


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_git_status_reports_staged_unstaged_untracked_without_secret_paths():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-status-tool-") as directory:
        root = Path(directory)
        _assert_git_status_fixture(root)


def _assert_git_status_fixture(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.test")
    (root / "tracked.txt").write_text("before\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "initial")
    head = _git(root, "rev-parse", "HEAD")
    (root / "tracked.txt").write_text("after\n")
    (root / "staged.txt").write_text("staged\n")
    _git(root, "add", "staged.txt")
    (root / "untracked.txt").write_text("untracked\n")
    (root / ".env").write_text("SECRET=must-not-appear\n")
    (root / "line\nbreak.txt").write_text("odd filename\n")

    result = asyncio.run(_direct_fallback("git_status", json.dumps({"path": str(root)})))

    assert result["exit_code"] == 0
    assert result["head"] == head
    files = {entry["path"]: entry for entry in result["files"]}
    assert files["tracked.txt"]["unstaged"] == "modified"
    assert files["staged.txt"]["staged"] == "added"
    assert files["untracked.txt"]["untracked"] is True
    assert files["untracked.txt"]["staged"] is None
    assert files["untracked.txt"]["unstaged"] is None
    assert ".env" not in files
    assert "line\nbreak.txt" not in files
    assert "SECRET" not in result["output"]
    assert _git(root, "rev-parse", "HEAD") == head
    block = function_call_to_tool_block("git_status", json.dumps({"path": str(root)}))
    assert block is not None
    assert asyncio.run(_direct_fallback(block.tool_type, block.content))["head"] == head


def test_git_status_rejects_out_of_scope_path():
    result = asyncio.run(_direct_fallback("git_status", json.dumps({"path": "/etc"})))
    assert result["exit_code"] == 1


def test_git_status_handles_unborn_repository_and_rejects_bad_arguments():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-status-unborn-") as directory:
        root = Path(directory)
        _git(root, "init", "-q")
        (root / "new.txt").write_text("new\n")
        result = asyncio.run(_direct_fallback("git_status", json.dumps({"path": str(root)})))
        assert result["exit_code"] == 0
        assert result["head"] is None
        assert result["files"][0]["path"] == "new.txt"
        assert result["files"][0]["untracked"] is True
        assert asyncio.run(_direct_fallback("git_status", json.dumps({
            "path": str(root), "command": "reset --hard",
        })))["exit_code"] == 1


def test_git_status_rename_and_output_bound():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-status-bounded-") as directory:
        root = Path(directory)
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "Fixture")
        _git(root, "config", "user.email", "fixture@example.test")
        (root / "before.txt").write_text("before\n")
        _git(root, "add", "before.txt")
        _git(root, "commit", "-qm", "initial")
        _git(root, "mv", "before.txt", "after.txt")
        for index in range(120):
            (root / f"extra-{index:03}.txt").write_text("x")
        result = asyncio.run(_direct_fallback("git_status", json.dumps({"path": str(root)})))
        assert result["exit_code"] == 0
        assert result["truncated"] is True
        assert len(result["files"]) <= 100
        renamed = next(entry for entry in result["files"] if entry["path"] == "after.txt")
        assert renamed["staged"] == "renamed"
        assert renamed["previous_path"] == "before.txt"
        assert len(result["output"]) < 7000


def test_git_status_never_falls_back_to_local_repo_for_host_bound_owner(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "alice")
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-status-host-deny-") as directory:
        _git(directory, "init", "-q")
        result = asyncio.run(_direct_fallback(
            "git_status", json.dumps({"path": directory}), owner="alice"))
    assert result["exit_code"] == 1
    assert result["code"] == "not_supported_by_route"
