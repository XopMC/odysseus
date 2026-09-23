"""Helpers for bounded, content-free per-run resource budget warnings."""

import math


WARNING_RESOURCES = frozenset({
    "model_rounds", "model_tokens", "model_requests", "wall_seconds",
    "tool_calls", "children",
})


def soft_budget_warning(resource, used, limit):
    """Return an 80%-of-hard-limit warning, or ``None`` when not applicable.

    Small integer limits use floor(80%) with a minimum threshold of one. This
    makes a two-request cap warn after the first request, while a one-request
    cap warns at its hard boundary instead of silently omitting the warning.
    The caller owns per-run deduplication and the run identity.
    """
    if not isinstance(resource, str) or resource not in WARNING_RESOURCES:
        return None
    if type(used) is not int or type(limit) is not int or used < 0 or limit <= 0:
        return None
    threshold = max(1, math.floor(limit * 0.8))
    if used < threshold:
        return None
    return {
        "type": "budget_warning",
        "resource": resource,
        "used": used,
        "limit": limit,
        "soft_limit": threshold,
    }
