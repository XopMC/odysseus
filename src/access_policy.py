"""Owner-selected approval modes for chat tool execution.

The preference controls *when the interactive approval gate is armed*.  It is
not a replacement for owner/role, project workspace, host transport, tool
registry, delegated-token, or external-content safety gates.  Those checks stay
authoritative at dispatch time even when ``full_access`` is selected.
"""

from __future__ import annotations

from typing import Any

from src.tool_capabilities import ToolCapabilities, ToolEffect


ACCESS_MODE_ASK_EVERY_TIME = "ask_every_time"
ACCESS_MODE_ASK_IMPORTANT = "ask_important"
ACCESS_MODE_FULL = "full_access"
ACCESS_MODES = frozenset(
    {ACCESS_MODE_ASK_EVERY_TIME, ACCESS_MODE_ASK_IMPORTANT, ACCESS_MODE_FULL}
)
DEFAULT_ACCESS_MODE = ACCESS_MODE_ASK_IMPORTANT

# A routine private/public read is useful without interrupting a task in the
# default mode.  Writes, code execution, network egress, UI effects and
# destructive/admin actions are the important operations users normally want to
# review before dispatch.
IMPORTANT_EFFECTS = frozenset(
    {
        ToolEffect.WRITE_WORKSPACE,
        ToolEffect.EXECUTE_CODE,
        ToolEffect.NETWORK_EGRESS,
        ToolEffect.EXTERNAL_SIDE_EFFECT,
        ToolEffect.UI_SIDE_EFFECT,
        ToolEffect.ADMIN_CHANGE,
        ToolEffect.DESTRUCTIVE,
    }
)


def normalize_access_mode(value: Any, *, default: str | None = DEFAULT_ACCESS_MODE) -> str | None:
    """Return a canonical mode or ``default`` for unknown/missing input."""

    candidate = str(value or "").strip().casefold()
    aliases = {
        "ask": ACCESS_MODE_ASK_EVERY_TIME,
        "every_time": ACCESS_MODE_ASK_EVERY_TIME,
        "ask_every": ACCESS_MODE_ASK_EVERY_TIME,
        "important": ACCESS_MODE_ASK_IMPORTANT,
        "ask_important_actions": ACCESS_MODE_ASK_IMPORTANT,
        "full": ACCESS_MODE_FULL,
        "all": ACCESS_MODE_FULL,
        "unrestricted": ACCESS_MODE_FULL,
    }
    candidate = aliases.get(candidate, candidate)
    if candidate in ACCESS_MODES:
        return candidate
    if default is None:
        return None
    return default if default in ACCESS_MODES else DEFAULT_ACCESS_MODE


def access_mode_requires_approval(
    mode: Any, capabilities: ToolCapabilities | None
) -> bool:
    """Whether the selected mode should arm the approval card for an action."""

    normalized = normalize_access_mode(mode, default=None)
    if normalized == ACCESS_MODE_FULL or capabilities is None:
        return False
    effects = set(capabilities.effects)
    if normalized == ACCESS_MODE_ASK_EVERY_TIME:
        # User-interaction tools are already an explicit model question; public
        # reads have no side effect and do not need a second approval card.
        return bool(effects - {ToolEffect.READ_PUBLIC, ToolEffect.USER_INTERACTION})
    return bool(effects & IMPORTANT_EFFECTS)


def access_mode_label(mode: Any) -> str:
    """Stable machine label used by logs and UI telemetry."""

    return normalize_access_mode(mode) or DEFAULT_ACCESS_MODE

