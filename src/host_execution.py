"""Explicit single-owner SSH host transport. Called only AFTER tool policy gates.

Credentials are mounted read-only files, never model arguments. The remote helper
is administrator configuration, not a tool-selected executable.
"""
import asyncio
import copy
import base64
import json
import os
import re
import shlex
import subprocess
import sys

TOOLS = frozenset({'bash', 'python', 'read_file', 'write_file', 'edit_file',
                   'apply_patch', 'ls', 'glob', 'grep', 'get_workspace'})


def enabled_for(owner):
    expected = os.environ.get('ODYSSEUS_HOST_OWNER', '')
    return os.environ.get('ODYSSEUS_HOST_ENABLED') == '1' and bool(expected) and owner == expected


def adapt_schemas(schemas, owner):
    """Do not advertise container confinement for explicitly host-bound tools."""
    if not enabled_for(owner):
        return schemas
    result = copy.deepcopy(schemas)
    descriptions = {
        'get_workspace': 'Return the configured default directory on the Jetson HOST. This is a starting directory, NOT a filesystem boundary. File tools accept absolute host paths subject to Unix permissions.',
        'bash': 'Execute Bash on the Jetson HOST as its configured Unix user. Each call starts in the default host directory; use explicit cd or absolute paths. Foreground timeout 120 seconds; prefix a long job with #!bg on its own first line for tracked background execution. Never send a sudo password; root commands need human approval at /host-access.',
        'python': 'Execute Python on the Jetson HOST, not in Docker. Use absolute host paths. Foreground timeout 120 seconds. Never send credentials or a sudo password.',
        'read_file': 'Read a UTF-8 regular file on the Jetson HOST using an absolute path. Supports line offset/limit. Not confined to the container workspace; Unix permissions apply. Maximum file size 2 MiB.',
        'write_file': 'Write a UTF-8 regular file on the Jetson HOST, using an absolute path. Unix permissions apply; not confined to the container workspace. Maximum 2 MiB. Existing symlink/hardlink writes are rejected.',
        'edit_file': 'Edit a unique exact string in a regular file on the Jetson HOST. Use an absolute path. Not confined to the container workspace; Unix permissions apply.',
        'apply_patch': 'Apply *** Begin Patch / *** End Patch with Add File, Update File, Delete File sections to absolute paths on the Jetson HOST. Unix permissions apply; not confined to the container workspace. No Move support. Atomic per file, not across files.',
        'ls': 'List a directory on the Jetson HOST. Absolute paths are allowed subject to Unix permissions; not confined to the container workspace.',
        'glob': 'Find matching file paths on the Jetson HOST with a bounded directory search. Absolute host paths are allowed; Unix permissions apply. Does not follow directory symlinks.',
        'grep': 'Search file contents on the Jetson HOST using a bounded regex search. Absolute host paths are allowed; Unix permissions apply. Does not follow directory symlinks.',
    }
    for schema in result:
        function = schema.get('function', {})
        if function.get('name') in descriptions:
            function['description'] = descriptions[function['name']]
    return result


def ssh_argv(remote_command=None):
    target = os.environ.get('ODYSSEUS_HOST_TARGET', '')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*', target):
        raise ValueError('ODYSSEUS_HOST_TARGET must be a fixed user@host')
    key = os.environ.get('ODYSSEUS_HOST_KEY', '/run/odysseus-host/id_ed25519')
    known = os.environ.get('ODYSSEUS_HOST_KNOWN_HOSTS', '/run/odysseus-host/known_hosts')
    helper = os.environ.get('ODYSSEUS_HOST_HELPER', '/home/xopmc/services/odysseus-host/host_exec.py')
    for path in (key, known, helper):
        if not os.path.isabs(path) or '\x00' in path or '\n' in path:
            raise ValueError('host transport paths must be absolute')
    if remote_command is None:
        remote_command = 'python3 ' + shlex.quote(helper)
    return ['ssh', '-T', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + known,
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
            '-i', key, '--', target, remote_command]


def request_for(tool, content, background=False):
    if tool not in TOOLS:
        raise ValueError('unsupported host tool')
    if tool in {'bash', 'python'}:
        if not isinstance(content, str):
            raise ValueError('host shell/python content must be a string')
        if re.search(r'\bsudo\b', content):
            raise ValueError('sudo needs explicit one-shot approval at /host-access; never include passwords in chat/tool arguments')
    return {'tool': tool, 'content': content,
            'cwd': os.environ.get('ODYSSEUS_HOST_CWD', '/home/xopmc'),
            'timeout': 3500 if background else 120}


def run_request(request):
    # The fixed remote helper bounds stdout to a single small JSON result.
    proc = subprocess.run(ssh_argv(), input=json.dumps(request), capture_output=True,
                          text=True, timeout=int(request['timeout']) + 25)
    if proc.returncode:
        return {'error': 'Jetson SSH failed: ' + proc.stderr[:2000], 'exit_code': proc.returncode}
    result = json.loads(proc.stdout)
    if not isinstance(result, dict) or 'exit_code' not in result:
        raise ValueError('invalid host helper response')
    result['execution_host'] = 'jetson'
    return result


async def execute(tool, content):
    try:
        request = request_for(tool, content)
        return await asyncio.to_thread(run_request, request)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        return {'error': f'Jetson host: {exc}', 'exit_code': 1}


def background_command(tool, content):
    request = request_for(tool, content, background=True)
    encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode()
    # Only ordinary task content belongs here: no password/token fields. Local
    # bg_jobs persists this command exactly as it already persists local scripts.
    return shlex.join([sys.executable, os.path.abspath(__file__), '--background', encoded])


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--background':
    try:
        response = run_request(json.loads(base64.urlsafe_b64decode(sys.argv[2])))
        print(response.get('output') or response.get('error') or '')
        sys.exit(int(response.get('exit_code', 1)))
    except Exception as exc:
        print(f'Host background transport failed: {exc}', file=sys.stderr)
        sys.exit(1)
