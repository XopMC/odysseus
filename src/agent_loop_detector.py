"""Bounded, content-free detection of repeated Agent tool evidence."""

from collections import deque
import hashlib
import json


_VOLATILE_KEYS = frozenset({"duration", "duration_ms", "elapsed", "elapsed_ms", "timestamp", "created_at", "updated_at"})


def _stable(value):
    if isinstance(value, dict):
        return {str(key): _stable(item) for key, item in value.items()
                if str(key) not in _VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint(batch):
    # Only hashes are retained; no tool arguments, results, or secrets enter
    # the detector's long-lived state or public events.
    # Preserve exact arguments. A timestamp in the requested action may be
    # meaningful; only timing metadata in the observation is volatile.
    normalized = [(tool, arguments, _stable(observation))
                  for tool, arguments, observation in batch]
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LoopDetector:
    """One diagnostic nudge, then a bounded escalation for unchanged evidence."""

    def __init__(self):
        self._recent = deque(maxlen=8)
        self._nudged = False

    def observe(self, batch):
        if not batch:
            return None
        fingerprint = _fingerprint(batch)
        self._recent.append(fingerprint)
        tail = list(self._recent)
        same = len(tail) >= 3 and tail[-3:] == [fingerprint] * 3
        cycle = len(tail) >= 5 and tail[-5] == tail[-3] == tail[-1] and tail[-4] == tail[-2]
        if not (same or cycle):
            if fingerprint not in tail[:-1]:
                self._nudged = False
            return None
        if not self._nudged:
            self._nudged = True
            return "nudge"
        # Require a further unchanged observation after the nudge. A changed
        # batch resets the warning and allows the agent to make progress.
        if len(tail) >= 5 and tail[-5:] == [fingerprint] * 5:
            return "escalate"
        if len(tail) >= 8 and tail[-8:-4] == tail[-4:]:
            return "escalate"
        return None
