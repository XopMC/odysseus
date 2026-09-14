"""Server-owned execution host registry; listing never contacts a host.

This is a transport/owner gate, not task authorization. Callers must validate
scope, operation permissions and human-only terminal input before calling.
Cancellation cannot undo an already accepted durable runner operation.
"""
import asyncio
import json
import os
import re
import shlex
import subprocess

from src import team_host
from src.host_execution import enabled_for

LEGACY = 'legacy-jetson'
MAX_BYTES = 8 * 1024 * 1024
OPS = frozenset(('runner.capabilities', 'resource.snapshot', 'scope.cancel',
    'workspace.digest', 'workspace.verification-copy',
    'lsp.discover', 'lsp.start', 'lsp.request', 'lsp.diagnostics', 'lsp.stop',
    'terminal.create', 'terminal.poll', 'terminal.input', 'terminal.resize',
    'terminal.interrupt', 'terminal.stop', 'terminal.list', 'command.start',
    'sandbox.command.start',
    'file.call', 'file.upload', 'file.download', 'file.checkpoint.list',
    'file.rollback', 'git.worktree.create', 'git.diff', 'git.integrate', 'git.rollback'))


def _plain(value, limit=4096):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _validate(item):
    fields = {'name', 'target', 'port', 'key_path', 'known_hosts_path', 'client_path'}
    if not isinstance(item, dict) or set(item) != fields:
        raise ValueError('Invalid execution host configuration')
    if not _plain(item['name'], 120) or not _plain(item['target'], 255):
        raise ValueError('Invalid execution host configuration')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*', item['target']):
        raise ValueError('Invalid execution host target')
    if type(item['port']) is not int or not 1 <= item['port'] <= 65535:
        raise ValueError('Invalid execution host port')
    for key in ('key_path', 'known_hosts_path', 'client_path'):
        path = item[key]
        if not _plain(path) or not path.startswith('/') or '..' in path.split('/'):
            raise ValueError('Invalid fixed execution host path')
    return dict(item)


def _registry(owner):
    if not enabled_for(owner):
        return {}
    raw = os.environ.get('ODYSSEUS_EXECUTION_HOSTS', '{}')
    if len(raw.encode()) > 65536:
        raise ValueError('Execution host configuration too large')
    try:
        configured = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('Invalid execution host configuration') from None
    if not isinstance(configured, dict) or len(configured) > 32:
        raise ValueError('Invalid execution host configuration')
    hosts = {}
    for identity, item in configured.items():
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', identity) or identity == LEGACY:
            raise ValueError('Invalid or reserved execution host id')
        hosts[identity] = _validate(item)
    hosts[LEGACY] = _validate({'name': 'Jetson (legacy)',
        'target': os.environ.get('ODYSSEUS_HOST_TARGET', ''), 'port': 22,
        'key_path': os.environ.get('ODYSSEUS_HOST_KEY', '/run/odysseus-host/id_ed25519'),
        'known_hosts_path': os.environ.get('ODYSSEUS_HOST_KNOWN_HOSTS', '/run/odysseus-host/known_hosts'),
        'client_path': os.environ.get('ODYSSEUS_HOST_RUNNER_CLIENT', '/home/xopmc/services/odysseus-host/host_runner_client.py')})
    return hosts


def public_hosts(owner):
    return [{'id': identity, 'name': item['name'], 'platform': 'unknown', 'status': 'configured'}
            for identity, item in _registry(owner).items()]


def _ssh_argv(host):
    host = _validate(host)
    return ['ssh', '-T', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + json.dumps(host['known_hosts_path']),
        '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
        '-p', str(host['port']), '-i', host['key_path'], '--', host['target'],
        'python3 ' + shlex.quote(host['client_path'])]


def _transport(host, payload):
    proc = subprocess.run(_ssh_argv(host), input=payload, text=True, capture_output=True, timeout=135)
    if proc.returncode:
        return {'ok': False, 'error': 'Execution host connection failed'}
    if len(proc.stdout.encode()) > MAX_BYTES:
        return {'ok': False, 'error': 'Execution host response too large'}
    return json.loads(proc.stdout)


async def call(host_id, op, args, owner, scope):
    if not enabled_for(owner):
        return {'ok': False, 'error': 'Execution host access is not enabled for this owner'}
    try:
        hosts = _registry(owner)
        if host_id not in hosts:
            return {'ok': False, 'error': 'Unknown execution host'}
        if op not in OPS or not isinstance(args, dict) or not _plain(scope, 200):
            return {'ok': False, 'error': 'Invalid runner request'}
        payload = json.dumps({'op': op, 'args': args, 'owner': owner, 'scope': scope}, allow_nan=False)
        if len(payload.encode()) > MAX_BYTES:
            return {'ok': False, 'error': 'Runner request too large'}
        if host_id == LEGACY:
            result = await team_host.call(op, args, owner, scope)
        else:
            result = await asyncio.to_thread(_transport, hosts[host_id], payload)
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            return {'ok': False, 'error': 'Invalid execution host response'}
        if not result['ok']:
            # SSH stderr and remote exception strings can contain credentials.
            failure = {'ok': False, 'error': 'Execution host operation failed'}
            if result.get('code') in {'not_git_repository', 'git_error'}:
                failure['code'] = result['code']
            return failure
        return result
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {'ok': False, 'error': 'Execution host transport or configuration failed'}
