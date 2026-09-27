import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src import host_execution


def helper(path, *, browse=False):
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "scripts/host_exec.py")],
        input=json.dumps({"tool": "workspace_info", "content": {"path": str(path), "browse": browse}}),
        text=True, capture_output=True, timeout=10,
    )
    assert proc.returncode == 0
    return json.loads(proc.stdout)


def test_host_workspace_helper_resolves_directory_without_reading_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "nested").mkdir()
    (root / ".hidden").mkdir()
    (root / "file.txt").write_text("private content")
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    result = helper(link, browse=True)
    assert result["exit_code"] == 0
    assert result["path"] == str(root.resolve())
    assert result["is_directory"] is True
    assert result["dirs"] == [{"name": "nested", "path": str(root / "nested")}]
    assert "private content" not in json.dumps(result)
    assert helper(root / "file.txt")["is_directory"] is False
    assert helper(root / "missing")["is_directory"] is False


@pytest.fixture
def host_owner(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_HOST_OWNER", "qa")


def test_send_time_host_workspace_does_not_stat_container(monkeypatch, host_owner):
    import routes.chat_routes as routes
    import src.tool_execution as execution
    monkeypatch.setattr(routes, "get_current_user", lambda request: "qa")
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(execution, "vet_workspace", lambda raw: pytest.fail("container path probe"))
    calls = []
    def request(payload):
        calls.append(payload)
        return {"path": "/host-only/project", "is_directory": True, "exit_code": 0}
    monkeypatch.setattr(host_execution, "run_request", request)
    assert routes._resolve_request_workspace(object(), "/host-only/project") == ("/host-only/project", "")
    assert calls[0]["tool"] == "workspace_info"


def test_unprivileged_workspace_does_not_probe_host(monkeypatch, host_owner):
    import routes.chat_routes as routes
    monkeypatch.setattr(routes, "get_current_user", lambda request: "qa")
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: False)
    monkeypatch.setattr(host_execution, "run_request", lambda *a: pytest.fail("host path oracle"))
    assert routes._resolve_request_workspace(object(), "/host-only/project") == ("", "")


def test_explicit_host_file_infers_its_remote_parent(monkeypatch, host_owner):
    import routes.chat_routes as routes
    import src.tool_execution as execution
    monkeypatch.setattr(routes, "get_current_user", lambda request: "qa")
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(execution, "vet_workspace", lambda raw: pytest.fail("container lookup"))
    calls = []
    def request(payload):
        path = payload['content']['path']
        calls.append(path)
        return {"path": path, "is_directory": path == '/host-only/project',
                "is_file": path.endswith('.py'), "exit_code": 0}
    monkeypatch.setattr(host_execution, "run_request", request)
    assert routes._resolve_workspace_from_message_path(
        object(), "Review file /host-only/project/main.py") == ('/host-only/project', '')
    assert calls == ['/host-only/project/main.py', '/host-only/project']


@pytest.mark.parametrize("resolved", ["/", "/home/qa/.ssh", "/home/qa/.GNUPG/key", "relative/path"])
def test_host_bind_rejects_root_sensitive_or_invalid_reply(monkeypatch, host_owner, resolved):
    monkeypatch.setattr(host_execution, "run_request", lambda *a: {
        "path": resolved, "is_directory": True, "exit_code": 0})
    assert host_execution.vet_workspace("/requested/alias", owner="qa") is None


def test_host_workspace_owner_is_checked_before_transport(monkeypatch, host_owner):
    monkeypatch.setattr(host_execution, "run_request", lambda *a: pytest.fail("owner bypass"))
    with pytest.raises(PermissionError):
        host_execution.vet_workspace("/host-only/project", owner="other")


