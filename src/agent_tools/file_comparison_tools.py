"""Read-only, workspace-confined byte and hash verification tools."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import stat


_HASH_MAX_BYTES = 64 * 1024 * 1024
_TOTAL_VERIFY_BYTES = 128 * 1024 * 1024
_FULL_DIFF_BYTES = 4 * 1024 * 1024
_DIFF_PREFIX_BYTES = 64 * 1024
_DIFF_CHARS = 16_000
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)


def _snapshot(raw_path: str) -> dict:
    from src.tool_execution import _resolve_tool_path

    if (not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in raw_path)):
        raise ValueError("invalid path")
    path = _resolve_tool_path(raw_path)
    fd = os.open(path, _OPEN_FLAGS)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
            raise ValueError("not a regular file")
        if before.st_size > _HASH_MAX_BYTES:
            raise ValueError("file exceeds verification size limit")
        digest = hashlib.sha256()
        prefix = bytearray()
        whole = [] if before.st_size <= _FULL_DIFF_BYTES else None
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if len(prefix) < _DIFF_PREFIX_BYTES:
                prefix.extend(chunk[:_DIFF_PREFIX_BYTES - len(prefix)])
            if whole is not None:
                whole.append(chunk)
        after = os.fstat(stream.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(after):
            raise ValueError("file changed while being verified")
    return {"sha256": digest.hexdigest(), "size_bytes": before.st_size,
            "prefix": bytes(prefix), "whole": b"".join(whole) if whole is not None else None,
            "binary": b"\x00" in prefix}


def _normalized(value: bytes) -> bytes:
    return value.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _compare(before_path: str, after_path: str) -> dict:
    left, right = _snapshot(before_path), _snapshot(after_path)
    identical = (left["size_bytes"] == right["size_bytes"]
                 and left["sha256"] == right["sha256"])
    binary = left["binary"] or right["binary"]
    normalized_equal = None
    if not binary and left["whole"] is not None and right["whole"] is not None:
        normalized_equal = _normalized(left["whole"]) == _normalized(right["whole"])
    diff = ""
    diff_truncated = False
    if not binary and not identical and normalized_equal is not True:
        before_lines = _normalized(left["prefix"]).decode("utf-8", "replace").splitlines(keepends=True)
        after_lines = _normalized(right["prefix"]).decode("utf-8", "replace").splitlines(keepends=True)
        diff_truncated = (left["size_bytes"] > len(left["prefix"])
                          or right["size_bytes"] > len(right["prefix"])
                          or len(before_lines) > 2000 or len(after_lines) > 2000)
        pieces = []
        used = 0
        for line in difflib.unified_diff(before_lines[:2000], after_lines[:2000],
                                         fromfile="before", tofile="after", lineterm="\n"):
            if used + len(line) > _DIFF_CHARS:
                diff_truncated = True
                break
            pieces.append(line)
            used += len(line)
        diff = "".join(pieces)
    if binary and not identical:
        diff_truncated = True
    return {"before_sha256": left["sha256"], "after_sha256": right["sha256"],
            "before_size_bytes": left["size_bytes"], "after_size_bytes": right["size_bytes"],
            "identical": identical, "normalized_equal": normalized_equal,
            "binary": binary, "diff": diff, "diff_truncated": diff_truncated,
            "output": diff or ("Exact bytes match" if identical else
                               "Hashes differ; text diff is empty or unavailable"),
            "exit_code": 0}


def _verify(files: list[dict]) -> dict:
    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise ValueError("invalid file list")
    results = []
    total = 0
    for entry in files:
        if (not isinstance(entry, dict) or set(entry) != {"path", "sha256"}
                or not isinstance(entry["sha256"], str)
                or not _HEX_SHA256.fullmatch(entry["sha256"])):
            raise ValueError("invalid hash assertion")
        actual = _snapshot(entry["path"])
        total += actual["size_bytes"]
        if total > _TOTAL_VERIFY_BYTES:
            raise ValueError("verification size limit exceeded")
        expected = entry["sha256"].lower()
        results.append({"path": entry["path"], "expected_sha256": expected,
                        "actual_sha256": actual["sha256"],
                        "size_bytes": actual["size_bytes"],
                        "matches": actual["sha256"] == expected})
    verified = all(item["matches"] for item in results)
    return {"files": results, "verified": verified, "total_bytes": total,
            "output": "All hashes match" if verified else "Hash assertion failed",
            "code": "ok" if verified else "hash_mismatch", "exit_code": 0 if verified else 1}


class CompareFilesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import host_execution
        if host_execution.enabled_for(ctx.get("owner")):
            return {"error": "File comparison is not available on this host route",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict) or set(args) != {"before", "after"}:
                raise ValueError("invalid arguments")
            return await asyncio.to_thread(_compare, args["before"], args["after"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {"error": "File comparison unavailable or path is not allowed",
                    "code": "invalid_arguments", "exit_code": 1}


class VerifyHashesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import host_execution
        if host_execution.enabled_for(ctx.get("owner")):
            return {"error": "Hash verification is not available on this host route",
                    "code": "not_supported_by_route", "exit_code": 1}
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict) or set(args) != {"files"}:
                raise ValueError("invalid arguments")
            return await asyncio.to_thread(_verify, args["files"])
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {"error": "Hash verification unavailable or path is not allowed",
                    "code": "invalid_arguments", "exit_code": 1}
