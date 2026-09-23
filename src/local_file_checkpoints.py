"""Durable checkpoints for in-container Agent/Goal file mutations.

The Jetson route delegates to the registered host runner. Local workspace
mutations use the same tested checkpoint/rollback implementation with a
separate app-owned state directory under DATA_DIR.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any

from src.constants import DATA_DIR


_LOCK = threading.RLock()
_RUNNER = None
_LOCAL_ID = re.compile(r"^local_[0-9a-f]{32}$")


def _runner():
    global _RUNNER
    with _LOCK:
        if _RUNNER is not None:
            return _RUNNER
        root = Path(DATA_DIR).expanduser().resolve()
        state = root / "agent-file-checkpoints"
        if state.is_symlink():
            raise OSError("Local checkpoint state must not be a symlink")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state, 0o700)
        if state.resolve().parent != root:
            raise OSError("Local checkpoint state escaped the application data directory")
        module_path = Path(__file__).resolve().parents[1] / "scripts" / "host_runner.py"
        spec = importlib.util.spec_from_file_location("odysseus_local_file_checkpoint_runner", module_path)
        if spec is None or spec.loader is None:
            raise ImportError("File checkpoint engine is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The reusable runner is normally launched as a script, which makes
        # its sibling host_files module importable by name. Load that exact
        # repository-owned sibling explicitly for the in-process backend; do
        # not add a workspace or model-controlled directory to sys.path.
        host_files_name = "host_files"
        host_files_path = module_path.with_name("host_files.py").resolve()
        loaded_host_files = sys.modules.get(host_files_name)
        if loaded_host_files is not None:
            if Path(getattr(loaded_host_files, "__file__", "")).resolve() != host_files_path:
                raise ImportError("A different host_files module is already loaded")
        else:
            host_files_spec = importlib.util.spec_from_file_location(host_files_name, host_files_path)
            if host_files_spec is None or host_files_spec.loader is None:
                raise ImportError("Host file checkpoint operations are unavailable")
            host_files_module = importlib.util.module_from_spec(host_files_spec)
            sys.modules[host_files_name] = host_files_module
            host_files_spec.loader.exec_module(host_files_module)
        _RUNNER = module.Runner(state)
        return _RUNNER


def begin(owner: str, session_id: str, paths: list[str], run_id: str | None = None) -> dict:
    if not isinstance(owner, str) or not owner or not isinstance(session_id, str) or not session_id:
        raise ValueError("owner and chat scope are required for file checkpoints")
    runner = _runner()
    with runner.lock:
        record = runner._begin_file_checkpoint(
            paths, owner, session_id, run_id=run_id, backend="container")
        return record


def finish(record: dict, success: bool) -> dict:
    runner = _runner()
    with runner.lock:
        runner._finish_file_checkpoint(record, success)
        result = runner._public_file_checkpoint(record)
        result["id"] = "local_" + record["id"]
        result["backend"] = "container"
        return result


def rollback(owner: str, session_id: str, checkpoint_id: str, expected_sha256: dict) -> dict:
    if not _LOCAL_ID.fullmatch(checkpoint_id or ""):
        raise ValueError("invalid local file checkpoint id")
    runner = _runner()
    result = runner.handle({
        "op": "file.rollback",
        "owner": owner,
        "scope": session_id,
        "args": {"checkpoint_id": checkpoint_id.removeprefix("local_"),
                 "expected_sha256": expected_sha256},
    })
    if not result.get("ok"):
        return {"error": result.get("error") or "Local checkpoint rollback refused",
                "code": result.get("code") or "rollback_refused", "exit_code": 1}
    payload = result.get("result") or {}
    payload["checkpoint_id"] = checkpoint_id
    payload["backend"] = "container"
    payload["exit_code"] = 0
    return payload


async def rollback_scoped(owner: str, session_id: str, checkpoint_id: str,
                          expected_sha256: dict) -> dict:
    """Serialize rollback with ordinary/fused Agent mutations by canonical path."""
    if not _LOCAL_ID.fullmatch(checkpoint_id or ""):
        raise ValueError("invalid local file checkpoint id")
    runner = _runner()
    raw_id = checkpoint_id.removeprefix("local_")
    record = runner.record("file_checkpoints", raw_id, owner, session_id)
    paths = [item["path"] for item in record.get("files", [])]
    from src.action_fusion import hold
    async with hold(paths, None):
        return rollback(owner, session_id, checkpoint_id, expected_sha256)
