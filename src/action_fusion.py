"""Safe edit/write/patch plus an exact follow-up verification command."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import os
import re
from typing import AsyncIterator


FUSIBLE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


def parse(tool: str, content: str) -> dict | None:
    if tool not in FUSIBLE_TOOLS:
        return None
    raw = str(content or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(args, dict) or "verify" not in args:
        return None
    verify = args.get("verify")
    if not isinstance(verify, dict) or set(verify) - {"command", "timeout_seconds"}:
        raise ValueError("verify must contain only command and optional timeout_seconds")
    command = str(verify.get("command") or "").strip()
    if not command or len(command) > 20_000 or "\0" in command:
        raise ValueError("verify.command is missing or too large")
    try:
        timeout = int(verify.get("timeout_seconds") or 300)
    except (TypeError, ValueError):
        raise ValueError("verify.timeout_seconds must be an integer") from None
    if not 1 <= timeout <= 3600:
        raise ValueError("verify.timeout_seconds must be between 1 and 3600")
    clean = dict(args)
    clean.pop("verify", None)
    if tool == "apply_patch":
        patch = str(clean.get("patch_text") or clean.get("patchText") or clean.get("patch") or "")
        paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
    else:
        paths = [str(clean.get("path") or "")]
    paths = [path.strip() for path in paths if path.strip()]
    return {
        "clean_content": json.dumps(clean, ensure_ascii=False),
        "command": command,
        "timeout_seconds": timeout,
        "paths": paths,
    }


def lock_keys(paths: list[str], workspace: str | None) -> list[str]:
    root = workspace or os.getcwd()
    return sorted({os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path)) for path in paths})


def fingerprints(paths: list[str], workspace: str | None) -> dict[str, str]:
    """Return stable post-mutation fingerprints used to fence verification."""
    found: dict[str, str] = {}
    for path in lock_keys(paths, workspace):
        try:
            if os.path.islink(path):
                found[path] = "symlink"
            elif not os.path.exists(path):
                found[path] = "missing"
            elif not os.path.isfile(path):
                found[path] = "not-file"
            else:
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                found[path] = digest.hexdigest()
        except OSError as exc:
            found[path] = f"error:{type(exc).__name__}"
    return found


@asynccontextmanager
async def hold(paths: list[str], workspace: str | None) -> AsyncIterator[None]:
    keys = lock_keys(paths, workspace) or ["__workspace__:" + os.path.realpath(workspace or os.getcwd())]
    async with _locks_guard:
        locks = [_locks.setdefault(key, asyncio.Lock()) for key in keys]
    for lock in locks:
        await lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()
