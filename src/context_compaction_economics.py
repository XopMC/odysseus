"""Plan-boundary economics for proactive context compaction.

The regular policy threshold remains the emergency safety rail.  This module
only decides whether paying one rewrite at a stable plan boundary is cheaper
than repeatedly prefilling the accumulated history over the expected horizon.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Optional


@dataclass(frozen=True)
class Economics:
    remaining_request_scale: float = 1.0
    remaining_request_stddev_k: float = 0.0
    window_reserve_tokens: int = 16_384
    first_compaction_request_scale: float = 2.0
    subsequent_compaction_margin: float = 1.5


DEFAULT_ECONOMICS = Economics()


@dataclass(frozen=True)
class Decision:
    compact: bool
    reason: str
    target_tokens: int
    estimated_savings: float
    rewrite_cost: float
    request_horizon: int
    cache_write_read_ratio: Optional[float]
    write_tokens: int
    archive_tokens: int
    memo_tokens: int
    completed_boundary_request_counts: Optional[list[int]]
    requests_per_boundary_mean: Optional[float]
    requests_per_boundary_lower_bound: Optional[float]
    unbounded_expected_remaining_requests: Optional[int]
    average_context_token_increment: Optional[float]
    window_request_upper_bound: Optional[int]
    expected_remaining_requests: Optional[int]
    breakeven_requests: Optional[float]
    combined_breakeven_requests: Optional[float]
    effective_horizon_requests: Optional[float]
    incremental_cache_cost_ratio: Optional[float]
    prior_compaction_count: int
    carried_debt_tokens: float
    cache_debt_repayment_tokens: float

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_remaining_requests(*, completed_boundary_request_counts: list[int],
                                remaining_boundaries: int, scale: float,
                                standard_deviation_k: float, context_tokens: int,
                                context_window_tokens: Optional[int],
                                average_context_token_increment: Optional[float]) -> dict:
    counts = [max(0, int(value)) for value in completed_boundary_request_counts]
    mean = sum(counts) / max(1, len(counts))
    lower = mean
    if standard_deviation_k:
        if len(counts) < 3:
            lower *= .5
        else:
            variance = sum((value - mean) ** 2 for value in counts) / (len(counts) - 1)
            lower = max(0.0, mean - standard_deviation_k * math.sqrt(variance))
    unbounded = 1 + math.floor(lower * max(0, int(remaining_boundaries)) * scale)
    upper = None if (context_window_tokens is None or average_context_token_increment is None
                     or average_context_token_increment <= 0) else max(
        0, math.floor((int(context_window_tokens) - int(context_tokens)) / average_context_token_increment)
    )
    return {"completed_boundary_request_counts": counts,
            "requests_per_boundary_mean": mean,
            "requests_per_boundary_lower_bound": lower,
            "unbounded_expected_remaining_requests": unbounded,
            "average_context_token_increment": average_context_token_increment,
            "window_request_upper_bound": upper,
            "expected_remaining_requests": unbounded if upper is None else min(unbounded, upper)}


def decide(*, write_tokens: Optional[int] = None, archive_tokens: Optional[int] = None,
           memo_tokens: int = 1000, context_tokens: Optional[int] = None,
           completed_boundary_request_counts: Optional[list[int]] = None,
           remaining_boundaries: int = 0,
           average_context_token_increment: Optional[float] = None,
           context_window_tokens: Optional[int] = None,
           prior_compaction_count: int = 0, carried_debt_tokens: float = 0,
           cache_debt_repayment_tokens: float = 0,
           cache_write_read_ratio: Optional[float] = 12.5,
           economics: Economics = DEFAULT_ECONOMICS,
           at_boundary: bool = True, used_tokens: Optional[int] = None,
           input_budget: Optional[int] = None, completed_boundaries: int = 0,
           tokens_since_boundary: int = 0, cache_debt_tokens: float = 0) -> Decision:
    """Apply the upstream economics; legacy fields adapt old callers for one release."""
    used = max(0, int(write_tokens if write_tokens is not None else (used_tokens or 0)))
    context = max(0, int(context_tokens if context_tokens is not None else used))
    if archive_tokens is None:
        budget = max(1, int(input_budget or max(used, 1)))
        if context_window_tokens is None:
            context_window_tokens = budget
        target = max(1024, min(used, int(budget * .55)))
        archive = max(0, used - target + int(memo_tokens))
        completed_boundary_request_counts = ([1] * max(0, int(completed_boundaries))
                                             if completed_boundary_request_counts is None
                                             else completed_boundary_request_counts)
        forced_reason = ("awaiting_plan_boundary" if not at_boundary and used < int(budget * .92)
                         else "insufficient_new_context" if tokens_since_boundary < max(512, int(budget * .03))
                         and used < int(budget * .92) else None)
        carried_debt_tokens = max(float(carried_debt_tokens), float(cache_debt_tokens))
    else:
        archive = max(0, int(archive_tokens))
        target = max(1, used - archive + max(0, int(memo_tokens)))
        forced_reason = None if at_boundary else "awaiting_plan_boundary"
    memo = max(0, int(memo_tokens))
    counts = None if completed_boundary_request_counts is None else list(completed_boundary_request_counts)
    horizon = None if counts is None else estimate_remaining_requests(
        completed_boundary_request_counts=counts, remaining_boundaries=remaining_boundaries,
        scale=economics.remaining_request_scale, standard_deviation_k=economics.remaining_request_stddev_k,
        context_tokens=context, context_window_tokens=context_window_tokens,
        average_context_token_increment=average_context_token_increment)
    try:
        ratio = None if cache_write_read_ratio is None else max(0.0, float(cache_write_read_ratio))
    except (TypeError, ValueError):
        ratio = None
    saving = archive - memo
    incremental = None if ratio is None else max(0.0, ratio - 1.0)
    breakeven = used * incremental / saving if saving > 0 and incremental is not None else None
    combined = ((max(0.0, carried_debt_tokens) + used * incremental) / saving
                if saving > 0 and incremental is not None else None)
    first = max(0, int(prior_compaction_count)) == 0
    expected = horizon["expected_remaining_requests"] if horizon else None
    effective = (None if expected is None else
                 min(expected * economics.first_compaction_request_scale,
                     horizon["window_request_upper_bound"] if horizon["window_request_upper_bound"] is not None else math.inf)
                 if first else expected)
    window_protection = (context_window_tokens is not None
                         and context >= int(context_window_tokens) - economics.window_reserve_tokens)
    base = bool(expected and breakeven is not None and breakeven <= expected)
    first_economic = bool(first and effective and breakeven is not None and breakeven <= effective)
    margin_open = bool(not first and expected is not None and breakeven is not None
                       and breakeven * economics.subsequent_compaction_margin <= expected)
    debt_open = bool(not first and expected is not None and combined is not None and combined <= expected)
    economic = first_economic if first else base and margin_open and debt_open
    compressible = saving > 0
    compact = compressible and (window_protection or economic) and forced_reason is None
    reason = (forced_reason or ("non_positive_saving" if not compressible else
              "window_protection" if window_protection else "economic" if economic else
              "horizon_unavailable" if horizon is None else "cache_ratio_unavailable" if breakeven is None else
              "deferred_subsequent_margin" if not first and base and not margin_open else
              "deferred_carried_debt" if not first and base and not debt_open else "deferred_economic"))
    fields = horizon or {"completed_boundary_request_counts": None, "requests_per_boundary_mean": None,
                         "requests_per_boundary_lower_bound": None, "unbounded_expected_remaining_requests": None,
                         "average_context_token_increment": average_context_token_increment,
                         "window_request_upper_bound": None, "expected_remaining_requests": None}
    requests = int(effective if effective is not None and math.isfinite(effective) else (expected or 0))
    return Decision(compact, reason, target, max(0.0, saving) * max(0, requests),
                    max(0.0, carried_debt_tokens) + used * (incremental or 0), requests, ratio,
                    used, archive, memo, fields["completed_boundary_request_counts"],
                    fields["requests_per_boundary_mean"], fields["requests_per_boundary_lower_bound"],
                    fields["unbounded_expected_remaining_requests"], fields["average_context_token_increment"],
                    fields["window_request_upper_bound"], fields["expected_remaining_requests"],
                    breakeven, combined, effective, incremental, max(0, int(prior_compaction_count)),
                    max(0.0, float(carried_debt_tokens)), max(0.0, float(cache_debt_repayment_tokens)))
