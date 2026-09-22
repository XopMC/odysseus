"""Conservative, content-free inventory of capabilities on this web host.

An installed package or configured endpoint is not proof that a feature works.
Use ``experimental`` until the selected host/provider has been probed; never
turn a missing implementation into a green capability badge.
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import or_

from core.database import McpServer, ModelEndpoint, SessionLocal
from src.engineering_lsp import discover as discover_lsp
from src.tool_utils import get_mcp_manager


def build_inventory(owner: str | None) -> dict[str, Any]:
    engineering = os.environ.get("ODYSSEUS_ENGINEERING_ENABLED") == "1"
    isolated = engineering and os.environ.get("ODYSSEUS_ISOLATED_RUNNER_ENABLED") == "1"
    try:
        languages = sorted(
            row["language"] for row in discover_lsp() if row.get("available") is True
        )
    except Exception:
        languages = []
    model_count = 0
    mcp_count = 0
    browser_mcp = False
    connected_mcp = 0
    connected_browser = False
    try:
        with SessionLocal() as db:
            model_count = db.query(ModelEndpoint).filter(
                ModelEndpoint.is_enabled.is_(True),
                or_(ModelEndpoint.owner.is_(None), ModelEndpoint.owner == owner),
            ).count()
            enabled_mcp = db.query(McpServer.name).filter(McpServer.is_enabled.is_(True)).all()
            mcp_count = len(enabled_mcp)
            browser_mcp = any("browser" in str(row[0] or "").casefold() for row in enabled_mcp)
    except Exception:
        # An unavailable metadata database cannot prove a capability works.
        pass
    manager = get_mcp_manager()
    if manager is not None:
        try:
            statuses = manager.get_all_statuses()
            connected = [row for row in statuses.values()
                         if isinstance(row, dict) and row.get("status") == "connected"
                         and type(row.get("tool_count")) is int and row["tool_count"] > 0]
            connected_mcp = len(connected)
            connected_browser = any("browser" in str(row.get("name") or "").casefold()
                                    for row in connected)
        except Exception:
            pass

    def item(status: str, reason: str, **details: Any) -> dict[str, Any]:
        return {"status": status, "reason": reason, **details}

    return {
        "schema_version": 1,
        "capabilities": {
            "lsp": item("experimental" if engineering and languages else "unavailable",
                        "Server-owned LSP bridge; host/language support requires a live probe"
                        if engineering and languages else "No enabled bridge with an installed language server",
                        languages=languages if engineering else []),
            "dap": item("unavailable", "DAP adapter is not implemented"),
            "browser": item("working" if connected_browser else "experimental" if browser_mcp else "unavailable",
                            "Browser MCP transport is connected"
                            if connected_browser else "A browser MCP server is configured; runtime access still requires a live probe"
                            if browser_mcp else "No configured browser transport"),
            "worktree": item("experimental" if engineering else "unavailable",
                             "Local Team worktree primitives require a configured runner and explicit review"
                             if engineering else "Engineering runner is disabled"),
            "cross_host_worktree": item("unavailable", "Cross-host worktree handoff is not implemented"),
            "sandbox": item("experimental" if isolated else "unavailable",
                            "Isolated runner is enabled; each selected host still needs a probe"
                            if isolated else "Isolated runner is disabled; trusted-host shell is not a sandbox"),
            "mcp": item("working" if connected_mcp else "experimental" if mcp_count else "unavailable",
                        "At least one MCP server has a live connection"
                        if connected_mcp else "Configured servers require a live connection and tool probe"
                        if mcp_count else "No enabled MCP server configured",
                        configured_count=mcp_count, connected_count=connected_mcp),
            "models": item("experimental" if model_count else "unavailable",
                           "Configured endpoints require a fresh model probe"
                           if model_count else "No visible enabled model endpoint configured",
                           configured_endpoint_count=model_count),
        },
    }
