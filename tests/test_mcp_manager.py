import asyncio
from unittest.mock import patch

from src.mcp_manager import _format_mcp_connection_error, McpManager


def test_lost_tool_reply_never_replays_side_effect_after_reconnect():
    from unittest.mock import AsyncMock
    manager = McpManager.__new__(McpManager)
    manager._sessions = {'builtin_browser': object()}
    manager.is_builtin = lambda identity: True
    effects = []
    async def dispatch(*args):
        effects.append('submitted')
        raise ConnectionError('reply lost after submission')
    manager._do_call = dispatch
    manager._reconnect_builtin = AsyncMock(return_value=True)
    result = asyncio.run(manager.call_tool('mcp__builtin_browser__browser_click', {'ref': 'submit'}))
    assert effects == ['submitted']
    assert result['outcome_unknown'] is True
    assert result['retryable'] is False
    assert result['exit_code'] == 1
    manager._reconnect_builtin.assert_awaited_once_with('builtin_browser')


def test_failed_reconnect_keeps_unknown_outcome_and_custom_server_is_not_retried():
    from unittest.mock import AsyncMock
    for builtin in (True, False):
        manager = McpManager.__new__(McpManager)
        manager._sessions = {'server': object()}
        manager.is_builtin = lambda identity: builtin
        manager._do_call = AsyncMock(side_effect=ConnectionError('private credential'))
        manager._reconnect_builtin = AsyncMock(side_effect=RuntimeError('reconnect failed'))
        result = asyncio.run(manager.call_tool('mcp__server__send', {}))
        assert result['outcome_unknown'] is True
        assert result['retryable'] is False
        assert 'private credential' not in result['error']
        manager._do_call.assert_awaited_once()
        assert manager._reconnect_builtin.await_count == int(builtin)


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@0.0.80 --version" in msg
    assert "restart Odysseus" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    async def fake_start(server_id, name, url):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()
