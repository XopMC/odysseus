"""Bounded on-disk SSE artifacts. No model/tool execution is recovered here.

The HTTP caller must authorize the session before reading. Files use opaque
run UUIDs, never user paths. An index on disk keeps replay RAM constant. Data
is flushed by unbuffered writes and fsynced at terminal checkpoints; a machine
power loss may discard the last uncheckpointed events. A process restart can
read complete frames but must report an unfinished run as interrupted.
"""
import hashlib
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
import time

_WORD = struct.Struct('!Q')
MAX_EVENT_BYTES = 2 * 1024 * 1024


def _storage_limit(name, default, *, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Replay is durable chat history, not a cache. Keep bounded DoS protection,
# but provide enough headroom for multi-hour runs and let operators override it.
MAX_RUN_BYTES = _storage_limit(
    "ODYSSEUS_REPLAY_MAX_RUN_BYTES", 1024 * 1024 * 1024,
    minimum=16 * 1024 * 1024, maximum=64 * 1024 * 1024 * 1024,
)
MAX_TOTAL_BYTES = max(MAX_RUN_BYTES, _storage_limit(
    "ODYSSEUS_REPLAY_MAX_TOTAL_BYTES", 16 * 1024 * 1024 * 1024,
    minimum=64 * 1024 * 1024, maximum=512 * 1024 * 1024 * 1024,
))
logger = logging.getLogger(__name__)
_reasoning_index_lock = threading.Lock()


class ReplayLimitError(OSError):
    pass


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=32)
def _reasoning_index(base: str, event_size: int, event_count: int) -> dict:
    """Map reasoning rounds to frame numbers with one sequential disk pass."""
    sidecar = Path(base + '.reasoning-index')
    try:
        saved = json.loads(sidecar.read_text(encoding='utf-8'))
        if saved.get('event_size') == event_size and saved.get('event_count') == event_count:
            rounds = saved.get('rounds')
            if isinstance(rounds, dict) and all(
                isinstance(value, list) and all(type(seq) is int and 0 <= seq < event_count for seq in value)
                for value in rounds.values()
            ):
                return rounds
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass

    rounds = {}
    with Path(base + '.events').open('rb') as source:
        for seq in range(event_count):
            header = source.read(_WORD.size)
            if len(header) != _WORD.size:
                raise ValueError('Incomplete reasoning replay index')
            length = _WORD.unpack(header)[0]
            if length > MAX_EVENT_BYTES:
                raise ValueError('Invalid reasoning replay frame')
            frame = source.read(length)
            if len(frame) != length:
                raise ValueError('Incomplete reasoning replay frame')
            raw = '\n'.join(
                line[5:].lstrip() for line in frame.decode('utf-8').splitlines()
                if line.startswith('data:')
            )
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get('delta') is None:
                continue
            if payload.get('thinking') is not True and payload.get('channel') not in {'thinking', 'thought'}:
                continue
            replay = payload.get('_replay') if isinstance(payload.get('_replay'), dict) else {}
            try:
                round_number = max(1, int(payload.get('round') or replay.get('round') or 1))
            except (TypeError, ValueError):
                round_number = 1
            rounds.setdefault(str(round_number), []).append(seq)

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=sidecar.parent,
            prefix=f'.{sidecar.name}.', delete=False,
        ) as output:
            temporary = Path(output.name)
            os.chmod(output.name, 0o600)
            json.dump({'event_size': event_size, 'event_count': event_count, 'rounds': rounds}, output)
        os.replace(temporary, sidecar)
    except OSError:
        logger.warning('Reasoning replay index could not be persisted', exc_info=False)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return rounds


