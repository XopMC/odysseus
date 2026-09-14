"""Host dispatch must remain behind the normal execution/ownership gates."""
import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('gate', ['disabled', 'guide', 'admin', 'external'])
def test_host_route_cannot_bypass_policy(monkeypatch, gate):
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    called = []

    async def host(*args):
        called.append(args)
        return {'output': 'unexpected', 'exit_code': 0}

    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(host_execution, 'execute', host)
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: gate != 'admin')
    context = ToolRunSecurityContext()
    if gate == 'external':
        context.observe_tool_result('web_search', {'output': 'untrusted', 'exit_code': 0})
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type='bash', content='echo safe'), owner='xopmc',
        disabled_tools={'bash'} if gate == 'disabled' else None,
        tool_policy=SimpleNamespace(blocks=lambda name: True) if gate == 'guide' else None,
        security_context=context))
    assert result['exit_code'] != 0
    assert called == []


def test_authorized_owner_routes_actual_content(monkeypatch):
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    called = []

    async def host(tool, content):
        called.append((tool, content))
        return {'output': 'host', 'exit_code': 0}

    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(host_execution, 'execute', host)
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type='bash', content='echo safe'), owner='xopmc',
        security_context=ToolRunSecurityContext()))
    assert result['output'] == 'host'
    assert called == [('bash', 'echo safe')]


def test_background_uses_existing_job_lifecycle(monkeypatch):
    from src import bg_jobs, host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    called = []
    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    monkeypatch.setattr(host_execution, 'background_command', lambda tool, content: 'fixed-ssh-wrapper')
    monkeypatch.setattr(bg_jobs, 'launch', lambda command, **kwargs: called.append((command, kwargs)) or {'id': 'host-job'})
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type='bash', content='#!bg\necho safe'), owner='xopmc',
        session_id='chat-owner', security_context=ToolRunSecurityContext()))
    assert result['bg_job_id'] == 'host-job'
    assert called[0][0] == 'fixed-ssh-wrapper'
    assert called[0][1]['session_id'] == 'chat-owner'
