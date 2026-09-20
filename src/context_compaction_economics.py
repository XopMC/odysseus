"""Plan-boundary economics for proactive context compaction.

The regular policy threshold remains the emergency safety rail.  This module
only decides whether paying one rewrite at a stable plan boundary is cheaper
than repeatedly prefilling the accumulated history over the expected horizon.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Decision:
    compact: bool
    reason: str
    target_tokens: int
    estimated_savings: int
    rewrite_cost: int
    request_horizon: int
    cache_write_read_ratio: float

    def to_dict(self) -> dict:
        return asdict(self)


def decide(*, at_boundary: bool, used_tokens: int, input_budget: int,
           completed_boundaries: int, tokens_since_boundary: int,
           cache_debt_tokens: int = 0,
           cache_write_read_ratio: float = 12.5) -> Decision:
    used = max(0, int(used_tokens))
    budget = max(1, int(input_budget))
    target = max(1024, min(used, int(budget * 0.55)))
    horizon = max(2, min(12, completed_boundaries + 2))
    try:
        ratio = min(1000.0, max(0.0, float(cache_write_read_ratio)))
    except (TypeError, ValueError):
        ratio = 12.5
    # 12.5 preserves the calibrated baseline; lower write/read prices make
    # proactive rewrites cheaper, while expensive cache writes delay them.
    rewrite = max(256, int(used * 0.30 * (ratio / 12.5))) + max(0, int(cache_debt_tokens))
    savings = max(0, used - target) * horizon
    if used >= int(budget * 0.92):
        return Decision(True, "window_protection", target, savings, rewrite, horizon, ratio)
    if not at_boundary:
        return Decision(False, "awaiting_plan_boundary", target, savings, rewrite, horizon, ratio)
    if tokens_since_boundary < max(512, int(budget * 0.03)):
        return Decision(False, "insufficient_new_context", target, savings, rewrite, horizon, ratio)
    if savings <= rewrite:
        return Decision(False, "rewrite_not_economic", target, savings, rewrite, horizon, ratio)
    return Decision(True, "economic_plan_boundary", target, savings, rewrite, horizon, ratio)
