"""Host diagnostics cannot inspect another user or a recycled process id."""

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('host_exec_diagnostic_test', ROOT / 'scripts/host_exec.py')
host_exec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_exec)


def _fixture(tmp_path):
    proc = tmp_path / 'proc'
    process = proc / '1234'
    (process / 'fd').mkdir(parents=True)
    (process / 'net').mkdir()
    fields = ['S'] + ['0'] * 21
    fields[1] = '100'
    fields[19] = '987654321'
    fields[21] = '3'
    (process / 'stat').write_text('1234 (fixture worker) ' + ' '.join(fields))
    (process / 'fd' / '3').symlink_to('socket:[555]')
    (process / 'net' / 'tcp').write_text(
        '  sl  local_address rem_address   st tx_queue tr tm->when retrnsmt   uid timeout inode\n'
        '   0: 00000000:13F4 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 555\n'
    )
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'service.log').write_text('one\ntwo\nthree\n')
    return proc, workspace


def _call(tool, args, workspace, proc):
    return host_exec._diagnostic(tool, json.dumps(args), str(workspace), str(proc))


def test_process_identity_is_exact_and_omits_arguments(tmp_path):
    proc, workspace = _fixture(tmp_path)
    result = _call('inspect_process', {'pid': 1234}, workspace, proc)
    assert result['exit_code'] == 0
    assert result['start_ticks'] == 987654321
    assert result['name'] == 'fixture worker'
    assert result['rss_bytes'] == 3 * os.sysconf('SC_PAGE_SIZE')
    assert 'argv' not in result and 'environment' not in result
    assert _call('inspect_process', {'pid': 1234, 'start_ticks': 987654320}, workspace, proc)['code'] == 'stale_revision'


def test_port_requires_fenced_owned_socket(tmp_path):
    proc, workspace = _fixture(tmp_path)
    args = {'pid': 1234, 'start_ticks': 987654321, 'port': 5108}
    result = _call('inspect_port', args, workspace, proc)
    assert result['exit_code'] == 0 and result['listening'] is True
    assert _call('inspect_port', {**args, 'port': 5109}, workspace, proc)['listening'] is False
    assert _call('inspect_port', {'pid': 1234, 'port': 5108}, workspace, proc)['exit_code'] == 1
    (proc / '1234' / 'fd' / '3').unlink()
    assert _call('inspect_port', args, workspace, proc)['listening'] is False


def test_log_tail_is_bounded_and_cannot_escape_workspace(tmp_path):
    proc, workspace = _fixture(tmp_path)
    args = {'pid': 1234, 'start_ticks': 987654321, 'path': 'service.log', 'lines': 2}
    result = _call('tail_log', args, workspace, proc)
    assert result['exit_code'] == 0
    assert result['output'] == 'two\nthree'
    assert result['truncated'] is True
    assert _call('tail_log', {**args, 'path': '../outside.log'}, workspace, proc)['code'] == 'permission_denied'
    (workspace / 'alias.log').symlink_to(workspace / 'service.log')
    assert _call('tail_log', {**args, 'path': 'alias.log'}, workspace, proc)['code'] == 'permission_denied'
    assert _call('tail_log', {**args, 'lines': 201}, workspace, proc)['exit_code'] == 1


def test_diagnostics_fail_closed_without_linux_proc(tmp_path):
    result = host_exec._diagnostic('inspect_process', '{}', str(tmp_path), str(tmp_path / 'missing-proc'))
    assert result['code'] == 'not_supported_by_route'


def test_other_unix_users_process_is_not_inspectable(tmp_path, monkeypatch):
    proc, workspace = _fixture(tmp_path)
    original = host_exec.os.stat
    target = str(proc / '1234')
    def other_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if str(path) == target:
            return SimpleNamespace(st_uid=os.geteuid() + 1)
        return info
    monkeypatch.setattr(host_exec.os, 'stat', other_owner)
    assert _call('inspect_process', {'pid': 1234}, workspace, proc)['code'] == 'permission_denied'


def test_unreadable_socket_fd_never_claims_port_is_closed(tmp_path, monkeypatch):
    proc, workspace = _fixture(tmp_path)
    monkeypatch.setattr(host_exec.os, 'readlink', lambda path: (_ for _ in ()).throw(PermissionError()))
    result = _call('inspect_port', {'pid': 1234, 'start_ticks': 987654321, 'port': 5108}, workspace, proc)
    assert result['code'] == 'permission_denied'
    assert 'listening' not in result


def test_agent_diagnostics_are_private_and_not_local_tools(monkeypatch):
    import asyncio
    import src.agent_tools as agent_tools
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    from src.tool_index import ToolIndex
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
    from src.tool_schemas import function_call_to_tool_block

    monkeypatch.setenv('ODYSSEUS_HOST_ENABLED', '0')
    index = ToolIndex.__new__(ToolIndex)
    index.retrieve = lambda query, k=8: []
    for tool in ('inspect_process', 'inspect_port', 'tail_log'):
        assert tool in agent_tools.TOOL_HANDLERS
        assert function_call_to_tool_block(tool, '{}').tool_type == tool
        assert ToolEffect.READ_PRIVATE in capabilities_for_tool(tool).effects
        assert tool in NON_ADMIN_BLOCKED_TOOLS
        assert tool in index.get_tools_for_query(f'call {tool} now')
        result = asyncio.run(agent_tools.TOOL_HANDLERS[tool]('{}', {'owner': 'alice'}))
        assert result['code'] == 'not_supported_by_route'
    assert 'inspect_process' not in index.get_tools_for_query('Use bash if needed.', always_include={'bash'})
