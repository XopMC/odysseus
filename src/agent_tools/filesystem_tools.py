import asyncio
import ast
import hashlib
import hmac
import heapq
import json
import os
import re
import stat
import difflib
import shutil
import subprocess
import tempfile
import time
from typing import Optional, Dict, Any, Tuple, List

from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS

_CODENAV_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "site-packages", ".idea", ".tox",
})
_CODENAV_MAX_HITS = 200
_CODENAV_MAX_LINE = 400
_GREP_TIMEOUT_SECONDS = 20
_GREP_STDERR_PREFIX = 20_000


def _git_ignored_paths(root: str, paths: list[str]) -> set[str]:
    """Ask Git's own ignore engine; failures leave the fixed safety skips intact."""
    if not paths or not shutil.which("git"):
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", root, "check-ignore", "-z", "--stdin"],
            input=("\0".join(paths) + "\0").encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode not in (0, 1):
        return set()
    return {item for item in result.stdout.decode("utf-8", "replace").split("\0") if item}


def _glob_to_regex(pat: str) -> "re.Pattern":
    """Translate a forward-slash glob (**, *, ?) into a compiled regex.
    `**/` matches zero or more complete directories.
    `*` matches within a single path segment (does not cross /).
    """
    i, n, out = 0, len(pat), []
    while i < n:
        if pat[i : i + 3] == "**/":
            out.append("(?:[^/]+/)*")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out))


def _python_grep_worker(payload: dict, output_queue) -> None:
    """Spawn-safe fallback grep worker used when ripgrep is unavailable.

    Keep this at module scope: a frozen Windows executable cannot safely be
    relaunched as ``sys.executable -c ...``, while multiprocessing can invoke a
    top-level target through its frozen-process bootstrap.
    """
    try:
        flags = re.IGNORECASE if payload["ignore_case"] else 0
        try:
            regex = re.compile(payload["pattern"], flags)
            glob_regex = (
                _glob_to_regex(payload["glob"].replace("\\", "/"))
                if payload["glob"]
                else None
            )
        except re.error as exc:
            output_queue.put(("error", f"grep: bad pattern: {exc}"))
            return

        requested_root = payload["root"]
        skip_dirs = set(payload["skip_dirs"])
        sensitive = {name.casefold() for name in payload["sensitive_names"]}
        max_hits = payload["max_hits"]
        files_only = bool(payload.get("files_only"))
        hits = 0

        def within(path: str, root: str) -> bool:
            try:
                return os.path.commonpath(
                    [os.path.normcase(path), os.path.normcase(root)]
                ) == os.path.normcase(root)
            except ValueError:
                return False

        def safe_file(path: str, target: str) -> Optional[str]:
            if os.path.islink(path):
                return None
            canonical = os.path.realpath(path)
            if not within(canonical, requested_root) or not within(canonical, target):
                return None
            parts = [part.casefold() for part in canonical.split(os.sep)]
            if any(part in sensitive for part in parts):
                return None
            try:
                if not os.path.isfile(canonical) or os.stat(canonical).st_nlink > 1:
                    return None
            except OSError:
                return None
            return canonical

        for target in payload["targets"]:
            if hits >= max_hits:
                break
            if os.path.isfile(target):
                file_iter = iter((target,))
            else:
                def walk_files():
                    for directory, dirnames, filenames in os.walk(
                        target, followlinks=False
                    ):
                        dirnames[:] = [
                            name
                            for name in dirnames
                            if name not in skip_dirs
                            and name.casefold() not in sensitive
                            and not os.path.islink(os.path.join(directory, name))
                        ]
                        dirnames.sort()
                        for name in sorted(filenames):
                            yield os.path.join(directory, name)

                file_iter = walk_files()

            for candidate in file_iter:
                path = safe_file(candidate, target)
                if path is None:
                    continue
                relative = os.path.relpath(path, requested_root).replace(os.sep, "/")
                if glob_regex and not (
                    glob_regex.fullmatch(relative)
                    or glob_regex.fullmatch(os.path.basename(path))
                ):
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="strict") as handle:
                        for number, line in enumerate(handle, 1):
                            if regex.search(line):
                                output_queue.put((
                                    "match",
                                    path,
                                    number,
                                    line.rstrip()[:_CODENAV_MAX_LINE],
                                ))
                                hits += 1
                                if files_only or hits >= max_hits:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
                if hits >= max_hits:
                    break
        output_queue.put(("done",))
    except BaseException as exc:
        try:
            output_queue.put(("error", f"grep: fallback worker failed: {exc}"))
        except BaseException:
            pass

def _unified_diff(old: str, new: str, path: str) -> Optional[Dict[str, Any]]:
    if old == new:
        return None
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    label = path or "file"
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{label}", tofile=f"b/{label}",
        lineterm="",
    ))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    truncated = False
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES]
        truncated = True
    text = "\n".join(diff_lines)
    if truncated:
        text += f"\n… diff truncated at {MAX_DIFF_LINES} lines"
    return {
        "text": text,
        "added": added,
        "removed": removed,
        "new_file": old == "",
        "file": os.path.basename(path) or (path or "file"),
    }

class _FileMutationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate_expected_sha256(value: Any, raw: bytes, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise _FileMutationError("invalid_arguments", f"{label} must be a 64-character SHA-256")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, value.lower()):
        raise _FileMutationError("stale_revision", "File hash changed; read the current file before editing")


