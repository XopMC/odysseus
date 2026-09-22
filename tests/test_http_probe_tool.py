"""A probe is a registered-endpoint HEAD request, never a model URL fetch."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import threading

import pytest

import src.agent_tools as agent_tools
from src.agent_tools import http_probe_tool as probe
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def test_http_probe_has_one_typed_schema_and_handler():
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, plan_mode_disabled_tools
    schemas = [schema for schema in FUNCTION_TOOL_SCHEMAS
               if schema.get('function', {}).get('name') == 'http_probe']
    assert len(schemas) == 1
    assert schemas[0]['function']['parameters']['required'] == ['endpoint_id']
    assert 'url' not in schemas[0]['function']['parameters']['properties']
    assert 'method' not in schemas[0]['function']['parameters']['properties']
    assert 'http_probe' in agent_tools.TOOL_HANDLERS
    assert ToolEffect.READ_PRIVATE in capabilities_for_tool('http_probe').effects
    assert ToolEffect.BROKERED_NETWORK_READ in capabilities_for_tool('http_probe').effects
    assert 'http_probe' in NON_ADMIN_BLOCKED_TOOLS
    assert 'http_probe' in plan_mode_disabled_tools()


class _ProbeHandler(BaseHTTPRequestHandler):
    seen = []

    def do_HEAD(self):
        self.seen.append((self.command, self.path, dict(self.headers)))
        self.send_response(302)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Location', 'http://127.0.0.1:1/redirect-must-not-run')
        self.send_header('Set-Cookie', 'secret=must-not-leak')
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def local_http():
    _ProbeHandler.seen = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_pinned_head_does_not_follow_redirect_or_expose_cookie(monkeypatch, local_http):
    monkeypatch.setattr(probe, '_resolve', lambda host, port: [
        (socket.AF_INET, ('127.0.0.1', local_http), '127.0.0.1')])
    result = probe.probe_url(f'http://registered.invalid:{local_http}/v1')
    assert result['exit_code'] == 0
    assert result['method'] == 'HEAD'
    assert result['status'] == 302
    assert result['redirect_followed'] is False
    assert result['connected_ip'] == '127.0.0.1'
    assert result['headers']['content-type'] == 'text/plain'
    assert 'location' not in result['headers']
    assert 'set-cookie' not in result['headers']
    assert len(_ProbeHandler.seen) == 1
    method, path, headers = _ProbeHandler.seen[0]
    assert (method, path) == ('HEAD', '/')
    assert headers['Host'] == f'registered.invalid:{local_http}'
    assert 'Authorization' not in headers


def test_url_and_dns_guards_reject_unregistered_ssrf_shapes(monkeypatch):
    for url in ('file:///etc/passwd', 'http://user:secret@host.test/',
                'http://host.test/?token=x', 'http://host.test/#frag',
                'http://host.test:0/'):
        with pytest.raises(ValueError):
            probe._target(url)
    monkeypatch.setattr(probe.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443)),
    ])
    with pytest.raises(PermissionError):
        probe._resolve('registered.example', 443)


def test_failed_first_ip_uses_next_pinned_ip(monkeypatch, local_http):
    monkeypatch.setattr(probe, '_resolve', lambda host, port: [
        (socket.AF_INET, ('127.0.0.1', 1), '127.0.0.1'),
        (socket.AF_INET, ('127.0.0.1', local_http), '127.0.0.1'),
    ])
    result = probe.probe_url(f'http://registered.invalid:{local_http}/')
    assert result['exit_code'] == 0
    assert result['status'] == 302
    assert len(_ProbeHandler.seen) == 1


def test_tls_failure_is_reported_without_disabling_validation(monkeypatch, local_http):
    monkeypatch.setattr(probe, '_resolve', lambda host, port: [
        (socket.AF_INET, ('127.0.0.1', local_http), '127.0.0.1')])
    class RejectingContext:
        def wrap_socket(self, sock, *, server_hostname):
            assert server_hostname == 'registered.invalid'
            raise ssl.SSLError('certificate invalid')
    monkeypatch.setattr(probe.ssl, 'create_default_context', lambda: RejectingContext())
    result = probe.probe_url(f'https://registered.invalid:{local_http}/')
    assert result['code'] == 'tls_error'
    assert result['exit_code'] == 1


def test_model_cannot_supply_url_method_or_headers(monkeypatch):
    calls = []
    monkeypatch.setattr(probe, '_registered_url', lambda owner, endpoint_id: calls.append((owner, endpoint_id)) or None)
    for content in ('{"url":"http://127.0.0.1/"}',
                    '{"endpoint_id":"e1","method":"POST"}',
                    '{"endpoint_id":"e1","headers":{"Authorization":"x"}}'):
        result = asyncio.run(probe.HttpProbeTool().execute(content, {'owner': 'alice'}))
        assert result['code'] == 'invalid_arguments'
    assert calls == []
    result = asyncio.run(probe.HttpProbeTool().execute('{"endpoint_id":"e1"}', {'owner': 'alice'}))
    assert result['code'] == 'not_found'
    assert calls == [('alice', 'e1')]


def test_registered_endpoint_lookup_is_owner_scoped(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database

    engine = create_engine('sqlite:///:memory:')
    database.ModelEndpoint.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        db.add_all([
            database.ModelEndpoint(id='alice-ep', name='Alice', base_url='http://127.0.0.1:1234/v1', owner='alice', is_enabled=True),
            database.ModelEndpoint(id='shared-ep', name='Shared', base_url='http://127.0.0.1:2345/v1', owner=None, is_enabled=True),
            database.ModelEndpoint(id='off-ep', name='Off', base_url='http://127.0.0.1:3456/v1', owner='alice', is_enabled=False),
        ])
        db.commit()
    monkeypatch.setattr(database, 'SessionLocal', session_factory)
    assert probe._registered_url('alice', 'alice-ep') == 'http://127.0.0.1:1234/v1'
    assert probe._registered_url('bob', 'alice-ep') is None
    assert probe._registered_url('bob', 'shared-ep') == 'http://127.0.0.1:2345/v1'
    assert probe._registered_url('alice', 'off-ep') is None
    engine.dispose()
