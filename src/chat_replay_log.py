"""Bounded on-disk SSE artifacts. No model/tool execution is recovered here.

The HTTP caller must authorize the session before reading. Files use opaque
run UUIDs, never user paths. An index on disk keeps replay RAM constant. Data
is flushed by unbuffered writes and fsynced at terminal checkpoints; a machine
power loss may discard the last uncheckpointed events. A process restart can
read complete frames but must report an unfinished run as interrupted.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import time

_WORD = struct.Struct('!Q')
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_RUN_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600


class ReplayLimitError(OSError):
    pass


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


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

    def path(self, suffix):
        return self.base.with_suffix(suffix)

    def _write_new(self, suffix, data):
        fd = os.open(self.path(suffix), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)

    def prune(self):
        # Only expired artifacts with a terminal checkpoint can be removed.
        # Never delete an active run to make space for another one.
        for path in self.root.glob('*.json'):
            if not re.fullmatch(r'[0-9a-f]{32}', path.stem):
                continue
            meta = json.loads(path.read_text())
            if (meta.get('status') in ('done', 'error', 'stopped') and
                    time.time() - meta.get('updated_at', time.time()) > RETENTION_SECONDS):
                for suffix in ('.events', '.index', '.json'):
                    path.with_suffix(suffix).unlink(missing_ok=True)

    def __len__(self):
        return self.path('.index').stat().st_size // _WORD.size

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
        # Enforce a global ceiling including abandoned/crashed artifacts. These
        # are not silently discarded; an operator can inspect them first.
        total = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        if total + len(raw) + 16 > MAX_TOTAL_BYTES:
            raise ReplayLimitError('Replay storage is full')
        with self.path('.events').open('ab', buffering=0) as data:
            data.write(_WORD.pack(len(raw)) + raw)
        # Publish the index only after the complete event frame was written.
        with self.path('.index').open('ab', buffering=0) as index:
            index.write(_WORD.pack(offset))

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
        if type(after_seq) is not int or after_seq < -1 or type(limit) is not int or not 1 <= limit <= 100:
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