class ReplayLog:
    def __init__(self, root, run_id, session_id, *, create=False):
        if not re.fullmatch(r'[0-9a-f]{32}', run_id):
            raise ValueError('Invalid replay ID')
        self.root = Path(root)
        self.run_id = run_id
        self.base = self.root / run_id
        self.session_hash = _key(session_id)
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.prune()
            if sum(p.stat().st_size for p in self.root.iterdir() if p.is_file()) + 4096 > MAX_TOTAL_BYTES:
                raise ReplayLimitError('Replay storage is full')
            self._write_new('.events', b'')
            self._write_new('.index', b'')
            self._write_new('.json', json.dumps({
                'session_hash': self.session_hash, 'status': 'running',
                'created_at': time.time(), 'run_id': run_id,
            }).encode())
        meta = json.loads(self.path('.json').read_text())
        if meta.get('session_hash') != self.session_hash:
            raise FileNotFoundError('Replay not found')
        self.metadata = meta
        # Replay readers must not enumerate the entire artifact directory.
        # Only a writer needs the global quota baseline; cache it after the
        # first append and increment it thereafter. A new writer process
        # recalculates once, including data written by the previous process.
        self._total_bytes = None

    def path(self, suffix):
        return self.base.with_suffix(suffix)

    def _write_new(self, suffix, data):
        fd = os.open(self.path(suffix), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)

    def prune(self):
        # Timeline/reasoning is part of chat history, not an expiring cache.
        # It is removed only by the owner-scoped chat deletion path. The global
        # quota below fails closed instead of silently hollowing old bubbles.
        return

    def __len__(self):
        return self.path('.index').stat().st_size // _WORD.size

    def reasoning_sequences(self, round_number: int):
        """Return indexed thinking frames for a terminal run, without replaying every frame."""
        count = len(self)
        if self.metadata.get('status') == 'running':
            return range(count)
        event_size = self.path('.events').stat().st_size
        with _reasoning_index_lock:
            return _reasoning_index(str(self.base), event_size, count).get(str(round_number), [])

    def __getitem__(self, seq):
        if type(seq) is not int or seq < 0 or seq >= len(self):
            raise IndexError(seq)
        with self.path('.index').open('rb') as index:
            index.seek(seq * _WORD.size)
            offset = _WORD.unpack(index.read(_WORD.size))[0]
        with self.path('.events').open('rb') as data:
            data.seek(offset)
            length = _WORD.unpack(data.read(_WORD.size))[0]
            if length > MAX_EVENT_BYTES:
                raise ValueError('Invalid replay frame')
            event = data.read(length)
            if len(event) != length:
                raise ValueError('Incomplete replay frame')
        return event.decode('utf-8')

    def append(self, event):
        raw = event.encode('utf-8')
        if len(raw) > MAX_EVENT_BYTES:
            raise ReplayLimitError('Replay event exceeds storage limit')
        offset = self.path('.events').stat().st_size
        if offset + len(raw) + 8 > MAX_RUN_BYTES:
            raise ReplayLimitError('Replay run exceeds storage limit')
        if self._total_bytes is None:
            self._total_bytes = sum(
                p.stat().st_size for p in self.root.iterdir() if p.is_file()
            )
        # Enforce a global ceiling including abandoned/crashed artifacts. These
        # are not silently discarded; an operator can inspect them first.
        if self._total_bytes + len(raw) + 16 > MAX_TOTAL_BYTES:
            raise ReplayLimitError('Replay storage is full')
        with self.path('.events').open('ab', buffering=0) as data:
            data.write(_WORD.pack(len(raw)) + raw)
        # Publish the index only after the complete event frame was written.
        with self.path('.index').open('ab', buffering=0) as index:
            index.write(_WORD.pack(offset))
        self._total_bytes += len(raw) + _WORD.size

    def checkpoint(self, status):
        if status not in ('done', 'error', 'stopped'):
            raise ValueError('Invalid replay status')
        for suffix in ('.events', '.index'):
            with self.path(suffix).open('rb') as handle:
                os.fsync(handle.fileno())
        meta = {**self.metadata, 'status': status, 'updated_at': time.time()}
        temp = self.path('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(meta, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.path('.json'))
        self.metadata = meta

    def page(self, after_seq=-1, limit=100, *, active=False):
        if type(after_seq) is not int or after_seq < -1 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Invalid replay cursor or page size')
        count = len(self)
        if after_seq >= count:
            raise ValueError('Replay cursor is ahead of the log')
        events, used = [], 0
        for seq in range(after_seq + 1, min(count, after_seq + 1 + limit)):
            event = self[seq]
            used += len(event.encode())
            if events and used > MAX_EVENT_BYTES:
                break
            events.append({'seq': seq, 'event': event})
        cursor = events[-1]['seq'] if events else after_seq
        status = self.metadata['status']
        if status == 'running' and not active:
            status = 'interrupted'
        return {'run_id': self.run_id, 'status': status, 'events': events,
                'next_seq': cursor, 'has_more': cursor + 1 < count}

    def page_before(self, before_seq, limit=100, *, active=False):
        """Read the immediately preceding page by indexed sequence, newest window first.

        The response itself is chronological so an older-page renderer can
        prepend it without reversing event order. No earlier replay frames are
        scanned, even when a run has hundreds of thousands of events.
        """
        if (type(before_seq) is not int or before_seq < 0 or
                type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError('Invalid replay cursor or page size')
        count = len(self)
        if before_seq > count:
            raise ValueError('Replay cursor is ahead of the log')
        events, used = [], 0
        for seq in range(before_seq - 1, max(-1, before_seq - limit - 1), -1):
            event = self[seq]
            used += len(event.encode())
            if events and used > MAX_EVENT_BYTES:
                break
            events.append({'seq': seq, 'event': event})
        events.reverse()
        cursor = events[0]['seq'] if events else before_seq
        status = self.metadata['status']
        if status == 'running' and not active:
            status = 'interrupted'
        return {'run_id': self.run_id, 'status': status, 'events': events,
                'previous_cursor': cursor, 'has_more_before': cursor > 0}
