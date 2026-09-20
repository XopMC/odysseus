"""Content-addressed large tool observations with exact paged recall.

The append-only transcript remains authoritative.  Projection may replace an
old large result with a stable handle only after the provider has received the
full result twice.  Storage/recall failures are deliberately fail-open.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Iterable, Optional

from src.constants import DATA_DIR


THRESHOLD_BYTES = 10 * 1024
FULL_SENDS = 2
EXCERPT_BYTES = 1024
RECALL_MAX_BYTES = 16 * 1024
RECALL_MAX_LINES = 400
_ID = re.compile(r"^obs_[a-f0-9]{24}$")
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


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
    return _scope(owner, session_id) / "objects" / f"{observation_id}.txt"


def observation_id(tool_name: str, tool_call_id: str, text: str) -> str:
    content_hash = _hash(text)
    return "obs_" + _hash(f"{tool_name}\0{tool_call_id}\0{content_hash}")[:24]


def archive(owner: Optional[str], session_id: Optional[str], *, tool_name: str,
            tool_call_id: str, text: str, force: bool = False) -> Optional[dict]:
    data = str(text or "").encode("utf-8")
    if not force and len(data) <= THRESHOLD_BYTES:
        return None
    oid = observation_id(tool_name, tool_call_id, text)
    path = _path(owner, session_id, oid)
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
    return {
        "id": oid,
        "sha256": _hash(data),
        "bytes": len(data),
        "lines": text.count("\n") + (0 if not text or text.endswith("\n") else 1),
        "tool": tool_name,
    }


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
                     session_id: Optional[str]) -> tuple[list[dict], dict]:
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
            )
            if meta and assistant_after[index] >= FULL_SENDS:
                replacement = placeholder(meta, content)
                message["content"] = replacement
                packed += 1
                removed += max(0, len(content.encode("utf-8")) - len(replacement.encode("utf-8")))
        except (OSError, ValueError):
            # Fail open: the exact original remains in the provider request.
            pass
        projected.append(message)
    return projected, {"packed": packed, "removed_bytes": removed}


def recall(owner: Optional[str], session_id: Optional[str], observation_id_value: str,
           offset: int = 0) -> dict:
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid observation offset")
    path = _path(owner, session_id, observation_id_value)
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
    return {
        "id": observation_id_value,
        "offset": offset,
        "next_offset": next_offset,
        "eof": next_offset >= status.st_size,
        "bytes": len(chunk),
        "text": chunk.decode("utf-8"),
    }
