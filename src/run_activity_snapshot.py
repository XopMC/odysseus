"""Bounded UI materialization of an active run, not a model checkpoint.

Token-event tails are not message tails. Keep coalesced recent activity so a
reconnect during a long tool call doesn't erase all preceding visible rounds.
The full event log remains authoritative and lazily pageable.
"""
from collections import deque
import copy
import json


class RunActivitySnapshot:
    MAX_RECORDS = 400
    MAX_ROUNDS = 50
    TEXT_CHARS = 65536
    MAX_RESPONSE_CHARS = 512000
    KINDS = frozenset({
        "agent_step", "tool_start", "tool_output", "tool_progress", "tool_call_progress",
        "model_actual", "fallback", "agent_prep", "ask_user", "plan_update", "goal_update",
    })

    def __init__(self):
        self.records = deque()
        self.round = 1
        self.size = 0

    def observe(self, payload, seq):
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        delta = payload.get("delta")
        if kind not in self.KINDS and not isinstance(delta, str):
            return
        replay = payload.get("_replay") or {}
        try:
            self.round = max(self.round, int(payload.get("round") or replay.get("round") or 1))
        except (TypeError, ValueError, OverflowError):
            pass
        channel = bool(payload.get("thinking") or payload.get("channel") in {"thinking", "thought"})
        key = (self.round, replay.get("segment_id"), channel) if isinstance(delta, str) else None
        previous = self.records[-1] if self.records else None
        if key is not None and previous and previous["key"] == key:
            record = previous
            record["seq"] = seq
            record["parts"].append(delta)
            record["chars"] += len(delta)
            record["size"] += len(delta)
            self.size += len(delta)
        elif kind == "tool_call_progress" and previous and previous["data"].get("type") == kind:
            # Argument streaming is still one pending call, not thousands of
            # messages. Keep its most recent state without losing its name.
            previous["seq"] = seq
            previous["data"].update({k: v for k, v in payload.items()
                                     if v is not None and (k != "name" or v)})
            size = len(json.dumps(previous["data"], ensure_ascii=False))
            self.size += size - previous["size"]
            previous["size"] = size
            return
        else:
            data = copy.deepcopy(payload)
            data.pop("delta", None)
            record = {"seq": seq, "first_seq": seq, "round": self.round,
                      "data": data, "key": key, "parts": deque([delta] if key is not None else []),
                      "chars": len(delta) if key is not None else 0, "truncated": False}
            record["size"] = len(json.dumps(data, ensure_ascii=False)) + record["chars"]
            self.size += record["size"]
            self.records.append(record)
        while record["chars"] > self.TEXT_CHARS and record["parts"]:
            excess = record["chars"] - self.TEXT_CHARS
            first = record["parts"].popleft()
            removed = min(excess, len(first))
            if removed < len(first):
                record["parts"].appendleft(first[removed:])
            record["chars"] -= removed
            record["size"] -= removed
            self.size -= removed
            record["truncated"] = True
        while self.records and (len(self.records) > self.MAX_RECORDS
                                or (len(self.records) > 1 and self.size > self.MAX_RESPONSE_CHARS)
                                or self.records[0]["round"] <= self.round - self.MAX_ROUNDS):
            self.size -= self.records.popleft()["size"]

    def snapshot(self):
        events = []
        used = 0
        first_seq = None
        for record in reversed(self.records):
            data = copy.deepcopy(record["data"])
            if record["key"] is not None:
                data["delta"] = "".join(record["parts"])
            replay = data.setdefault("_replay", {})
            replay["seq"] = record["seq"]
            if record["truncated"]:
                replay["preview_truncated"] = True
            size = len(json.dumps(data, ensure_ascii=False))
            if events and used + size > self.MAX_RESPONSE_CHARS:
                break
            events.append({"seq": record["seq"], "data": data})
            first_seq = record["first_seq"]
            used += size
        return {"events": list(reversed(events)), "first_seq": first_seq}