def _syntax_preflight(path: str, text: str, *, enabled: bool = True) -> dict:
    if type(enabled) is not bool:
        raise _FileMutationError("invalid_arguments", "validate_syntax must be boolean")
    if not enabled:
        raise _FileMutationError("validation_required", "syntax validation cannot be disabled")
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".py", ".pyi"}:
        try:
            ast.parse(text, filename=path)
        except SyntaxError as exc:
            raise _FileMutationError(
                "syntax_error", f"Python syntax error at line {exc.lineno}, column {exc.offset}: {exc.msg}"
            ) from None
        return {"status": "passed", "parser": "python_ast"}
    if suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise _FileMutationError(
                "syntax_error", f"JSON syntax error at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from None
        return {"status": "passed", "parser": "json"}
    if suffix in {".js", ".mjs", ".cjs"}:
        node = shutil.which("node")
        if not node:
            raise _FileMutationError("validation_unavailable", "JavaScript syntax validator (node) is unavailable")
        temp_suffix = ".mjs" if suffix == ".js" else suffix
        fd, temporary = tempfile.mkstemp(prefix=".odysseus-syntax-", suffix=temp_suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                result = subprocess.run(
                    [node, "--check", temporary], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=8, check=False,
                )
            except subprocess.TimeoutExpired:
                raise _FileMutationError("validation_timeout", "JavaScript syntax check timed out") from None
            if result.returncode:
                diagnostic = (result.stderr or result.stdout or "Syntax check failed").strip()[:2000]
                raise _FileMutationError("syntax_error", diagnostic)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return {"status": "passed", "parser": "node --check"}
    return {"status": "not_applicable", "extension": suffix or "(none)"}


def _read_mutation_source(path: str) -> tuple[bytes, str, os.stat_result]:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _FileMutationError("invalid_arguments", "Edits require regular, non-hard-linked text files")
        raw = stream.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _FileMutationError("invalid_arguments", "File is not UTF-8 text") from None
    return raw, text, info


def _assert_same_file(path: str, previous: os.stat_result) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        raise _FileMutationError("stale_revision", "File changed during edit; read it again") from None
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or (
        current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns
    ) != (
        previous.st_dev, previous.st_ino, previous.st_size, previous.st_mtime_ns, previous.st_ctime_ns
    ):
        raise _FileMutationError("stale_revision", "File changed during edit; read it again")


class EditFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        raw_path = (args.get("path") or "").strip()
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        expected_sha256 = args.get("expected_sha256")
        validate_syntax = args.get("validate_syntax", True)
        if not raw_path:
            return {"error": "edit_file: path required", "exit_code": 1}
        if expected_sha256 is None:
            return {"error": "edit_file: read_file first and pass its full-file SHA-256 as expected_sha256",
                    "code": "precondition_required", "exit_code": 1}
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"edit_file: {e}", "exit_code": 1}
        if old == "":
            return {"error": "edit_file: old_string required (use write_file to create a file)", "exit_code": 1}
        if old == new:
            return {"error": "edit_file: old_string and new_string are identical", "exit_code": 1}

        def _apply():
            raw, original, info = _read_mutation_source(path)
            _validate_expected_sha256(expected_sha256, raw, label="expected_sha256")
            count = original.count(old)
            if count == 0:
                return original, None, "not_found", None
            if count > 1 and not replace_all:
                return original, None, f"not_unique:{count}", None
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            syntax = _syntax_preflight(path, updated, enabled=validate_syntax)
            _assert_same_file(path, info)
            from core.atomic_io import atomic_write_text
            atomic_write_text(path, updated, preserve_mode=True)
            return original, updated, "ok", syntax

        try:
            original, updated, status, syntax = await asyncio.to_thread(_apply)
        except FileNotFoundError:
            return {"error": f"edit_file: {path}: not found (use write_file to create it)", "exit_code": 1}
        except (IsADirectoryError, UnicodeDecodeError):
            return {"error": f"edit_file: {path}: not an editable text file", "exit_code": 1}
        except _FileMutationError as e:
            return {"error": f"edit_file: {e}", "code": e.code, "exit_code": 1}
        except PermissionError:
            return {"error": f"edit_file: {path}: permission denied", "exit_code": 1}
        except OSError as e:
            return {"error": f"edit_file: {path}: {e}", "exit_code": 1}

        if status == "not_found":
            return {"error": f"edit_file: old_string not found in {path}. Read the file and match it exactly.", "exit_code": 1}
        if status.startswith("not_unique"):
            n = status.split(":", 1)[1]
            return {"error": f"edit_file: old_string is not unique in {path} ({n} matches). Add surrounding context or set replace_all=true.", "exit_code": 1}

        n = original.count(old)
        result = {"output": f"Edited {path} ({n} replacement{'s' if n != 1 else ''})", "exit_code": 0}
        result["syntax_check"] = syntax
        result["hash_precondition"] = "matched" if expected_sha256 is not None else "not_supplied"
        diff = _unified_diff(original, updated, path)
        if diff:
            result["diff"] = diff
        return result

class ReadFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path
        raw_path, offset, limit = content.split("\n", 1)[0].strip(), 0, 0
        byte_offset, byte_limit, line_numbers = None, None, False
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                raw_path = str(_a.get("path", "")).strip()
                offset = int(_a.get("offset") or 0)
                limit = int(_a.get("limit") or 0)
                byte_offset = _a.get("byte_offset")
                byte_limit = _a.get("byte_limit")
                line_numbers = _a.get("line_numbers", False)
            except (json.JSONDecodeError, TypeError, ValueError):
                return {"error": "read_file: invalid arguments", "exit_code": 1}
        if (offset < 0 or limit < 0 or type(line_numbers) is not bool
                or (byte_offset is not None and (type(byte_offset) is not int or byte_offset < 0))
                or (byte_limit is not None and (type(byte_limit) is not int or byte_limit < 1))
                or ((byte_offset is not None or byte_limit is not None) and (offset or limit or line_numbers))):
            return {"error": "read_file: invalid or conflicting range arguments", "exit_code": 1}
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"read_file: {e}", "exit_code": 1}
        try:
            def _read():
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("only regular files can be read")
                    digest = hashlib.sha256()
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ValueError("file changed during read; retry")
                    stream.seek(0)
                    head = stream.read(8192)
                    stream.seek(0)
                    binary = b"\x00" in head
                    if byte_offset is not None or byte_limit is not None:
                        start = min(byte_offset or 0, info.st_size)
                        end = min(info.st_size, start + min(byte_limit or MAX_READ_CHARS, MAX_READ_CHARS))
                        stream.seek(start)
                        data = stream.read(max(0, end - start))
                        byte_range = [start, end]
                        truncated = end < info.st_size
                        budget_truncated = (byte_limit or MAX_READ_CHARS) > MAX_READ_CHARS and truncated
                    elif offset or limit or line_numbers:
                        start, out, size = max(offset, 1), [], 0
                        for i, line in enumerate(stream, 1):
                            if i < start:
                                continue
                            if limit and i >= start + limit:
                                break
                            line = (f"{i}: ".encode() + line) if line_numbers else line
                            out.append(line)
                            size += len(line)
                            if size > MAX_READ_CHARS:
                                break
                        data = b"".join(out)[:MAX_READ_CHARS]
                        byte_range = None
                        truncated = size > MAX_READ_CHARS
                        budget_truncated = truncated
                    else:
                        data = stream.read(MAX_READ_CHARS + 1)
                        byte_range = None
                        truncated = len(data) > MAX_READ_CHARS
                        budget_truncated = truncated
                        data = data[:MAX_READ_CHARS]
                    binary = binary or b"\x00" in data
                    try:
                        output = data.decode("utf-8")
                    except UnicodeDecodeError:
                        binary = True
                        output = ""
                    if binary:
                        output = f"[Binary file: {info.st_size} bytes; content omitted]"
                    elif budget_truncated:
                        output += f"\n... [truncated at {MAX_READ_CHARS} bytes]"
                    result = {"output": output, "exit_code": 0, "sha256": digest.hexdigest(),
                              "size_bytes": info.st_size, "encoding": "binary" if binary else "utf-8",
                              "is_binary": binary, "truncated": truncated}
                    # Keep the prompt bounded while allowing exact, owner-scoped
                    # recall of a larger text file.  A failed/quota-limited archive
                    # never changes the bytes returned by read_file.
                    if truncated and not binary and ctx.get("session_id"):
                        from src.observation_pack import archive
                        from src.settings import get_setting
                        artifact_limit = max(1024, int(get_setting(
                            "observation_pack_object_max_bytes", 16_777_216) or 16_777_216))
                        if info.st_size <= artifact_limit:
                            stream.seek(0)
                            try:
                                complete_bytes = stream.read()
                                archive_info = os.fstat(stream.fileno())
                                if (len(complete_bytes) != info.st_size
                                        or hashlib.sha256(complete_bytes).hexdigest() != digest.hexdigest()
                                        or (archive_info.st_dev, archive_info.st_ino, archive_info.st_mtime_ns)
                                        != (info.st_dev, info.st_ino, info.st_mtime_ns)):
                                    raise ValueError("file changed before artifact creation")
                                complete_text = complete_bytes.decode("utf-8")
                                artifact = archive(
                                    ctx.get("owner"), ctx["session_id"], tool_name="read_file",
                                    tool_call_id=f"{path}:{digest.hexdigest()}",
                                    text=complete_text, force=True,
                                    run_id=ctx.get("parent_run_id"))
                                if artifact:
                                    result["artifact_id"] = artifact["id"]
                                    result["output"] = output[:2000] + (
                                        f"\n[Preview limited to 2000 characters. Full file: call read_tool_artifact with "
                                        f"id={artifact['id']} and offset=0]"
                                    )
                            except (OSError, UnicodeDecodeError, ValueError):
                                result["artifact_unavailable"] = True
                        else:
                            result["artifact_unavailable"] = True
                    if byte_range is not None:
                        result["byte_range"] = byte_range
                    return result
            return await asyncio.to_thread(_read)
        except FileNotFoundError:
            return {"error": "read_file: file not found", "code": "not_found", "exit_code": 1}
        except PermissionError:
            return {"error": "read_file: permission denied", "code": "permission_denied", "exit_code": 1}
        except IsADirectoryError:
            return {"error": "read_file: target is a directory (use ls)",
                    "code": "invalid_arguments", "exit_code": 1}
        except ValueError as e:
            code = "stale_revision" if "changed during read" in str(e) else "invalid_arguments"
            return {"error": "read_file: file changed during read" if code == "stale_revision"
                    else "read_file: invalid file or range", "code": code, "exit_code": 1}
        except OSError:
            return {"error": "read_file: file is unavailable", "code": "transport_unavailable", "exit_code": 1}

class WriteFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        lines = content.split("\n", 1)
        raw_path = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        # Decode JSON-object args (the fenced inline-args shape
        # ```write_file {"path": "...", "content": "..."}```), matching
        # ReadFileTool above. Without this the whole JSON string becomes the
        # path and the file is written under a garbage name. This is the live
        # path: there is no filesystem MCP server, so write_file always runs
        # here via _direct_fallback, not through _build_mcp_args.
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                if isinstance(_a, dict) and "path" in _a:
                    raw_path = str(_a.get("path", "")).strip()
                    body = str(_a.get("content", ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"write_file: {e}", "exit_code": 1}
        try:
            def _write():
                old = ""
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        old = f.read()
                except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, OSError):
                    old = ""
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                return old, len(body)
            old_content, size = await asyncio.to_thread(_write)
        except PermissionError:
            return {"error": f"write_file: {path}: permission denied", "exit_code": 1}
        except OSError as e:
            return {"error": f"write_file: {path}: {e}", "exit_code": 1}
        diff = _unified_diff(old_content, body, path)
        result = {"output": f"Wrote {size} bytes to {path}", "exit_code": 0}
        if diff:
            result["diff"] = diff
        return result

class ApplyPatchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        """Apply a small Codex-style patch using exact context matching.

        This is deliberately stricter than git-apply: if an update hunk's old
        text is not found exactly once, the whole patch is rejected before any
        file is changed. That keeps agent edits reviewable and avoids fuzzy
        corruption when the model patches stale context.
        """
        from src.tool_execution import _resolve_tool_path

        patch_text = content or ""
        expected_by_path = {}
        validate_syntax = True
        stripped = patch_text.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                return {"error": "apply_patch: invalid arguments", "code": "invalid_arguments", "exit_code": 1}
            if not isinstance(args, dict):
                return {"error": "apply_patch: invalid arguments", "code": "invalid_arguments", "exit_code": 1}
            patch_text = str(args.get("patch_text") or args.get("patchText") or args.get("patch") or "")
            expected_by_path = args.get("expected_sha256_by_path") or {}
            validate_syntax = args.get("validate_syntax", True)
            allowed = {"patch_text", "patchText", "patch", "verify", "expected_sha256_by_path", "validate_syntax"}
            if set(args) - allowed:
                return {"error": "apply_patch: unsupported arguments", "code": "invalid_arguments", "exit_code": 1}
            if (not isinstance(expected_by_path, dict) or len(expected_by_path) > 32
                    or any(not isinstance(key, str) for key in expected_by_path)
                    or any(not isinstance(value, str) or (
                        value != "missing" and not re.fullmatch(r"[a-fA-F0-9]{64}", value)
                    ) for value in expected_by_path.values())):
                return {"error": "apply_patch: invalid expected_sha256_by_path", "code": "invalid_arguments", "exit_code": 1}
            if type(validate_syntax) is not bool:
                return {"error": "apply_patch: validate_syntax must be boolean", "code": "invalid_arguments", "exit_code": 1}
        if not patch_text.strip():
            return {"error": "apply_patch: patch_text required", "exit_code": 1}

        try:
            ops = _parse_agent_patch(patch_text)
            if not ops:
                return {"error": "apply_patch: no file operations found", "exit_code": 1}
            prepared = []
            consumed_hashes = set()
            for op in ops:
                path = _resolve_tool_path(op["path"])
                kind = op["kind"]
                expected = expected_by_path.get(op["path"], expected_by_path.get(path))
                has_expected = op["path"] in expected_by_path or path in expected_by_path
                if not has_expected:
                    raise _FileMutationError(
                        "precondition_required",
                        f"Read each patch target and include its SHA-256 in expected_sha256_by_path: {op['path']}",
                    )
                if op["path"] in expected_by_path:
                    consumed_hashes.add(op["path"])
                elif path in expected_by_path:
                    consumed_hashes.add(path)
                if kind == "add":
                    if os.path.exists(path):
                        return {"error": f"apply_patch: {op['path']}: already exists", "exit_code": 1}
                    if has_expected and expected != "missing":
                        return {"error": f"apply_patch: {op['path']}: expected missing-file precondition",
                                "code": "stale_revision", "exit_code": 1}
                    old = ""
                    new = op["content"]
                    info = None
                elif kind == "delete":
                    try:
                        raw, old, info = _read_mutation_source(path)
                    except FileNotFoundError:
                        return {"error": f"apply_patch: {op['path']}: not found", "code": "not_found", "exit_code": 1}
                    if has_expected:
                        _validate_expected_sha256(expected, raw, label=f"expected_sha256_by_path[{op['path']}]")
                    new = ""
                else:
                    try:
                        raw, old, info = _read_mutation_source(path)
                    except FileNotFoundError:
                        return {"error": f"apply_patch: {op['path']}: not found", "code": "not_found", "exit_code": 1}
                    if has_expected:
                        _validate_expected_sha256(expected, raw, label=f"expected_sha256_by_path[{op['path']}]")
                    new = _apply_patch_hunks(old, op["hunks"], op["path"])
                syntax = None if kind == "delete" else _syntax_preflight(path, new, enabled=validate_syntax)
                prepared.append((kind, path, old, new, info, syntax))
            if consumed_hashes != set(expected_by_path):
                raise _FileMutationError("invalid_arguments", "Hash precondition path is not part of this patch")

            diffs = []
            changed = []
            from core.atomic_io import atomic_write_text
            try:
                for kind, path, old, new, info, _syntax in prepared:
                    if info is not None:
                        _assert_same_file(path, info)
                    if kind == "delete":
                        os.unlink(path)
                    else:
                        atomic_write_text(path, new, preserve_mode=info is not None,
                                          exclusive=info is None)
                    changed.append((kind, path, old, new, info))
                    diff = _unified_diff(old, new, path)
                    if diff:
                        diffs.append(diff)
            except Exception as commit_error:
                rollback_errors = []
                for kind, path, old, new, info in reversed(changed):
                    try:
                        if kind == "add":
                            raw_current, _text, _current_info = _read_mutation_source(path)
                            if not hmac.compare_digest(hashlib.sha256(raw_current).hexdigest(),
                                                       hashlib.sha256(new.encode("utf-8")).hexdigest()):
                                raise _FileMutationError("rollback_conflict", "new file changed during rollback")
                            os.unlink(path)
                        elif kind == "delete":
                            if os.path.exists(path):
                                raise _FileMutationError("rollback_conflict", "deleted file path was recreated")
                            atomic_write_text(path, old, exclusive=True,
                                              preserve_metadata_from=info)
                        else:
                            raw_current, _text, _current_info = _read_mutation_source(path)
                            if not hmac.compare_digest(hashlib.sha256(raw_current).hexdigest(),
                                                       hashlib.sha256(new.encode("utf-8")).hexdigest()):
                                raise _FileMutationError("rollback_conflict", "updated file changed during rollback")
                            atomic_write_text(path, old, preserve_mode=True)
                    except Exception as rollback_error:
                        rollback_errors.append(f"{path}: {rollback_error}")
                if rollback_errors:
                    raise _FileMutationError(
                        "patch_rollback_failed",
                        f"patch commit failed; rollback needs inspection: {'; '.join(rollback_errors)[:1500]}",
                    ) from commit_error
                raise _FileMutationError("patch_commit_failed", "patch commit failed; all earlier file changes were rolled back") from commit_error
        except _FileMutationError as exc:
            return {"error": f"apply_patch: {exc}", "code": exc.code, "exit_code": 1}
        except (ValueError, UnicodeDecodeError, PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}

        added = sum(int(d.get("added") or 0) for d in diffs)
        removed = sum(int(d.get("removed") or 0) for d in diffs)
        text_parts = [d.get("text", "") for d in diffs if d.get("text")]
        diff_text = "\n".join(text_parts)
        if len(diff_text.splitlines()) > MAX_DIFF_LINES:
            diff_text = "\n".join(diff_text.splitlines()[:MAX_DIFF_LINES]) + f"\n... diff truncated at {MAX_DIFF_LINES} lines"
        result = {
            "output": f"Applied patch ({len(prepared)} file{'s' if len(prepared) != 1 else ''}, +{added}/-{removed})",
            "exit_code": 0,
            "syntax_checks": [
                {"path": path, **syntax} for _kind, path, _old, _new, _info, syntax in prepared
                if syntax is not None
            ],
            "hash_preconditions": "checked",
        }
        if diffs:
            result["diff"] = {
                "text": diff_text,
                "added": added,
                "removed": removed,
                "new_file": any(d.get("new_file") for d in diffs),
                "file": "patch",
            }
        return result

