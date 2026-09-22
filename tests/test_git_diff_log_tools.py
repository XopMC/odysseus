"""Typed Git diff/log are bounded read-only views of an allowed repository."""

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from src.tool_execution import _direct_fallback
from src.tool_schemas import function_call_to_tool_block


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run(tool, args):
    return asyncio.run(_direct_fallback(tool, json.dumps(args)))


def test_git_diff_exposes_only_requested_file_and_exact_blob_hashes():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-diff-tool-") as directory:
        root = Path(directory)
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "Fixture")
        _git(root, "config", "user.email", "fixture@example.test")
        (root / "a.txt").write_text("old\n")
        (root / "b.txt").write_text("other\n")
        (root / ".env").write_text("SECRET=hidden\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "first")
        before = _git(root, "rev-parse", "HEAD:a.txt")
        (root / "a.txt").write_text("new\n")
        (root / "b.txt").write_text("must-not-appear\n")
        after = _git(root, "hash-object", "--no-filters", "a.txt")

        result = _run("git_diff", {"path": str(root), "file": "a.txt"})
        assert result["exit_code"] == 0
        assert result["before_hash"] == before
        assert result["after_hash"] == after
        assert "-old" in result["patch"] and "+new" in result["patch"]
        assert "must-not-appear" not in result["patch"]
        assert _run("git_diff", {"path": str(root), "file": ".env"})["exit_code"] == 1
        assert _run("git_diff", {"path": str(root), "file": "../outside"})["exit_code"] == 1

        block = function_call_to_tool_block("git_diff", json.dumps({
            "path": str(root), "file": "a.txt",
        }))
        assert block is not None
        assert _run(block.tool_type, json.loads(block.content))["before_hash"] == before


def test_git_log_is_structured_bounded_and_does_not_run_model_arguments():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-log-tool-") as directory:
        root = Path(directory)
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "Fixture")
        _git(root, "config", "user.email", "fixture@example.test")
        (root / "a.txt").write_text("first\n")
        _git(root, "add", "a.txt")
        _git(root, "commit", "-qm", "first")
        first = _git(root, "rev-parse", "HEAD")
        (root / "a.txt").write_text("second\n")
        _git(root, "add", "a.txt")
        _git(root, "commit", "-qm", "second")
        second = _git(root, "rev-parse", "HEAD")

        result = _run("git_log", {"path": str(root), "limit": 1})
        assert result["exit_code"] == 0
        assert len(result["commits"]) == 1
        assert result["commits"][0]["hash"] == second
        assert result["commits"][0]["parents"] == [first]
        assert result["commits"][0]["subject"] == "second"
        assert _run("git_log", {"path": str(root), "limit": 1000})["exit_code"] == 1
        assert _run("git_log", {"path": "/etc"})["exit_code"] == 1
        assert _run("git_log", {"path": str(root), "command": "reset --hard"})["exit_code"] == 1
        assert _git(root, "rev-parse", "HEAD") == second


def test_git_diff_staged_hashes_and_host_bound_routes_fail_closed(monkeypatch):
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="git-diff-staged-") as directory:
        root = Path(directory)
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "Fixture")
        _git(root, "config", "user.email", "fixture@example.test")
        (root / "a.txt").write_text("old\n")
        _git(root, "add", "a.txt")
        _git(root, "commit", "-qm", "first")
        before = _git(root, "rev-parse", "HEAD:a.txt")
        (root / "a.txt").write_text("staged\n")
        _git(root, "add", "a.txt")
        after = _git(root, "rev-parse", ":a.txt")
        (root / "a.txt").write_text("unstaged\n")

        result = _run("git_diff", {"path": str(root), "file": "a.txt", "staged": True})
        assert result["exit_code"] == 0
        assert result["before_hash"] == before
        assert result["after_hash"] == after
        assert "+staged" in result["patch"]
        assert "unstaged" not in result["patch"]

        monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
        monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "alice")
        for tool, args in (("git_diff", {"path": str(root), "file": "a.txt"}),
                           ("git_log", {"path": str(root)})):
            blocked = asyncio.run(_direct_fallback(tool, json.dumps(args), owner="alice"))
            assert blocked["code"] == "not_supported_by_route"
            assert blocked["exit_code"] == 1
