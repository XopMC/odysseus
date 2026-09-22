"""Opt-in harness-efficiency profiles shared by Agent, Goal and children."""

from __future__ import annotations

from src.settings import get_setting


PROFILE_FEATURES = {
    "off": frozenset(),
    "performance": frozenset({"action_fusion", "observation_pack"}),
    "efficiency": frozenset({
        "action_fusion",
        "observation_pack",
        "evidence_reducer",
        "online_context_compact",
    }),
}

CORE_AGENT_TOOLS = frozenset({
    "get_workspace", "ls", "glob", "grep", "search_files", "read_file",
    "write_file", "edit_file", "apply_patch", "bash", "python", "todowrite",
    "read_tool_artifact",
    "manage_auto_research_lab",
})


def profile_name() -> str:
    value = str(get_setting("agent_efficiency_profile", "performance") or "performance")
    return value if value in PROFILE_FEATURES else "off"


def enabled(feature: str) -> bool:
    return feature in PROFILE_FEATURES[profile_name()]