def _parse_agent_patch(patch_text: str) -> List[Dict[str, Any]]:
    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")

    ops: List[Dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            body = []
            i += 1
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ValueError(f"add file {path}: every content line must start with +")
                body.append(lines[i][1:])
                i += 1
            ops.append({"kind": "add", "path": path, "content": "\n".join(body) + ("\n" if body else "")})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            ops.append({"kind": "delete", "path": path})
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            hunks = []
            current = []
            i += 1
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                raise ValueError("move operations are not supported")
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                elif lines[i].startswith((" ", "-", "+")):
                    current.append(lines[i])
                elif lines[i] == "":
                    current.append(" ")
                else:
                    raise ValueError(f"update file {path}: invalid patch line {lines[i]!r}")
                i += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError(f"update file {path}: no hunks")
            ops.append({"kind": "update", "path": path, "hunks": hunks})
            continue
        raise ValueError(f"unexpected patch line: {line!r}")
    return ops

def _apply_patch_hunks(original: str, hunks: List[List[str]], label: str) -> str:
    updated = original
    for idx, hunk in enumerate(hunks, 1):
        old_lines = []
        new_lines = []
        for line in hunk:
            prefix, body = line[:1], line[1:]
            if prefix in (" ", "-"):
                old_lines.append(body)
            if prefix in (" ", "+"):
                new_lines.append(body)
        old_text = "\n".join(old_lines)
        new_text = "\n".join(new_lines)
        if old_text and old_text in updated:
            occurrences = updated.count(old_text)
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text, new_text, 1)
        elif old_text + "\n" in updated:
            occurrences = updated.count(old_text + "\n")
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text + "\n", new_text + "\n", 1)
        else:
            raise ValueError(f"{label}: hunk {idx} context not found")
    return updated

class LsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _is_denied_tool_path,
            _resolve_search_root,
            _truncate,
        )
        raw_path = ""
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                raw_path = str(json.loads(_s).get("path", "")).strip()
            except json.JSONDecodeError:
                raw_path = ""
        else:
            raw_path = _s.split("\n", 1)[0].strip()
        try:
            root = _resolve_search_root(raw_path)
        except ValueError as e:
            return {"error": f"ls: {e}", "exit_code": 1}

        def _ls():
            if not os.path.isdir(root):
                return None, f"ls: {root}: not a directory"
            rows = []
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        if _is_denied_tool_path(os.path.realpath(entry.path)):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            size = entry.stat(follow_symlinks=False).st_size if not is_dir else 0
                        except OSError:
                            continue
                        rows.append((is_dir, entry.name, size))
            except (PermissionError, OSError) as _e:
                return None, f"ls: {_e}"
            rows.sort(key=lambda r: (not r[0], r[1].lower()))
            lines = [f"{root}:"]
            for is_dir, name, size in rows[:_CODENAV_MAX_HITS]:
                lines.append(f"  {name}/" if is_dir else f"  {name}  ({size} B)")
            if len(rows) > _CODENAV_MAX_HITS:
                lines.append(f"  ... [{len(rows) - _CODENAV_MAX_HITS} more]")
            if not rows:
                lines.append("  (empty)")
            return "\n".join(lines), None

        out, err = await asyncio.to_thread(_ls)
        if err:
            return {"error": err, "exit_code": 1}
        return {"output": _truncate(out), "exit_code": 0}


class ListTreeTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _can_traverse_tool_path, _is_denied_tool_path,
            _resolve_search_root, _truncate,
        )
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {"path": content or ""}
            if not isinstance(args, dict):
                raise ValueError
            depth = args.get("max_depth", 2)
            limit = args.get("max_entries", 100)
            if type(depth) is not int or not 1 <= depth <= 6 or type(limit) is not int or not 1 <= limit <= 200:
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"error": "list_tree: invalid arguments", "exit_code": 1}
        try:
            root = _resolve_search_root(str(args.get("path") or ""))
        except ValueError as exc:
            return {"error": f"list_tree: {exc}", "exit_code": 1}

        def _walk():
            if not os.path.isdir(root):
                return None, False, "list_tree: path is not a directory"
            rows, pending, truncated = [], [(root, 0)], False
            while pending and len(rows) < limit:
                current, level = pending.pop()
                if level >= depth:
                    continue
                if not _can_traverse_tool_path(os.path.realpath(current)):
                    continue
                try:
                    with os.scandir(current) as children:
                        candidates = []
                        for index, entry in enumerate(children):
                            if index >= 10_000:
                                truncated = True
                                break
                            if (entry.name.startswith(".") or entry.name in _CODENAV_SKIP_DIRS
                                    or entry.is_symlink() or _is_denied_tool_path(os.path.realpath(entry.path))):
                                continue
                            candidates.append(entry)
                        ignored = _git_ignored_paths(root, [entry.path for entry in candidates])
                        ordered = heapq.nsmallest(limit + 1,
                                                  (entry for entry in candidates if entry.path not in ignored),
                                                  key=lambda entry: entry.name.casefold())
                except OSError:
                    return None, False, "list_tree: unable to enumerate requested directory"
                if len(ordered) > limit:
                    truncated = True
                descend = []
                for entry in ordered[:limit]:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if not is_dir and not stat.S_ISREG(info.st_mode):
                        continue
                    relative = os.path.relpath(entry.path, root).replace(os.sep, "/")
                    rows.append({"path": relative, "kind": "directory" if is_dir else "file",
                                 "size_bytes": 0 if is_dir else info.st_size})
                    if is_dir:
                        descend.append((entry.path, level + 1))
                    if len(rows) >= limit:
                        truncated = True
                        break
                pending.extend(reversed(descend))
            if pending:
                truncated = True
            return rows, truncated, None

        rows, truncated, error = await asyncio.to_thread(_walk)
        if error:
            return {"error": error, "exit_code": 1}
        lines = [f"{row['path']}/" if row["kind"] == "directory"
                 else f"{row['path']} ({row['size_bytes']} B)" for row in rows]
        return {"output": _truncate("\n".join(lines) or "(empty)"),
                "entries": rows, "truncated": truncated, "exit_code": 0}


class FileOutlineTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, _truncate
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {"path": content or ""}
            if not isinstance(args, dict):
                raise ValueError
            maximum = args.get("max_symbols", 100)
            if type(maximum) is not int or not 1 <= maximum <= 200:
                raise ValueError
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"error": "file_outline: invalid arguments", "exit_code": 1}
        try:
            path = _resolve_tool_path(str(args.get("path") or ""))
        except ValueError as exc:
            return {"error": f"file_outline: {exc}", "exit_code": 1}
        if not path.endswith((".py", ".pyi")):
            return {"error": "file_outline: unavailable for this file type",
                    "code": "unsupported_language", "exit_code": 1}

        def _outline():
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                    raise ValueError("regular Python file of at most 2 MiB required")
                source = stream.read(2 * 1024 * 1024 + 1).decode("utf-8")
            tree = ast.parse(source, filename=path)
            symbols = []
            def visit(body, prefix=""):
                for node in body:
                    if isinstance(node, ast.ClassDef):
                        name = prefix + node.name
                        symbols.append({"kind": "class", "name": name, "line": node.lineno,
                                        "end_line": node.end_lineno})
                        visit(node.body, name + ".")
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append({"kind": "method" if prefix else (
                            "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"),
                            "name": prefix + node.name, "line": node.lineno,
                            "end_line": node.end_lineno})
            visit(tree.body)
            return symbols

        try:
            symbols = await asyncio.to_thread(_outline)
        except (OSError, UnicodeError, ValueError, SyntaxError):
            return {"error": "file_outline: unable to parse requested Python file", "exit_code": 1}
        selected = symbols[:maximum]
        output = "\n".join(f"{item['line']}: {item['kind']} {item['name']}" for item in selected)
        return {"output": _truncate(output or "(no symbols)"), "symbols": selected,
                "truncated": len(symbols) > maximum, "parser": "python_ast", "exit_code": 0}

class GlobTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _SENSITIVE_BASENAMES,
            _can_traverse_tool_path,
            _is_denied_tool_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "glob: pattern is required", "exit_code": 1}
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"glob: {e}", "exit_code": 1}

        def _glob():
            base = os.path.abspath(root)
            if not os.path.isdir(base):
                return None, f"glob: {root}: not a directory"
            rbase = os.path.realpath(base)
            norm_pat = pattern.replace("\\", "/")
            # Fast path: literal pattern (no wildcards) → direct path lookup.
            if not any(c in norm_pat for c in "*?["):
                cand = os.path.realpath(os.path.join(base, norm_pat))
                # Keep the literal lookup inside the search root. os.path.join
                # lets an absolute pattern (or one containing ../) escape `base`,
                # which would turn glob into an existence/path oracle for
                # arbitrary host files — bypassing the workspace/allowlist
                # confinement that _resolve_search_root applies to the root.
                # An escaping literal falls through to the walk, which only ever
                # yields paths under base.
                nbase = os.path.normcase(rbase)
                try:
                    inside = cand == rbase or os.path.commonpath(
                        [os.path.normcase(cand), nbase]
                    ) == nbase
                except ValueError:
                    inside = False
                # A literal that names a deny-listed sensitive file (.env,
                # .ssh/id_rsa, …) falls through to the walk, which skips it —
                # otherwise glob would surface secret paths that read_file /
                # grep already refuse to touch.
                if inside and os.path.exists(cand) and not _is_denied_tool_path(cand):
                    return [cand], None
                # Literal not at exact path — fall through to walk so
                # e.g. "foo.py" still matches at any depth (like rglob).
            # Compile glob to regex: * stays within one segment, **/ spans dirs.
            regex = _glob_to_regex(norm_pat)
            matched = []
            cap = _CODENAV_MAX_HITS * 5
            try:
                for dp, dns, fns in os.walk(base):
                    if not _can_traverse_tool_path(os.path.realpath(dp)):
                        dns[:] = []
                        continue
                    # Prune skipped dirs before descending (unlike rglob which
                    # descends first then filters — fatal on large node_modules).
                    # Sensitive dirs (.ssh, .gnupg, …) are pruned too so glob
                    # never enumerates the keys/tokens inside them.
                    dns[:] = [
                        d for d in dns
                        if d not in _CODENAV_SKIP_DIRS
                        and d not in _SENSITIVE_BASENAMES
                        and _can_traverse_tool_path(os.path.realpath(os.path.join(dp, d)))
                    ]
                    for name in fns + dns:
                        full = os.path.join(dp, name)
                        rel = os.path.relpath(full, base).replace(os.sep, "/")
                        if regex.fullmatch(rel) or regex.fullmatch(name):
                            # Skip deny-listed sensitive files (.env, id_rsa,
                            # known_hosts, …) the same way grep does.
                            if _is_denied_tool_path(os.path.realpath(full)):
                                continue
                            try:
                                mtime = os.stat(full).st_mtime
                            except OSError:
                                mtime = 0
                            matched.append((mtime, full))
                    if len(matched) > cap:
                        break
            except OSError:
                # Do not echo the model-supplied pattern or OS path. Besides
                # leaking host layout, an escaping glob can turn this error
                # into a path-existence oracle.
                return None, "glob: unable to enumerate requested files"
            matched.sort(key=lambda t: t[0], reverse=True)
            return [pth for _, pth in matched[:_CODENAV_MAX_HITS]], None

        paths, err = await asyncio.to_thread(_glob)
        if err:
            return {"error": err, "exit_code": 1}
        if not paths:
            # Patterns may contain absolute paths or sensitive fragments.
            # Keep the stable user-facing prefix for compatibility, but never
            # reflect attacker-controlled path text back into the transcript.
            return {"output": "No files matching the requested pattern", "exit_code": 0}
        out = "\n".join(paths)
        if len(paths) >= _CODENAV_MAX_HITS:
            out += f"\n... [capped at {_CODENAV_MAX_HITS} files]"
        return {"output": _truncate(out), "exit_code": 0}

class GrepTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import (
            _SENSITIVE_BASENAMES,
            _SENSITIVE_FILE_PATTERNS,
            _agent_readable_data_subdirs,
            _is_denied_tool_path,
            _is_sensitive_path,
            _path_within,
            _resolve_search_root,
            _truncate,
        )
        args: Dict[str, Any] = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "grep: pattern is required", "exit_code": 1}
        ignore_case = bool(args.get("ignore_case"))
        glob_pat = str(args.get("glob", "") or "").strip()
        search_v2 = bool(args.get("_search_v2"))
        files_only = search_v2 and args.get("mode", "files") == "files"
        if search_v2 and args.get("mode", "files") not in ("files", "matches"):
            return {"error": "search_files: mode must be files or matches", "exit_code": 1}
        if search_v2:
            cursor, page_size = args.get("cursor", 0), args.get("page_size", 25)
            if (type(cursor) is not int or cursor < 0 or cursor >= 1000
                    or type(page_size) is not int or not 1 <= page_size <= 50):
                return {"error": "search_files: invalid cursor or page_size", "exit_code": 1}
            max_hits = min(1000, cursor + page_size + 1)
        else:
            try:
                max_hits = int(args.get("max_results") or _CODENAV_MAX_HITS)
            except (TypeError, ValueError):
                max_hits = _CODENAV_MAX_HITS
            max_hits = max(1, min(max_hits, _CODENAV_MAX_HITS))
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"grep: {e}", "exit_code": 1}

        def _grep():
            import multiprocessing
            import queue
            import subprocess
            import threading

            from src.constants import DATA_DIR

            rg = shutil.which("rg")
            real_root = os.path.realpath(root)
            data_dir = os.path.realpath(DATA_DIR)
            spans_state = _path_within(data_dir, real_root)

            def is_top_level_safe(path: str, *, partition_generated: bool) -> bool:
                lexical = os.path.abspath(path)
                if os.path.islink(lexical):
                    return False
                canonical = os.path.realpath(lexical)
                if not _path_within(canonical, real_root):
                    return False
                if partition_generated and os.path.basename(lexical) in _CODENAV_SKIP_DIRS:
                    return False
                if _is_sensitive_path(canonical) or _is_denied_tool_path(canonical):
                    return False
                return True

            def safe_targets() -> tuple[list[str], Optional[str]]:
                candidates: list[tuple[str, bool]] = []
                if not spans_state:
                    # Preserve direct-root compatibility: skip-directory policy
                    # prunes descendants, but an explicitly requested allowed
                    # root named node_modules remains searchable.
                    candidates.append((real_root, False))
                else:
                    current = real_root
                    if current != data_dir:
                        for part in os.path.relpath(data_dir, current).split(os.sep):
                            try:
                                with os.scandir(current) as entries:
                                    for entry in entries:
                                        if entry.name != part:
                                            # Reject a sibling link lexically before
                                            # canonicalizing or treating it as a target.
                                            if entry.is_symlink():
                                                continue
                                            candidates.append((entry.path, True))
                            except OSError as exc:
                                return [], f"grep: {exc}"
                            current = os.path.join(current, part)
                    for readable in _agent_readable_data_subdirs():
                        if (
                            _path_within(readable, data_dir)
                            and _path_within(readable, real_root)
                            and os.path.exists(readable)
                        ):
                            candidates.append((readable, True))

                targets: list[str] = []
                seen: set[str] = set()
                for candidate, partition_generated in candidates:
                    if not is_top_level_safe(
                        candidate, partition_generated=partition_generated
                    ):
                        continue
                    canonical = os.path.realpath(candidate)
                    if canonical not in seen:
                        seen.add(canonical)
                        targets.append(canonical)
                return targets, None

            targets, target_error = safe_targets()
            if target_error:
                return None, target_error

            base = real_root if os.path.isdir(real_root) else os.path.dirname(real_root)
            deadline = time.monotonic() + _GREP_TIMEOUT_SECONDS
            lines: list[str] = []

            def parse_rg_result(raw: str) -> Optional[str]:
                try:
                    record = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    return None
                if record.get("type") != "match":
                    return None
                data = record.get("data") or {}
                path = (data.get("path") or {}).get("text")
                text_value = (data.get("lines") or {}).get("text")
                number = data.get("line_number")
                if not isinstance(path, str) or not isinstance(text_value, str):
                    return None
                absolute = path if os.path.isabs(path) else os.path.join(base, path)
                canonical = os.path.realpath(absolute)
                if not _path_within(canonical, real_root) or _is_denied_tool_path(canonical):
                    return None
                if files_only:
                    return os.path.abspath(absolute)
                return f"{os.path.abspath(absolute)}:{number}:{text_value.rstrip()[:_CODENAV_MAX_LINE]}"

            def run_rg(cmd: list[str]) -> Optional[str]:
                try:
                    process = subprocess.Popen(
                        cmd,
                        cwd=base,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except Exception as exc:
                    return f"grep: {exc}"
                output: queue.Queue[Optional[str]] = queue.Queue(maxsize=max_hits + 2)
                stderr_prefix: list[str] = []
                stderr_size = 0
                stop_reader = threading.Event()

                def enqueue_stdout(value: Optional[str]) -> bool:
                    # The consumer stops at the result cap or deadline. Never
                    # leave a producer blocked on its bounded queue afterward.
                    while not stop_reader.is_set():
                        try:
                            output.put(value, timeout=0.05)
                            return True
                        except queue.Full:
                            continue
                    return False

                def read_stdout() -> None:
                    assert process.stdout is not None
                    try:
                        for line in process.stdout:
                            if not enqueue_stdout(line.rstrip("\n")):
                                break
                    finally:
                        enqueue_stdout(None)

                def read_stderr() -> None:
                    nonlocal stderr_size
                    assert process.stderr is not None
                    while True:
                        chunk = process.stderr.read(4096)
                        if not chunk:
                            break
                        if stderr_size < _GREP_STDERR_PREFIX:
                            kept = chunk[:_GREP_STDERR_PREFIX - stderr_size]
                            stderr_prefix.append(kept)
                            stderr_size += len(kept)

                stdout_thread = threading.Thread(target=read_stdout, daemon=True)
                stderr_thread = threading.Thread(target=read_stderr, daemon=True)
                stdout_thread.start()
                stderr_thread.start()
                timed_out = False
                capped = False
                try:
                    while len(lines) < max_hits:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        try:
                            raw = output.get(timeout=remaining)
                        except queue.Empty:
                            timed_out = True
                            break
                        if raw is None:
                            break
                        parsed = parse_rg_result(raw)
                        if parsed and parsed not in lines:
                            lines.append(parsed)
                    capped = len(lines) >= max_hits
                finally:
                    stop_reader.set()
                    if (timed_out or capped) and process.poll() is None:
                        process.terminate()
                    try:
                        remaining = max(0.01, deadline - time.monotonic())
                        return_code = process.wait(timeout=min(1, remaining))
                    except subprocess.TimeoutExpired:
                        process.kill()
                        return_code = process.wait()
                    stdout_thread.join()
                    stderr_thread.join()
                if timed_out:
                    return "grep: timed out"
                if not capped and return_code not in (0, 1):
                    detail = "".join(stderr_prefix).strip()
                    return f"grep: {detail or f'process exited {return_code}'}"
                return None

            if rg:
                # Validate even when policy filtering leaves no search targets.
                if not targets:
                    error = run_rg([rg, "--json", "--no-config", "--regexp", pattern])
                    return (None, error) if error else ([], None)
                relative_targets = [os.path.relpath(target, base) for target in targets]
                for offset in range(0, len(relative_targets), 128):
                    if len(lines) >= max_hits:
                        break
                    cmd = [
                        rg, "--json", "--no-config", "--no-follow",
                        "--sort", "path",
                        "--max-count", str(1 if files_only else max_hits - len(lines)),
                        "--max-columns", str(_CODENAV_MAX_LINE),
                        "--max-columns-preview",
                    ]
                    if ignore_case:
                        cmd.append("--ignore-case")
                    if glob_pat:
                        cmd += ["--glob", glob_pat]
                    for sensitive_pattern in _SENSITIVE_FILE_PATTERNS:
                        cmd += ["--iglob", f"!{sensitive_pattern}"]
                    for skipped_dir in _CODENAV_SKIP_DIRS:
                        cmd += ["--glob", f"!**/{skipped_dir}/**"]
                    cmd += ["--regexp", pattern, "--", *relative_targets[offset:offset + 128]]
                    error = run_rg(cmd)
                    if error:
                        return None, error
                return lines, None

            # This runs inside asyncio.to_thread(), so forking would clone a
            # multithreaded process and can deadlock. Spawn is platform-safe and
            # PyInstaller-compatible via launcher's early freeze_support().
            payload = {
                "root": real_root,
                "targets": targets,
                "pattern": pattern,
                "ignore_case": ignore_case,
                "glob": glob_pat,
                "max_hits": max_hits,
                "files_only": files_only,
                "skip_dirs": tuple(_CODENAV_SKIP_DIRS),
                "sensitive_names": tuple(
                    set(_SENSITIVE_BASENAMES) | set(_SENSITIVE_FILE_PATTERNS)
                ),
            }
            try:
                context = multiprocessing.get_context("spawn")
                output_queue = context.Queue(maxsize=max_hits + 2)
                worker = context.Process(
                    target=_python_grep_worker, args=(payload, output_queue)
                )
                worker.start()
            except Exception as exc:
                try:
                    output_queue.close()
                except (NameError, OSError, ValueError):
                    pass
                return None, f"grep: could not start fallback worker: {exc}"
            error = None
            completed = False
            try:
                while len(lines) < max_hits:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = "grep: timed out"
                        break
                    try:
                        # Keep queue waits short enough to observe a spawn
                        # worker that dies during bootstrap/import before it
                        # can enqueue either an error or the done sentinel.
                        record = output_queue.get(timeout=min(0.05, remaining))
                    except queue.Empty:
                        if worker.is_alive():
                            continue
                        worker.join(timeout=0)
                        try:
                            # A multiprocessing queue's feeder can make the
                            # final record visible at process-exit time. Give
                            # that record precedence over the exit status.
                            remaining = deadline - time.monotonic()
                            record = output_queue.get(
                                timeout=min(0.05, max(0, remaining))
                            )
                        except queue.Empty:
                            error = f"grep: fallback worker exited {worker.exitcode}"
                            break
                    if record[0] == "done":
                        completed = True
                        break
                    if record[0] == "error":
                        error = record[1]
                        break
                    _, path, number, text_value = record
                    canonical = os.path.realpath(path)
                    if not _path_within(canonical, real_root) or _is_denied_tool_path(canonical):
                        continue
                    rendered = path if files_only else f"{path}:{number}:{text_value}"
                    if rendered not in lines:
                        lines.append(rendered)
            finally:
                if completed:
                    worker.join(timeout=min(1, max(0.01, deadline - time.monotonic())))
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=1)
                if worker.is_alive():
                    worker.kill()
                    worker.join()
                output_queue.close()
            if error:
                return None, error
            if worker.exitcode not in (0, None) and len(lines) < max_hits:
                return None, f"grep: fallback worker exited {worker.exitcode}"
            return lines, None

        lines, err = await asyncio.to_thread(_grep)
        if err:
            return {"error": err, "exit_code": 1}
        if search_v2:
            selected = lines[cursor:cursor + page_size]
            has_more = len(lines) > cursor + page_size
            next_cursor = cursor + page_size if has_more else None
            result = {
                "output": _truncate("\n".join(selected) or f"No matches for {pattern!r} under {root}"),
                "exit_code": 0,
                "mode": "files" if files_only else "matches",
                "next_cursor": next_cursor,
                "result_limit": 1000,
                "truncated": len(lines) >= 1000,
            }
            result["files" if files_only else "matches"] = selected
            return result
        if not lines:
            return {"output": f"No matches for {pattern!r} under {root}", "exit_code": 0}
        out = "\n".join(ln[:_CODENAV_MAX_LINE] for ln in lines)
        if len(lines) >= max_hits:
            out += f"\n... [capped at {max_hits} matches]"
        return {"output": _truncate(out), "exit_code": 0}


class SearchFilesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return {"error": "search_files: arguments must be a JSON object", "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "search_files: arguments must be a JSON object", "exit_code": 1}
        args["_search_v2"] = True
        return await GrepTool().execute(json.dumps(args), ctx)

class GetWorkspaceTool:
    """Report the active workspace folder (no args). File tools are confined to
    it; the shell starts there (cwd) but is NOT sandboxed."""
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import get_active_workspace
        ws = get_active_workspace()
        if ws:
            return {
                "output": f"{ws}\n(File tools are confined to this folder; the shell starts "
                          f"here but is not sandboxed and can reach outside it.)",
                "exit_code": 0,
            }
        return {
            "output": "No workspace is set. File tools use the default allowed roots; "
                      "resolve paths from the user or use absolute paths.",
            "exit_code": 0,
        }
