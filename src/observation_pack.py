"""Content-addressed large tool observations with exact paged recall.

The append-only transcript remains authoritative.  Projection may replace an
old large result with a stable handle only after the provider has received the
full result twice.  Storage/recall failures are deliberately fail-open.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import fcntl
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from src.constants import DATA_DIR
from src.settings import get_setting


THRESHOLD_BYTES = 10 * 1024
FULL_SENDS = 2
EXCERPT_BYTES = 1024
RECALL_MAX_BYTES = 16 * 1024
RECALL_MAX_LINES = 400
SEARCH_MAX_BYTES = 32 * 1024 * 1024
_ID = re.compile(r"^obs_[a-f0-9]{24}$")
_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)


def _hash(value: str | bytes) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _scope(owner: Optional[str], session_id: Optional[str]) -> Path:
    owner_key = _hash(owner or "")[:24]
    session_key = _hash(session_id or "")[:24]
    return Path(DATA_DIR) / "tool_observations" / owner_key / session_key


def _path(owner: Optional[str], session_id: Optional[str], observation_id: str) -> Path:
    if not _ID.fullmatch(observation_id):
        raise ValueError("Invalid observation id")
    scope = _scope(owner, session_id)
    objects = scope / "objects"
    if scope.parent.is_symlink() or scope.is_symlink() or objects.is_symlink():
        raise OSError("Observation scope is not regular")
    return objects / f"{observation_id}.txt"


@contextmanager
def _owner_lock(owner: Optional[str]):
    root = _scope(owner, "").parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise OSError("Observation owner directory is not regular")
    fd = os.open(root / ".quota.lock", _LOCK_FLAGS, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def owner_usage(owner: Optional[str]) -> int:
    root = _scope(owner, "").parent
    total = 0
    try:
        for path in root.glob("*/objects/*.txt"):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    except OSError:
        return total
    return total


def delete_session(owner: Optional[str], session_id: Optional[str]) -> bool:
    """Remove one exact owner/session archive without following symlinks."""
    target = _scope(owner, session_id)
    base = (Path(DATA_DIR) / "tool_observations").resolve()
    resolved = target.resolve(strict=False)
    if resolved.parent.parent != base or target.is_symlink():
        raise OSError("Invalid observation scope")
    with _owner_lock(owner):
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True


def observation_id(tool_name: str, tool_call_id: str, text: str) -> str:
    content_hash = _hash(text)
    return "obs_" + _hash(f"{tool_name}\0{tool_call_id}\0{content_hash}")[:24]


def _journal(owner: Optional[str], session_id: Optional[str], event: str, **payload) -> None:
    path = _scope(owner, session_id) / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
        os.write(fd, (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode())
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def archive(owner: Optional[str], session_id: Optional[str], *, tool_name: str,
            tool_call_id: str, text: str, force: bool = False,
            run_id: Optional[str] = None) -> Optional[dict]:
    if run_id is not None and not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid observation run id")
    data = str(text or "").encode("utf-8")
    if not force and len(data) <= THRESHOLD_BYTES:
        return None
    oid = observation_id(tool_name, tool_call_id, text)
    path = _path(owner, session_id, oid)
    with _owner_lock(owner):
        if _scope(owner, session_id).is_symlink() or path.parent.is_symlink():
            raise OSError("Observation scope is not regular")
        object_limit = max(1024, int(get_setting("observation_pack_object_max_bytes", 16_777_216) or 16_777_216))
        owner_limit = max(object_limit, int(get_setting("observation_pack_owner_max_bytes", 536_870_912) or 536_870_912))
        if len(data) > object_limit:
            raise OSError("Observation exceeds the configured object quota")
        if not path.exists() and owner_usage(owner) + len(data) > owner_limit:
            raise OSError("Observation owner quota exceeded")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise OSError("Observation directory is not a regular directory")
        try:
            fd = os.open(path, _CREATE_FLAGS, 0o600)
        except FileExistsError:
            with os.fdopen(os.open(path, _READ_FLAGS), "rb") as handle:
                existing = handle.read()
            if existing != data:
                raise OSError("Observation integrity mismatch")
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        if run_id is not None:
            run_root = _scope(owner, session_id) / "runs"
            if run_root.is_symlink():
                raise OSError("Observation run index is not regular")
            run_dir = run_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if run_root.is_symlink() or run_dir.is_symlink() or not run_dir.is_dir():
                raise OSError("Observation run index is not a regular directory")
            # Seal the object before making its run index visible. If a crash
            # occurs between these writes, access fails closed until archive
            # retries and completes the index.
            marker = path.with_suffix(".runbound")
            try:
                marker_fd = os.open(marker, _CREATE_FLAGS, 0o600)
            except FileExistsError:
                marker_fd = os.open(marker, _READ_FLAGS)
                if not stat.S_ISREG(os.fstat(marker_fd).st_mode):
                    os.close(marker_fd)
                    raise OSError("Observation run marker is not regular")
            else:
                os.fsync(marker_fd)
            os.close(marker_fd)
            index_path = run_dir / f"{oid}.json"
            try:
                index_fd = os.open(index_path, _CREATE_FLAGS, 0o600)
            except FileExistsError:
                if index_path.is_symlink() or not index_path.is_file():
                    raise OSError("Observation run index is not regular")
            else:
                with os.fdopen(index_fd, "w", encoding="utf-8") as index:
                    json.dump({"id": oid, "tool": str(tool_name)[:80],
                               "bytes": len(data), "sha256": _hash(data)}, index)
                    index.flush()
                    os.fsync(index.fileno())
    return {
        "id": oid,
        "sha256": _hash(data),
        "bytes": len(data),
        "lines": text.count("\n") + (0 if not text or text.endswith("\n") else 1),
        "tool": tool_name,
    }


def search(owner: Optional[str], session_id: Optional[str], run_id: str,
           query: str, *, limit: int = 10, cursor: Optional[str] = None) -> dict:
    """Find bounded excerpts in artifacts indexed for one exact owned run.

    Legacy artifacts without a run index remain recallable by ID but are not
    attributed to a run by guessing from their session directory.
    """
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid observation run id")
    if not isinstance(query, str) or not 1 <= len(query) <= 128 or not query.strip() \
            or any(ch in query for ch in "\r\n\0"):
        raise ValueError("Invalid observation search query")
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Invalid observation search limit")
    if cursor is not None and (not isinstance(cursor, str) or not _ID.fullmatch(cursor)):
        raise ValueError("Invalid observation search cursor")
    scope = _scope(owner, session_id)
    run_root = scope / "runs"
    if scope.parent.is_symlink() or scope.is_symlink() or run_root.is_symlink():
        raise OSError("Observation run index is not regular")
    run_dir = run_root / run_id
    if not run_dir.exists():
        return {"run_id": run_id, "matches": [], "next_cursor": None, "scanned_bytes": 0}
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise OSError("Observation run index is not regular")
    entries = sorted(path for path in run_dir.glob("obs_*.json")
                     if _ID.fullmatch(path.stem) and (cursor is None or path.stem > cursor))
    matches = []
    scanned_bytes = 0
    last_id = None
    query_folded = query.casefold()
    max_scan_bytes = SEARCH_MAX_BYTES
    for path in entries:
        if len(matches) >= limit:
            break
        oid = path.stem
        budget_break = False
        try:
            fd = os.open(path, _READ_FLAGS)
            with os.fdopen(fd, "rb") as meta_stream:
                if not stat.S_ISREG(os.fstat(meta_stream.fileno()).st_mode):
                    continue
                meta = json.loads(meta_stream.read(1024))
            if not isinstance(meta, dict) or meta.get("id") != oid:
                continue
            object_path = _path(owner, session_id, oid)
            fd = os.open(object_path, _READ_FLAGS)
            with os.fdopen(fd, "rb") as stream:
                status = os.fstat(stream.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_size > 16_777_216:
                    continue
                if scanned_bytes + status.st_size > max_scan_bytes:
                    budget_break = True
                    break
                body = stream.read()
            if _hash(body) != meta.get("sha256"):
                continue
            scanned_bytes += len(body)
            for number, line in enumerate(body.decode("utf-8").splitlines(), 1):
                index = line.casefold().find(query_folded)
                if index >= 0:
                    matches.append({"id": oid, "tool": meta.get("tool"),
                                    "line": number, "snippet": line[max(0, index - 60):index + 100],
                                    "bytes": len(body)})
                    break
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
        finally:
            if not budget_break:
                last_id = oid
    remaining = any(path.stem > last_id for path in entries) if last_id else bool(entries)
    return {"run_id": run_id, "matches": matches, "next_cursor": last_id if remaining else None,
            "scanned_bytes": scanned_bytes}


def _complete_lines(text: str, budget: int, *, tail: bool) -> str:
    lines = text.splitlines(keepends=True)
    chosen: list[str] = []
    used = 0
    source: Iterable[str] = reversed(lines) if tail else lines
    for line in source:
        size = len(line.encode("utf-8"))
        if used + size > budget:
            break
        if tail:
            chosen.insert(0, line)
        else:
            chosen.append(line)
        used += size
    return "".join(chosen)


def placeholder(meta: dict, text: str) -> str:
    half = EXCERPT_BYTES // 2
    head = _complete_lines(text, half, tail=False)
    tail = _complete_lines(text, EXCERPT_BYTES - half, tail=True)
    return "\n".join([
        f"[large tool result replaced after {FULL_SENDS} full provider requests]",
        f"id: {meta['id']}",
        f"tool: {meta['tool']}",
        f"original_bytes: {meta['bytes']}",
        f"original_lines: {meta['lines']}",
        f"source_sha256: {meta['sha256']}",
        f"retrieve: call read_tool_artifact with id={meta['id']} and offset=0",
        "[first complete lines]",
        head,
        "[middle omitted; last complete lines]",
        tail,
    ])


def project_messages(messages: list[dict], *, owner: Optional[str],
                     session_id: Optional[str], run_id: Optional[str] = None) -> tuple[list[dict], dict]:
    """Project model messages and return deterministic savings telemetry."""
    assistant_after = [0] * len(messages)
    count = 0
    for index in range(len(messages) - 1, -1, -1):
        assistant_after[index] = count
        if messages[index].get("role") == "assistant":
            count += 1
    projected: list[dict] = []
    packed = 0
    removed = 0
    for index, original in enumerate(messages):
        message = {key: value for key, value in original.items() if not key.startswith("_observation_")}
        source = original.get("_observation_source")
        content = original.get("content")
        if not isinstance(source, dict) or not isinstance(content, str):
            projected.append(message)
            continue
        try:
            meta = archive(
                owner, session_id,
                tool_name=str(source.get("tool_name") or "tool"),
                tool_call_id=str(source.get("tool_call_id") or f"message-{index}"),
                text=content,
                run_id=run_id,
            )
            if meta and assistant_after[index] >= FULL_SENDS:
                replacement = placeholder(meta, content)
                message["content"] = replacement
                packed += 1
                saved = max(0, len(content.encode("utf-8")) - len(replacement.encode("utf-8")))
                removed += saved
                _journal(owner, session_id, "placeholder", id=meta["id"],
                         request=count + 1, send_number=assistant_after[index] + 1,
                         tool=meta["tool"], original_bytes=meta["bytes"],
                         original_lines=meta["lines"], content_hash=meta["sha256"],
                         placeholder_bytes=len(replacement.encode()), removed_bytes=saved,
                         removed_tokens=(saved + 3) // 4)
            elif meta:
                _journal(owner, session_id, "full", id=meta["id"], request=count + 1,
                         send_number=assistant_after[index] + 1, tool=meta["tool"],
                         original_bytes=meta["bytes"], original_lines=meta["lines"],
                         original_tokens=(meta["bytes"] + 3) // 4, content_hash=meta["sha256"])
        except (OSError, ValueError):
            # Fail open: the exact original remains in the provider request.
            pass
        projected.append(message)
    return projected, {"packed": packed, "removed_bytes": removed}


def recall(owner: Optional[str], session_id: Optional[str], observation_id_value: str,
           offset: int = 0, *, run_id: Optional[str] = None) -> dict:
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid observation offset")
    path = _path(owner, session_id, observation_id_value)
    marker = path.with_suffix(".runbound")
    if marker.is_symlink():
        raise OSError("Observation run marker is not regular")
    if marker.exists():
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise PermissionError("Observation belongs to a different run")
        run_root = _scope(owner, session_id) / "runs"
        run_dir = run_root / run_id
        if run_root.is_symlink() or run_dir.is_symlink():
            raise OSError("Observation run index is not regular")
        index_path = run_dir / f"{observation_id_value}.json"
        try:
            index_fd = os.open(index_path, _READ_FLAGS)
            with os.fdopen(index_fd, "rb") as index:
                if not stat.S_ISREG(os.fstat(index.fileno()).st_mode):
                    raise PermissionError("Observation belongs to a different run")
                index_meta = json.loads(index.read(1024))
            if not isinstance(index_meta, dict) or index_meta.get("id") != observation_id_value:
                raise PermissionError("Observation belongs to a different run")
        except (FileNotFoundError, ValueError, TypeError) as exc:
            raise PermissionError("Observation belongs to a different run") from exc
    fd = os.open(path, _READ_FLAGS)
    try:
        status = os.fstat(fd)
        if not os.path.isfile(path) or os.path.islink(path):
            raise OSError("Stored observation is not a regular file")
        if offset > status.st_size:
            raise ValueError("Observation offset exceeds size")
        data = os.pread(fd, min(RECALL_MAX_BYTES + 4, status.st_size - offset), offset)
    finally:
        os.close(fd)
    end = min(len(data), RECALL_MAX_BYTES)
    line_count = 0
    for index, byte in enumerate(data[:end]):
        if byte == 0x0A:
            line_count += 1
            if line_count == RECALL_MAX_LINES:
                end = index + 1
                break
    while end > 0 and end < len(data) and (data[end] & 0xC0) == 0x80:
        end -= 1
    chunk = data[:end]
    next_offset = offset + len(chunk)
    result = {
        "id": observation_id_value,
        "offset": offset,
        "next_offset": next_offset,
        "eof": next_offset >= status.st_size,
        "bytes": len(chunk),
        "text": chunk.decode("utf-8"),
    }
    try:
        _journal(owner, session_id, "recall", id=observation_id_value,
                 offset=offset, next_offset=next_offset, eof=result["eof"], bytes=len(chunk))
    except OSError:
        pass
    return result
