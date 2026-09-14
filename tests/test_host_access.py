"""Human-only elevation boundary: no internal/bearer or cross-origin access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from routes.host_access_routes import authorize


def request(**changes):
    values = dict(state=SimpleNamespace(api_token=False),
                  app=SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(get_username_for_token=lambda token: 'xopmc'))),
                  cookies={'odysseus_session': 'test'},
                  headers={'origin': 'http://localhost:5131', 'x-odysseus-host-action': 'one-shot-sudo'},
                  base_url='http://localhost:5131/', url=SimpleNamespace(scheme='http', hostname='localhost'))
    values.update(changes)
    return SimpleNamespace(**values)


def test_interactive_owner_can_authorize():
    with patch('src.host_execution.enabled_for', return_value=True):
        assert authorize(request(), True) == 'xopmc'


@pytest.mark.parametrize('changes', [
    {'cookies': {}}, {'state': SimpleNamespace(api_token=True)},
    {'headers': {'X-Odysseus-Internal-Token': 'internal'}},
    {'headers': {'origin': 'http://evil.test', 'x-odysseus-host-action': 'one-shot-sudo'}},
    {'headers': {'origin': 'http://localhost:5131'}},
    {'url': SimpleNamespace(scheme='http', hostname='192.168.50.6')},
])
def test_rejects_ambient_or_insecure_authority(changes):
    with patch('src.host_execution.enabled_for', return_value=True):
        with pytest.raises(HTTPException):
            authorize(request(**changes), True)


def test_sudo_password_only_stdin_and_no_shell():
    spec = importlib.util.spec_from_file_location('host_sudo_test', Path(__file__).parents[1] / 'scripts/host_sudo.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module.subprocess, 'Popen') as popen:
        popen.return_value.returncode = 0
        result = module.execute({'argv': ['id', '-u'], 'password': 'test-secret'})
        argv = popen.call_args.args[0]
        assert argv == ['/usr/bin/sudo', '-k', '-S', '-p', '', '--', 'id', '-u']
        assert 'test-secret' not in repr(popen.call_args)
        assert not popen.call_args.kwargs.get('shell')
        popen.return_value.communicate.assert_called_once_with(b'test-secret\n', timeout=30)
        assert result['exit_code'] == 0


def test_insecure_page_never_renders_password_form():
    with patch('src.host_execution.enabled_for', return_value=True):
        with pytest.raises(HTTPException) as exc:
            authorize(request(url=SimpleNamespace(scheme='http', hostname='192.168.50.6')))
        assert exc.value.status_code == 400
