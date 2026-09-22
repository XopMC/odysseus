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


def test_host_read_file_passes_owner_session_for_private_artifact(monkeypatch):
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    from types import SimpleNamespace
    captured = []
    async def host(tool, content, **scope):
        captured.append((tool, content, scope))
        return {'output': 'preview', 'exit_code': 0}
    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(host_execution, 'execute', host)
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    security = ToolRunSecurityContext()
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type='read_file', content='/tmp/readme.txt'),
        owner='xopmc', session_id='session-a',
        security_context=security))
    assert result['output'] == 'preview'
    assert captured == [('read_file', '/tmp/readme.txt',
                         {'owner': 'xopmc', 'session_id': 'session-a', 'run_id': security.run_id})]


@pytest.mark.parametrize('tool', ['run_tests', 'run_lint'])
def test_host_verification_routes_after_permission_gate_with_artifact_scope(monkeypatch, tool):
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    captured = []
    async def host(name, content, **scope):
        captured.append((name, content, scope))
        return {'output': 'verification result', 'exit_code': 0}
    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(host_execution, 'execute', host)
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    _, denied = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=tool, content='{}'), owner='xopmc', session_id='session-a',
        disabled_tools={tool}, security_context=ToolRunSecurityContext()))
    assert denied['exit_code'] != 0
    assert captured == []
    security = ToolRunSecurityContext()
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=tool, content='{}'), owner='xopmc', session_id='session-a',
        security_context=security))
    assert result['exit_code'] == 0
    assert captured == [(tool, '{}', {'owner': 'xopmc', 'session_id': 'session-a',
                                      'run_id': security.run_id})]


@pytest.mark.parametrize('tool', ['inspect_process', 'inspect_port', 'tail_log'])
def test_host_diagnostic_routes_only_after_owner_and_policy_gates(monkeypatch, tool):
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    captured = []
    async def host(name, content, **scope):
        captured.append((name, content, scope))
        return {'output': 'bounded host inspection', 'exit_code': 0}
    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(host_execution, 'execute', host)
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    _, denied = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=tool, content='{}'), owner='xopmc',
        disabled_tools={tool}, security_context=ToolRunSecurityContext()))
    assert denied['exit_code'] != 0 and captured == []
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=tool, content='{}'), owner='xopmc', session_id='session-a',
        security_context=ToolRunSecurityContext()))
    assert result['exit_code'] == 0
    assert captured == [(tool, '{}', {'owner': 'xopmc', 'session_id': 'session-a'})]


@pytest.mark.parametrize('tool', ['git_status', 'git_diff', 'git_log'])
def test_typed_git_tools_route_to_exact_host_adapter(monkeypatch, tool):
    from src import host_execution, tool_execution
    from src.tool_capabilities import ToolRunSecurityContext
    captured = []

    async def host(name, content):
        captured.append((name, content))
        return {'output': 'host git inspection', 'exit_code': 0}

    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '1')
    monkeypatch.setenv('ODYSSEUS_HOST_OWNER', 'xopmc')
    monkeypatch.setattr(host_execution, 'execute', host)
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    content = '{"path":"/home/xopmc/project","file":"a.txt"}'
    _, result = asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type=tool, content=content), owner='xopmc',
        security_context=ToolRunSecurityContext()))
    assert result['output'] == 'host git inspection'
    assert captured == [(tool, content)]


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
