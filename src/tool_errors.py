"""Stable error categories and safe recovery guidance for agent tool results."""

from __future__ import annotations


_ACTIONS = {
    "not_found": "Refresh the inventory or check the exact ID/path before retrying.",
    "permission_denied": "Request the required access or narrow the action; do not retry unchanged.",
    "transport_unavailable": "Check the registered transport health before another attempt.",
    "not_supported_by_route": "Use a tool supported by this host route or update the host runner; do not retry unchanged.",
    "stale_revision": "Reload the latest revision and reapply against current state.",
    "unknown_outcome": "Inspect the external state or receipt before any retry; the action may have happened.",
    "timeout": "Check whether the operation completed; retry only if it is read-only or idempotent.",
    "failed": "Inspect the error and correct inputs; verify side effects before retrying.",
}
_CODE_MAP = {
    "not_found": "not_found",
    "permission_denied": "permission_denied",
    "disabled_by_policy": "permission_denied",
    "access_denied": "permission_denied",
    "transport_unavailable": "transport_unavailable",
    "unavailable_transport": "transport_unavailable",
    "not_supported_by_route": "not_supported_by_route",
    "stale_revision": "stale_revision",
    "unknown_outcome": "unknown_outcome",
    "timeout": "timeout",
}


def enrich_tool_error(result: dict) -> dict:
    """Add machine-readable recovery metadata without changing legacy fields.

    Never infer a safe replay from a provider error string. An unknown result is
    especially not proof that an effectful action failed to execute.
    """
    if not isinstance(result, dict) or result.get("approval_required") is True:
        return result
    if result.get("exit_code") == 0 and result.get("outcome_unknown") is not True:
        return result
    if (result.get("exit_code") is None and not result.get("error")
            and not result.get("code") and result.get("outcome_unknown") is not True):
        return result
    enriched = dict(result)
    code = str(result.get("code") or "")
    if result.get("outcome_unknown") is True:
        category = "unknown_outcome"
    elif result.get("timed_out") is True or result.get("exit_code") == 124:
        category = "timeout"
    elif result.get("blocked") is True:
        category = "permission_denied"
    else:
        category = _CODE_MAP.get(code, "failed")
    enriched["error_category"] = category
    enriched["next_action"] = _ACTIONS[category]
    # A timeout does not prove that an effectful action failed.  Transport
    # hints must not turn it into an automatic replay, even when the provider
    # marked the raw error retryable.
    enriched["retryable"] = (
        category not in {"unknown_outcome", "timeout"}
        and result.get("retryable") is True
    )
    return enriched
