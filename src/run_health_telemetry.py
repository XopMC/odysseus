"""Content-free latency measurements derived from canonical run events."""

import time


class RunHealthTelemetry:
    def __init__(self, started_at: float) -> None:
        self.round_started_at = started_at
        self.round_first_token_seen = False
        self.ttft_last_ms = None
        self.ttft_max_ms = None
        self.ttft_count = 0
        self.prefill_tps_last = None
        self.tool_started = {}
        self.tool_latency_sum_ms = 0.0
        self.tool_latency_max_ms = 0.0
        self.tool_latency_count = 0
        self.compaction_failures = 0
        self.compaction_last_ms = None
        self.compaction_max_ms = None
        self.sse_reconnects = 0

    def reconnect(self) -> None:
        self.sse_reconnects += 1

    def observe(self, payload: dict, *, now: float | None = None) -> None:
        if not isinstance(payload, dict):
            return
        now = time.time() if now is None else now
        kind = payload.get("type")
        if kind == "agent_step":
            self.round_started_at = now
            self.round_first_token_seen = False
        elif isinstance(payload.get("delta"), str) and payload["delta"] and not self.round_first_token_seen:
            elapsed = max(0.0, now - self.round_started_at) * 1000
            self.ttft_last_ms = round(elapsed, 1)
            self.ttft_max_ms = round(max(self.ttft_max_ms or 0, elapsed), 1)
            self.ttft_count += 1
            self.round_first_token_seen = True
        elif kind == "tool_start":
            key = payload.get("tool_call_id") or payload.get("tool")
            if isinstance(key, str) and len(key) <= 200:
                self.tool_started[key] = now
        elif kind == "tool_output":
            key = payload.get("tool_call_id") or payload.get("tool")
            started = self.tool_started.pop(key, None) if isinstance(key, str) else None
            if started is not None:
                elapsed = max(0.0, now - started) * 1000
                self.tool_latency_sum_ms += elapsed
                self.tool_latency_max_ms = max(self.tool_latency_max_ms, elapsed)
                self.tool_latency_count += 1
        elif kind == "context_compaction_failed":
            self.compaction_failures += 1
            self._compaction_duration(payload)
        elif kind == "compacted":
            self._compaction_duration(payload)
        elif kind == "metrics":
            data = payload.get("data")
            value = data.get("prefill_tps") if isinstance(data, dict) else None
            if type(value) in (int, float) and 0 < value < 1_000_000:
                self.prefill_tps_last = round(value, 2)

    def _compaction_duration(self, payload: dict) -> None:
        value = payload.get("duration_ms")
        if type(value) in (int, float) and 0 <= value < 3_600_000:
            self.compaction_last_ms = round(value, 1)
            self.compaction_max_ms = round(max(self.compaction_max_ms or 0, value), 1)

    def snapshot(self) -> dict:
        return {
            "ttft_last_ms": self.ttft_last_ms,
            "ttft_max_ms": self.ttft_max_ms,
            "ttft_count": self.ttft_count,
            "prefill_tps_last": self.prefill_tps_last,
            "tool_latency_mean_ms": round(self.tool_latency_sum_ms / self.tool_latency_count, 1)
            if self.tool_latency_count else None,
            "tool_latency_max_ms": round(self.tool_latency_max_ms, 1)
            if self.tool_latency_count else None,
            "tool_latency_count": self.tool_latency_count,
            "compaction_failures": self.compaction_failures,
            "compaction_last_ms": self.compaction_last_ms,
            "compaction_max_ms": self.compaction_max_ms,
            "sse_reconnects": self.sse_reconnects,
        }
