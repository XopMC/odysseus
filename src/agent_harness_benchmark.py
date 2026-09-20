"""Reproducible A/B measurements for long-running Agent harness profiles.

The runner is transport-agnostic: production/API tests provide an ``invoke``
callable and a frozen task set.  Raw responses and metrics are retained so a
latency/token win can never hide a quality regression.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import statistics
import time
from typing import Awaitable, Callable


@dataclass(frozen=True)
class Trial:
    task_id: str
    profile: str
    input_tokens: int
    output_tokens: int
    requests: int
    ttft_seconds: float
    duration_seconds: float
    quality: float
    completed: bool
    evidence: list[str]


async def run(tasks: list[dict], profiles: list[str], *,
              invoke: Callable[[str, dict], Awaitable[dict]]) -> dict:
    frozen = json.loads(json.dumps(tasks, sort_keys=True))
    task_hash = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    trials: list[Trial] = []
    for profile in profiles:
        for task in frozen:
            started = time.monotonic()
            result = await invoke(profile, dict(task))
            trials.append(Trial(
                task_id=str(task.get("id") or ""), profile=profile,
                input_tokens=max(0, int(result.get("input_tokens") or 0)),
                output_tokens=max(0, int(result.get("output_tokens") or 0)),
                requests=max(0, int(result.get("requests") or 0)),
                ttft_seconds=max(0.0, float(result.get("ttft_seconds") or 0.0)),
                duration_seconds=max(0.0, float(result.get("duration_seconds") or (time.monotonic() - started))),
                quality=max(0.0, min(1.0, float(result.get("quality") or 0.0))),
                completed=bool(result.get("completed")),
                evidence=[str(x) for x in result.get("evidence") or []],
            ))
    aggregates = {}
    for profile in profiles:
        rows = [row for row in trials if row.profile == profile]
        aggregates[profile] = {
            "trials": len(rows),
            "input_tokens": sum(row.input_tokens for row in rows),
            "output_tokens": sum(row.output_tokens for row in rows),
            "requests": sum(row.requests for row in rows),
            "median_ttft_seconds": statistics.median([row.ttft_seconds for row in rows]) if rows else 0,
            "median_duration_seconds": statistics.median([row.duration_seconds for row in rows]) if rows else 0,
            "mean_quality": statistics.fmean([row.quality for row in rows]) if rows else 0,
            "completion_rate": statistics.fmean([1.0 if row.completed else 0.0 for row in rows]) if rows else 0,
        }
    return {"schema": 1, "task_set_sha256": task_hash,
            "trials": [asdict(row) for row in trials], "aggregates": aggregates}


def run_sync(tasks: list[dict], profiles: list[str], *, invoke) -> dict:
    return asyncio.run(run(tasks, profiles, invoke=invoke))
