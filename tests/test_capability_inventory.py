"""Capability inventory must be conservative and omit endpoint secrets."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import capability_inventory as inventory


class _Rows:
    def __init__(self, result):
        self.result = result

    def filter(self, *clauses):
        return self

    def count(self):
        return self.result if isinstance(self.result, int) else len(self.result)

    def all(self):
        return self.result if isinstance(self.result, list) else []


class _Db:
    def __init__(self, model_count=0, mcp_names=()):
        self.model_count = model_count
        self.mcp_names = [(name,) for name in mcp_names]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, target):
        return _Rows(self.model_count if target is inventory.ModelEndpoint else self.mcp_names)


def test_inventory_never_claims_unconfigured_features_work(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_ENGINEERING_ENABLED", raising=False)
    monkeypatch.delenv("ODYSSEUS_ISOLATED_RUNNER_ENABLED", raising=False)
    monkeypatch.setattr(inventory, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(inventory, "discover_lsp", lambda: [{"language": "python", "available": True}])
    monkeypatch.setattr(inventory, "get_mcp_manager", lambda: None)
    result = inventory.build_inventory("alice")
    assert result["schema_version"] == 1
    assert all(row["status"] == "unavailable" for row in result["capabilities"].values())


def test_inventory_distinguishes_configured_from_live_without_secrets(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_ENGINEERING_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_ISOLATED_RUNNER_ENABLED", "1")
    monkeypatch.setattr(inventory, "SessionLocal", lambda: _Db(1, ["Browser MCP"]))
    monkeypatch.setattr(inventory, "discover_lsp", lambda: [{"language": "python", "available": True}])

    class Connected:
        def get_all_statuses(self):
            return {"server": {"status": "connected", "name": "Browser MCP", "tool_count": 2,
                               "error": "PRIVATE-PROVIDER-DETAIL"}}

    monkeypatch.setattr(inventory, "get_mcp_manager", lambda: Connected())
    result = inventory.build_inventory("alice")
    caps = result["capabilities"]
    assert caps["lsp"]["status"] == "experimental"
    assert caps["browser"]["status"] == "working"
    assert caps["mcp"]["status"] == "working"
    assert caps["models"]["status"] == "experimental"
    assert caps["dap"]["status"] == caps["cross_host_worktree"]["status"] == "unavailable"
    assert "PRIVATE-PROVIDER-DETAIL" not in json.dumps(result)


def test_inventory_route_requires_chat_scope_for_bearer_token(monkeypatch):
    from routes.codex_routes import setup_codex_routes

    router = setup_codex_routes()
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/codex/inventory")
    request = SimpleNamespace(state=SimpleNamespace(
        api_token=True, api_token_owner="alice", api_token_scopes=["todos:read"],
        current_user="api",
    ))
    with pytest.raises(HTTPException) as caught:
        endpoint(request)
    assert caught.value.status_code == 403

    request.state.api_token_scopes = ["chat"]
    monkeypatch.setattr(inventory, "build_inventory", lambda owner: {"owner_seen": owner})
    assert endpoint(request) == {"owner_seen": "alice"}
