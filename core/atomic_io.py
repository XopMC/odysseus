"""Atomic JSON file writes.

Use this everywhere a JSON config file is persisted. A plain `open("w") +
json.dump` truncates the file on first write and only fills it with new
content afterwards — a kill -9 / power loss / OOM in between produces a
truncated or empty file. For password DBs (`auth.json`) and live state
(`sessions.json`, `settings.json`, `integrations.json`, `cookbook_state.json`),
that's a data-loss event.

`atomic_write_json` writes to a sibling tmp file, fsyncs, then `os.replace`s
into place. On POSIX `os.replace` is atomic on the same filesystem.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from typing import Any, Optional


def atomic_write_json(path: str, data: Any, *, indent: Optional[int] = None) -> None:
    """Atomically persist `data` as JSON at `path`.

    The temp file uses a random suffix so two concurrent writers saving the
    same file don't collide on the rename target. A PID suffix does not do
    this: the PID is constant for the life of a process, so two writers on
    the same path within one process (or one single-process container, where
    the PID never changes at all) still race for the same temp file.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        # Directly unlink to avoid a check-then-act race condition.
        # Swallows FileNotFoundError (on success path) and other cleanup OSErrors.
        try:
            os.unlink(tmp)
        except OSError:
            pass


def atomic_write_text(path: str, text: str, *, preserve_mode: bool = False,
                      exclusive: bool = False, preserve_metadata_from=None) -> None:
    if not isinstance(text, str):
        raise TypeError("atomic_write_text expects a string")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    metadata = preserve_metadata_from
    if metadata is None and preserve_mode:
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            pass

    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            if metadata is not None:
                if hasattr(os, "fchown"):
                    os.fchown(f.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(f.fileno(), stat.S_IMODE(metadata.st_mode))
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            # Link is an atomic O_EXCL-style publication on the same volume:
            # never overwrite a file created after the caller's preflight.
            os.link(tmp, path)
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
        try:
            directory_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is not available on every supported platform.
            pass
    finally:
        # Directly unlink to avoid a check-then-act race condition.
        # Swallows FileNotFoundError (on success path) and other cleanup OSErrors.
        try:
            os.unlink(tmp)
        except OSError:
            pass
