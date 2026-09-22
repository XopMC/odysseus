"""Bounded, read-only Git inspection for the local Agent workspace."""

from __future__ import annotations

import asyncio
import json
import os
import re
import selectors
import subprocess
import time


_STATUS_BYTES = 256 * 1024
_STATUS_FILES = 100
_DIFF_BYTES = 32 * 1024
_LOG_BYTES = 16 * 1024
_DEADLINE_SECONDS = 8
_CODES = {
    "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "U": "unmerged", "T": "type_changed", "?": "untracked",
}


def _git_small(cwd: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", cwd, *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=_DEADLINE_SECONDS, check=False,
        env={**os.environ, "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("not an accessible Git repository")
    return result.stdout.decode("utf-8", "replace").strip()


def _git_bounded_bytes(cwd: str, args: list[str], maximum: int) -> tuple[bytes, bool]:
    process = subprocess.Popen(
        ["git", "--no-optional-locks", "-C", cwd, "-c", "core.fsmonitor=false", *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"},
    )
    chunks, size, truncated = [], 0, False
    deadline = time.monotonic() + _DEADLINE_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Git status deadline exceeded")
                if not selector.select(remaining):
                    raise TimeoutError("Git status deadline exceeded")
                chunk = os.read(process.stdout.fileno(), min(8192, maximum + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    truncated = True
                    break
        if truncated:
            process.kill()
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if not truncated and process.returncode:
            raise ValueError("Git inspection unavailable")
        return b"".join(chunks)[:maximum], truncated
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        if process.stdout:
            process.stdout.close()


def _git_status_bytes(cwd: str) -> tuple[bytes, bool]:
    return _git_bounded_bytes(
        cwd, ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        _STATUS_BYTES,
    )


def _repository(cwd: str) -> tuple[str, str | None]:
    from src.tool_execution import _resolve_search_root

    requested = _resolve_search_root(cwd)
    if not os.path.isdir(requested):
        raise ValueError("path is not a directory")
    root = os.path.realpath(_git_small(requested, "rev-parse", "--show-toplevel"))
    _resolve_search_root(root)
    try:
        head = _git_small(root, "rev-parse", "--verify", "HEAD")
    except ValueError:
        head = None
    return root, head


def _safe_file(root: str, raw: str) -> str:
    from src.tool_execution import _is_denied_tool_path

    if (not isinstance(raw, str) or not raw or len(raw) > 300 or os.path.isabs(raw)
            or ".." in raw.split("/") or "\\" in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise ValueError("invalid relative file path")
    lexical = os.path.join(root, raw)
    resolved = os.path.realpath(lexical)
    if (os.path.commonpath((root, resolved)) != root or os.path.islink(lexical)
            or _is_denied_tool_path(resolved)):
        raise ValueError("file is outside the allowed repository or is sensitive")
    return raw


def _optional_hash(root: str, *args: str) -> str | None:
    try:
        value = _git_small(root, *args)
    except ValueError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None


def _diff(cwd: str, file: str, staged: bool) -> dict:
    root, head = _repository(cwd)
    relative = _safe_file(root, file)
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames",
            "--no-color", "--unified=3"]
    if staged:
        args.append("--cached")
    args.extend(("--", relative))
    raw, truncated = _git_bounded_bytes(root, args, _DIFF_BYTES)
    patch = raw.decode("utf-8", "replace")
    if staged:
        before = _optional_hash(root, "rev-parse", "--verify", f"HEAD:{relative}")
        after = _optional_hash(root, "rev-parse", "--verify", f":{relative}")
    else:
        before = _optional_hash(root, "rev-parse", "--verify", f":{relative}")
        current = os.path.join(root, relative)
        after = (_optional_hash(root, "hash-object", "--no-filters", "--", relative)
                 if os.path.isfile(current) else None)
    return {"repository": root, "head": head, "file": relative,
            "staged": staged, "before_hash": before, "after_hash": after,
            "patch": patch, "output": patch or "(no diff)",
            "truncated": truncated, "exit_code": 0}


def _log(cwd: str, limit: int, file: str | None) -> dict:
    root, head = _repository(cwd)
    args = ["log", "--no-show-signature", f"--max-count={limit}",
            "--pretty=format:%H%x1f%P%x1f%aI%x1f%s%x1e"]
    if file is not None:
        args.extend(("--", _safe_file(root, file)))
    if head is None:
        return {"repository": root, "head": None, "commits": [],
                "output": "(no commits)", "truncated": False, "exit_code": 0}
    raw, truncated = _git_bounded_bytes(root, args, _LOG_BYTES)
    commits = []
    for record in raw.decode("utf-8", "replace").split("\x1e"):
        fields = record.strip("\r\n").split("\x1f")
        if len(fields) != 4 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
            continue
        subject = re.sub(r"[\x00-\x1f\x7f]", " ", fields[3])[:200]
        commits.append({"hash": fields[0], "parents": fields[1].split() if fields[1] else [],
                        "author_date": fields[2], "subject": subject})
    output = "\n".join(f"{entry['hash'][:12]} {entry['subject']}" for entry in commits)
    if truncated:
        output += "\n[Log truncated]"
    return {"repository": root, "head": head, "commits": commits,
            "output": output or "(no commits)", "truncated": truncated, "exit_code": 0}


def _status(cwd: str) -> dict:
    from src.tool_execution import _is_denied_tool_path

    root, head = _repository(cwd)
    raw, truncated = _git_status_bytes(root)
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    else:
        records = records[:-1]
        truncated = True
    files = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            truncated = True
            continue
        x, y = chr(record[0]), chr(record[1])
        path = record[3:].decode("utf-8", "replace")
        previous = None
        if x in "RC" or y in "RC":
            if index >= len(records):
                truncated = True
                break
            previous = records[index].decode("utf-8", "replace")
            index += 1
        paths = [path] + ([previous] if previous else [])
        if any(not candidate or os.path.isabs(candidate) or
               any(ord(char) < 32 or ord(char) == 127 for char in candidate) or
               _is_denied_tool_path(os.path.realpath(os.path.join(root, candidate)))
               for candidate in paths):
            continue
        untracked = x == "?" and y == "?"
        entry = {"path": path[:300],
                 "staged": None if untracked else _CODES.get(x),
                 "unstaged": None if untracked else _CODES.get(y),
                 "untracked": untracked}
        if previous:
            entry["previous_path"] = previous[:300]
        files.append(entry)
        if len(files) >= _STATUS_FILES:
            truncated = index < len(records) or truncated
            break
    output = "\n".join(
        f"untracked: {entry['path']}" if entry["untracked"] else
        f"{entry['staged'] or '-'} / {entry['unstaged'] or '-'}: {entry['path']}"
        for entry in files
    )[:6000] or "(clean)"
    if truncated:
        output += "\n[Status truncated; narrow the repository or use a file-level inspection.]"
    return {"repository": root, "head": head, "files": files,
            "truncated": truncated, "output": output, "exit_code": 0}


class GitStatusTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_search_root
        from src import host_execution

        if host_execution.enabled_for(ctx.get("owner")):
            return {"error": "git_status: unavailable on this host route",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content) if content.strip() else {}
            if not isinstance(args, dict) or set(args) - {"path"} or not isinstance(args.get("path", ""), str):
                raise ValueError("invalid arguments")
            root = _resolve_search_root(args.get("path", ""))
            return await asyncio.to_thread(_status, root)
        except (json.JSONDecodeError, OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
            return {"error": f"git_status: {exc}", "exit_code": 1}


class GitDiffTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_search_root
        from src import host_execution

        if host_execution.enabled_for(ctx.get("owner")):
            return {"error": "git_diff: unavailable on this host route",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content)
            if (not isinstance(args, dict) or set(args) - {"path", "file", "staged"}
                    or not isinstance(args.get("path", ""), str)
                    or not isinstance(args.get("file"), str)
                    or type(args.get("staged", False)) is not bool):
                raise ValueError("invalid arguments")
            root = _resolve_search_root(args.get("path", ""))
            return await asyncio.to_thread(_diff, root, args["file"], args.get("staged", False))
        except (json.JSONDecodeError, OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
            return {"error": f"git_diff: {exc}", "exit_code": 1}


class GitLogTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_search_root
        from src import host_execution

        if host_execution.enabled_for(ctx.get("owner")):
            return {"error": "git_log: unavailable on this host route",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content) if content.strip() else {}
            if (not isinstance(args, dict) or set(args) - {"path", "file", "limit"}
                    or not isinstance(args.get("path", ""), str)
                    or ("file" in args and not isinstance(args["file"], str))
                    or type(args.get("limit", 10)) is not int
                    or not 1 <= args.get("limit", 10) <= 20):
                raise ValueError("invalid arguments")
            root = _resolve_search_root(args.get("path", ""))
            return await asyncio.to_thread(_log, root, args.get("limit", 10), args.get("file"))
        except (json.JSONDecodeError, OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
            return {"error": f"git_log: {exc}", "exit_code": 1}
