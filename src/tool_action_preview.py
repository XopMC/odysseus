"""Bounded, display-only summaries for exact tool-approval cards."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MAX_PREVIEW_CHARS = 8_000
MAX_PREVIEW_ITEMS = 20
MAX_ACTION_ARGS_CHARS = 2_000_000
MAX_WRITE_FILE_BYTES = 2 * 1024 * 1024


def _args(tool_name: str, content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        return {}
    if len(content) > MAX_ACTION_ARGS_CHARS:
        return {"_preview_limited": True}
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    if tool_name == "write_file":
        path, separator, body = content.partition("\n")
        return {"path": path.strip(), "content": body if separator else ""}
    if tool_name in {"bash", "python"}:
        return {"command" if tool_name == "bash" else "code": content}
    if tool_name == "web_fetch":
        return {"url": content.splitlines()[0].strip() if content else ""}
    return {}


def _safe_http_target(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return "unavailable: invalid HTTP target"
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        # Never show credentials, query values, fragment tokens, or path
        # segments immediately following conventional secret-bearing labels.
        path_parts = parsed.path.split("/")
        safe_parts = []
        redact_next = False
        secret_labels = {"token", "key", "api_key", "apikey", "secret", "password", "auth", "authorization"}
        for part in path_parts:
            if redact_next:
                safe_parts.append("[redacted]")
                redact_next = False
                continue
            if re.fullmatch(r"eyJ[A-Za-z0-9_-]{12,}(?:\.[A-Za-z0-9_-]+){1,2}", part):
                safe_parts.append("[redacted]")
                continue
            safe_parts.append(part)
            if part.casefold() in secret_labels:
                redact_next = True
        safe_path = "/".join(safe_parts)
        return urlunsplit((parsed.scheme.lower(), host, safe_path, "", ""))[:2048]
    except (TypeError, ValueError):
        return "unavailable: invalid HTTP target"


def _edit_diff(args: dict[str, Any]) -> str:
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        patch = args.get("patch")
        if isinstance(patch, str):
            return patch[:MAX_PREVIEW_CHARS]
        return "Diff unavailable: exact old/new text was not supplied."
    if max(len(old), len(new)) > 128_000:
        return "Diff omitted: requested edit fragment exceeds the preview size limit."
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile="before (requested fragment)",
        tofile="after (requested fragment)", lineterm="",
    ))
    if len(diff) > MAX_PREVIEW_CHARS:
        diff = diff[:MAX_PREVIEW_CHARS] + "\n… preview truncated"
    return diff or "No textual diff."


def write_file_preview_contract(content: Any) -> tuple[dict[str, Any], str | None]:
    """Validate the before-image needed for an honest full-file write preview."""
    args = _args("write_file", content)
    if args.get("_preview_limited") is True:
        return args, "write_file arguments exceed the safe preview limit"
    path, proposed = args.get("path"), args.get("content")
    expected = args.get("expected_sha256")
    if not isinstance(path, str) or not path.strip() or not isinstance(proposed, str):
        return args, "write_file requires JSON path, content and expected_sha256 arguments"
    if len(proposed.encode("utf-8", "replace")) > MAX_WRITE_FILE_BYTES:
        return args, "write_file content exceeds the 2 MiB limit"
    if expected == "missing":
        if args.get("before_content") not in (None, ""):
            return args, "new-file write must not include before_content"
        return args, None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        return args, "write_file requires the current full-file SHA-256, or 'missing' for creation"
    before = args.get("before_content")
    if not isinstance(before, str):
        return args, "overwriting a file requires before_content from the exact read_file result"
    if len(before.encode("utf-8", "replace")) > MAX_WRITE_FILE_BYTES:
        return args, "before_content exceeds the 2 MiB preview limit; use edit_file or apply_patch"
    if hashlib.sha256(before.encode("utf-8", "replace")).hexdigest() != expected.lower():
        return args, "before_content does not match expected_sha256"
    return args, None


def build_tool_action_preview(
    *,
    tool_name: Any,
    content: Any,
    workspace: Any,
    effects: Any,
    action_hash: Any,
    execution_target: Any = None,
    execution_cwd: Any = None,
) -> dict[str, Any]:
    """Return a bounded schema for UI display; it is never execution authority."""
    name = str(tool_name or "tool")[:128]
    parsed = _args(name, content)
    effect_list = sorted({str(effect)[:80] for effect in (effects or ()) if effect})[:MAX_PREVIEW_ITEMS]
    digest = str(action_hash or "")
    common = {
        "tool": name,
        "effect_class": effect_list,
        "action_hash": digest if len(digest) == 64 else "",
        "execution_target": str(execution_target or "Action-defined destination")[:128],
    }
    if parsed.get("_preview_limited") is True:
        return {
            **common,
            "kind": "tool",
            "summary": "Arguments exceed the safe preview parsing limit; inspect the exact sealed arguments below.",
            "preview_limited": True,
        }

    if name in {"bash", "python"}:
        command = parsed.get("command", parsed.get("code", content))
        command = command if isinstance(command, str) else ""
        return {
            **common,
            "kind": "shell",
            "working_directory": str(execution_cwd or workspace or "")[:2048],
            "command": command[:MAX_PREVIEW_CHARS] + ("\n… preview truncated" if len(command) > MAX_PREVIEW_CHARS else ""),
        }

    if name in {"write_file", "edit_file", "apply_patch"}:
        path = parsed.get("path") or parsed.get("file_path") or parsed.get("target")
        path = str(path or "")[:2048]
        preview: dict[str, Any] = {**common, "kind": "file", "path": path}
        if name == "edit_file":
            preview["diff"] = _edit_diff(parsed)
            preview["diff_scope"] = "requested_fragment_not_full_file"
        elif name == "apply_patch":
            patch_text = content if isinstance(content, str) else ""
            preview["diff"] = patch_text[:MAX_PREVIEW_CHARS]
            if len(patch_text) > MAX_PREVIEW_CHARS:
                preview["diff"] += "\n… preview truncated"
            preview["diff_scope"] = "submitted_patch"
        else:
            write_args, contract_error = write_file_preview_contract(content)
            proposed = write_args.get("content", "")
            proposed = proposed if isinstance(proposed, str) else ""
            expected = str(write_args.get("expected_sha256") or "")
            if contract_error:
                preview["diff"] = f"Preview unavailable: {contract_error}."
                preview["diff_scope"] = "unavailable"
            else:
                before = "" if expected == "missing" else write_args["before_content"]
                preview["diff"] = _edit_diff({"old_string": before, "new_string": proposed})
                preview["diff_scope"] = "new_file" if expected == "missing" else "full_file"
            preview["expected_sha256"] = expected
            preview["proposed_bytes"] = len(proposed.encode("utf-8", "replace"))
            preview["proposed_sha256"] = hashlib.sha256(proposed.encode("utf-8", "replace")).hexdigest()
        return preview

    method = str(parsed.get("method") or "").upper()[:16]
    target = parsed.get("url") or parsed.get("target_url") or parsed.get("target")
    if method == "POST" or (isinstance(target, str) and name in {"api_call", "app_api"}):
        body = next((parsed.get(key) for key in ("json", "body", "data", "payload")
                     if parsed.get(key) is not None), None)
        try:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else b""
        except (TypeError, ValueError):
            encoded = b""
        keys = sorted(str(key)[:128] for key in body)[:MAX_PREVIEW_ITEMS] if isinstance(body, dict) else []
        return {
            **common,
            "kind": "http_request",
            "method": method or "UNKNOWN",
            "target": _safe_http_target(target),
            "payload_keys": keys,
            "payload_bytes": len(encoded),
        }

    return {**common, "kind": "tool", "summary": "Review the exact sealed arguments below."}