def test_host_calls_carry_bound_workspace_without_changing_global_default(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_CWD", "/default")
    request = host_execution.request_for("python", "print(1)", workspace="/selected")
    assert request["cwd"] == "/selected"
    assert host_execution.request_for("python", "print(1)")["cwd"] == "/default"
    seen = []
    monkeypatch.setattr(host_execution, "run_request", lambda request: seen.append(request) or {"exit_code": 0})
    asyncio.run(host_execution.execute("python", "print(1)", workspace="/selected"))
    assert seen[0]["cwd"] == "/selected"


def test_private_workspace_lookup_is_not_an_advertised_model_tool():
    assert "workspace_info" not in host_execution.TOOLS
    with pytest.raises(ValueError, match="unsupported"):
        host_execution.request_for("workspace_info", "{}")


def test_missing_host_validator_fails_closed_before_chat_run(monkeypatch, host_owner):
    from fastapi import HTTPException
    import routes.chat_routes as routes
    monkeypatch.setattr(routes, "get_current_user", lambda request: "qa")
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(host_execution, "run_request", lambda *a: {"exit_code": 1, "error": "old helper"})
    with pytest.raises(HTTPException) as exc:
        routes._resolve_request_workspace(object(), "/host-only/project")
    assert exc.value.status_code == 503


def test_workspace_picker_and_send_use_same_host_filesystem(monkeypatch, host_owner):
    import routes.workspace_routes as routes
    monkeypatch.setattr(routes, "get_current_user", lambda request: "qa")
    monkeypatch.setattr(routes, "owner_is_admin_or_single_user", lambda owner: True)
    calls = []
    def request(payload):
        calls.append(payload)
        return {"path": "/host-only/project", "parent": "/host-only",
                "dirs": [{"name": "src", "path": "/host-only/project/src"}],
                "truncated": False, "is_directory": True, "exit_code": 0}
    monkeypatch.setattr(host_execution, "run_request", request)
    endpoints = {route.path: route.endpoint for route in routes.setup_workspace_routes().routes}
    assert endpoints['/api/workspace/vet'](object(), path="/host-only/project") == {
        "ok": True, "path": "/host-only/project"}
    result = endpoints['/api/workspace/browse'](object(), path="/host-only/project")
    assert result['selectable'] is True
    assert result['execution_host'] == 'jetson'
    assert result['dirs'][0]['path'] == '/host-only/project/src'
    assert [call['content']['browse'] for call in calls] == [False, True]


def test_foreground_file_and_background_dispatch_keep_workspace(monkeypatch, host_owner):
    from types import SimpleNamespace
    from src import tool_execution, bg_jobs
    from src.tool_capabilities import ToolRunSecurityContext
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    seen = []
    async def execute(tool, content, **kwargs):
        seen.append(kwargs)
        return {'exit_code': 0, 'output': 'ok'}
    monkeypatch.setattr(host_execution, 'execute', execute)
    for tool in ['python', 'read_file', 'write_file']:
        asyncio.run(tool_execution.execute_tool_block(
            SimpleNamespace(tool_type=tool, content='safe'), owner='qa', session_id='session',
            workspace='/host-only/project', security_context=ToolRunSecurityContext()))
    assert len(seen) == 3
    assert all(row['workspace'] == '/host-only/project' for row in seen)
    commands = []
    def launch(command, **kwargs):
        # The SSH transport runs in the container, not in the host-only cwd.
        assert kwargs['cwd'] != '/host-only/project'
        assert os.path.isdir(kwargs['cwd'])
        commands.append(command)
        return {'id': 'job'}
    monkeypatch.setattr(bg_jobs, 'launch', launch)
    asyncio.run(tool_execution.execute_tool_block(
        SimpleNamespace(tool_type='bash', content='#!bg\necho safe'), owner='qa', session_id='session',
        workspace='/host-only/project', security_context=ToolRunSecurityContext()))
    import shlex, base64
    request = json.loads(base64.urlsafe_b64decode(shlex.split(commands[0])[-1]))
    assert request['cwd'] == '/host-only/project'
