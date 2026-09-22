"""Safe edit/write/patch plus an exact follow-up verification command."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import os
import re
import base64
import shlex
from typing import AsyncIterator
from urllib.parse import unquote, urlparse


FUSIBLE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


def mutation_paths(tool: str, content: str) -> list[str]:
    """Extract every file target from an ordinary or fused mutation."""
    if tool not in FUSIBLE_TOOLS:
        return []
    raw = str(content or "")
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        args = None
    if tool == "apply_patch":
        patch = (
            str(args.get("patch_text") or args.get("patchText") or args.get("patch") or "")
            if isinstance(args, dict) else raw
        )
        return [path.strip() for path in re.findall(
            r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE,
        ) if path.strip()]
    if tool == "write_file" and not (isinstance(args, dict) and "path" in args):
        # Native calls without a fused verify use the legacy path\nbody form.
        # The executor accepts it, so it must acquire the same path lock.
        path = raw.split("\n", 1)[0].strip()
    else:
        path = str(args.get("path", "")).strip() if isinstance(args, dict) else ""
    return [path] if path else []


def _expand_tool_path(path: str, workspace: str | None) -> str:
    value = path[1:] if path.startswith("@") else path
    if value.startswith("file://"):
        parsed = urlparse(value)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("Only local file URLs can be queued")
        value = unquote(parsed.path)
    value = os.path.expanduser(value)
    root = workspace or os.getcwd()
    return os.path.realpath(value if os.path.isabs(value) else os.path.join(root, value))


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
    paths = mutation_paths(tool, json.dumps(clean, ensure_ascii=False))
    return {
        "clean_content": json.dumps(clean, ensure_ascii=False),
        "command": command,
        "timeout_seconds": timeout,
        "paths": paths,
    }


def lock_keys(paths: list[str], workspace: str | None) -> list[str]:
    return sorted({_expand_tool_path(path, workspace) for path in paths})


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


def remote_fingerprint_command(paths: list[str], workspace: str | None) -> str:
    """Build a data-only host command that fingerprints exact resolved paths."""
    payload = base64.urlsafe_b64encode(json.dumps(lock_keys(paths, workspace)).encode()).decode()
    script = (
        "import base64,hashlib,json,os,sys;"
        "ps=json.loads(base64.urlsafe_b64decode(sys.argv[1]));out={};"
        "\nfor p in ps:\n"
        " try:\n"
        "  if os.path.islink(p): v='symlink'\n"
        "  elif not os.path.exists(p): v='missing'\n"
        "  elif not os.path.isfile(p): v='not-file'\n"
        "  else:\n"
        "   h=hashlib.sha256()\n"
        "   with open(p,'rb') as f:\n"
        "    for c in iter(lambda:f.read(1048576),b''): h.update(c)\n"
        "   v=h.hexdigest()\n"
        " except OSError as e: v='error:'+type(e).__name__\n"
        " out[p]=v\n"
        "print(json.dumps(out,sort_keys=True,separators=(',',':')))"
    )
    return "python3 -c " + shlex.quote(script) + " " + shlex.quote(payload)


def remote_fenced_verify_command(expected: dict[str, str], command: str) -> str:
    """Verify remote fingerprints and run the requested command in one host call."""
    payload = base64.urlsafe_b64encode(json.dumps(expected, sort_keys=True).encode()).decode()
    script = (
        "import base64,hashlib,json,os,sys;"
        "exp=json.loads(base64.urlsafe_b64decode(sys.argv[1]));"
        "\nfor p,want in exp.items():\n"
        " try:\n"
        "  if os.path.islink(p): got='symlink'\n"
        "  elif not os.path.exists(p): got='missing'\n"
        "  elif not os.path.isfile(p): got='not-file'\n"
        "  else:\n"
        "   h=hashlib.sha256()\n"
        "   with open(p,'rb') as f:\n"
        "    for c in iter(lambda:f.read(1048576),b''): h.update(c)\n"
        "   got=h.hexdigest()\n"
        " except OSError as e: got='error:'+type(e).__name__\n"
        " if got!=want:\n"
        "  print('Action Fusion fence mismatch: '+p,file=sys.stderr);sys.exit(73)\n"
        "os.execvp('bash',['bash','-lc',sys.argv[2]])"
    )
    return "python3 -c " + shlex.quote(script) + " " + shlex.quote(payload) + " " + shlex.quote(command)


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
