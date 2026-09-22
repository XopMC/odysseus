import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
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
    assert result['code'] == 'unknown_outcome'
    assert result['retryable'] is False
    assert result['exit_code'] == 1
    manager._reconnect_builtin.assert_awaited_once_with('builtin_browser')


def test_browser_server_process_code_and_file_upload_are_never_dispatched():
    manager = McpManager()
    manager._sessions['builtin_browser'] = object()
    manager._connections['builtin_browser'] = {'status': 'connected', 'name': 'Browser'}
    manager._tools['builtin_browser'] = [
        {'name': name, 'description': name, 'input_schema': {'type': 'object', 'properties': {}}}
        for name in ('browser_snapshot', 'browser_run_code_unsafe', 'browser_file_upload',
                     'browser_drop', 'browser_evaluate', 'browser_network_request')
    ]
    calls = []
    async def fake_call(*args):
        calls.append(args)
        return {'stdout': 'unexpected', 'exit_code': 0}
    manager._do_call = fake_call
    tools = {tool['name']: tool for tool in manager.get_all_tools() if tool['server_id'] == 'builtin_browser'}
    assert tools['browser_snapshot']['is_disabled'] is False
    for name in ('browser_run_code_unsafe', 'browser_file_upload', 'browser_drop',
                 'browser_evaluate', 'browser_network_request'):
        assert tools[name]['is_disabled'] is True
        result = asyncio.run(manager.call_tool(f'mcp__builtin_browser__{name}', {}))
        assert result['exit_code'] == 1
    schemas = manager.get_all_openai_schemas()
    assert [schema['function']['name'] for schema in schemas] == [
        'mcp__builtin_browser__browser_snapshot']
    assert calls == []
    result = asyncio.run(manager.call_tool('mcp__builtin_browser__browser_snapshot', {}))
    assert result['exit_code'] == 0
    assert len(calls) == 1


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


def test_stdio_transport_closes_in_owning_task_after_cross_task_disconnect():
    manager = McpManager()
    owners = []

    class OwnedStack:
        def __init__(self):
            self.creator = asyncio.current_task()

        async def aclose(self):
            assert asyncio.current_task() is self.creator
            owners.append("closed")

    async def fake_connect(server_id, *_args):
        manager._stacks[server_id] = OwnedStack()
        manager._sessions[server_id] = object()
        return True

    async def scenario():
        with patch.object(manager, "_connect_stdio", side_effect=fake_connect):
            assert await manager.connect_server("owned", "n", "stdio", command="test")
            assert "owned" in manager._sessions
            await asyncio.create_task(manager.disconnect_server("owned"))
            assert "owned" not in manager._sessions

    asyncio.run(scenario())
    assert owners == ["closed"]


def test_http_late_oauth_connection_keeps_owning_task_until_disconnect():
    manager = McpManager()
    closed = []

    class OwnedStack:
        def __init__(self):
            self.creator = asyncio.current_task()

        async def aclose(self):
            assert asyncio.current_task() is self.creator
            closed.append(True)

    async def scenario():
        authorize = asyncio.Event()

        async def fake_http(server_id, name, url):
            await authorize.wait()
            manager._stacks[server_id] = OwnedStack()
            manager._sessions[server_id] = object()
            manager._connections[server_id] = {"status": "connected", "name": name}
            return True

        with patch.object(manager, "_connect_http", side_effect=fake_http):
            assert await manager._start_http_connect("http", "remote", "https://example.test/mcp", wait=0.01) is False
            assert manager._connections["http"]["status"] == "needs_auth"
            authorize.set()
            for _ in range(100):
                if manager._connections["http"]["status"] == "connected":
                    break
                await asyncio.sleep(0.001)
            assert manager._connections["http"]["status"] == "connected"
            await asyncio.create_task(manager.disconnect_server("http"))
            assert "http" not in manager._sessions

    asyncio.run(scenario())
    assert closed == [True]


def test_http_connector_timeout_is_not_misreported_as_oauth_wait():
    manager = McpManager()

    async def fail_http(*_args):
        raise asyncio.TimeoutError("provider deadline")

    async def scenario():
        with patch.object(manager, "_connect_http", side_effect=fail_http):
            assert await manager._start_http_connect(
                "http", "remote", "https://example.test/mcp", wait=1) is False
        assert manager._connections["http"]["status"] == "error"

    asyncio.run(scenario())


def test_disconnect_cancels_pending_http_authorization_without_waiting_for_browser():
    manager = McpManager()

    async def scenario():
        cancelled = asyncio.Event()

        async def wait_for_browser(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(manager, "_connect_http", side_effect=wait_for_browser):
            assert await manager._start_http_connect(
                "pending", "remote", "https://example.test/mcp", wait=0.01) is False
            await asyncio.wait_for(manager.disconnect_server("pending"), timeout=0.5)
        assert cancelled.is_set()
        assert "pending" not in manager._lifetime_tasks

    asyncio.run(scenario())


def test_http_transport_context_exits_in_its_creating_task():
    manager = McpManager()
    closed = []

    @asynccontextmanager
    async def transport(_url, auth):
        assert auth is not None
        owner = asyncio.current_task()
        try:
            yield None, None, None
        finally:
            assert asyncio.current_task() is owner
            closed.append(True)

    class Session:
        def __init__(self, *_streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    async def scenario():
        with (patch("mcp.ClientSession", Session),
              patch("mcp.client.streamable_http.streamablehttp_client", transport),
              patch("src.mcp_oauth.build_provider", return_value=object()),
              patch("src.mcp_oauth.clear_auth_url")):
            assert await manager.connect_server("http", "remote", "http", url="https://example.test/mcp")
            await asyncio.create_task(manager.disconnect_server("http"))

    asyncio.run(scenario())
    assert closed == [True]
