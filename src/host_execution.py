"""Explicit single-owner SSH host transport. Called only AFTER tool policy gates.

Credentials are mounted read-only files, never model arguments. The remote helper
is administrator configuration, not a tool-selected executable.
"""
import asyncio
import copy
import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

TOOLS = frozenset({'bash', 'python', 'read_file', 'write_file', 'edit_file',
                   'apply_patch', 'ls', 'glob', 'grep', 'search_files', 'list_tree', 'file_outline', 'git_status', 'git_diff', 'git_log', 'compare_files', 'verify_hashes', 'inspect_toolchain', 'run_tests', 'run_lint', 'inspect_process', 'inspect_port', 'tail_log', 'get_workspace'})
FILE_MUTATION_TOOLS = frozenset({'write_file', 'edit_file', 'apply_patch'})
TOOLS = TOOLS | {'rollback_file_checkpoint'}


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
        'read_file': 'Read a bounded window of a regular file on the Jetson HOST using an absolute path. Supports line or byte ranges, line numbers, SHA-256 and binary detection. For large UTF-8 files, returns a short preview plus an owner-scoped artifact handle. Not confined to the container workspace; Unix permissions apply. Maximum file size 2 MiB.',
        'write_file': 'Write a UTF-8 regular file on the Jetson HOST, using an absolute path. Unix permissions apply; not confined to the container workspace. Maximum 2 MiB. Existing symlink/hardlink writes are rejected.',
        'edit_file': 'Edit a unique exact string in a regular file on the Jetson HOST. Use an absolute path. Pass read_file.sha256 as expected_sha256 to reject stale content. Supported Python/JSON/JavaScript syntax is checked before the atomic replacement. Not confined to the container workspace; Unix permissions apply.',
        'apply_patch': 'Apply *** Begin Patch / *** End Patch with Add File, Update File, Delete File sections to absolute paths on the Jetson HOST. Pass full-file SHA-256 values in expected_sha256_by_path (use missing for Add File) to fence stale changes. All patch hunks and supported syntax are checked before any file is changed; a handled later commit error rolls back earlier files if their after-hashes still match. Unix permissions apply; not confined to the container workspace. No Move support.',
        'rollback_file_checkpoint': 'Roll back an Agent/Goal/Team file mutation checkpoint by exact checkpoint_id and expected_sha256 map copied from the returned file_checkpoint. Server rechecks owner, chat scope, file identity, every after-hash, active writers and original snapshot integrity; if any file changed, rollback is refused and user edits are preserved.',
        'ls': 'List a directory on the Jetson HOST. Absolute paths are allowed subject to Unix permissions; not confined to the container workspace.',
        'glob': 'Find matching file paths on the Jetson HOST with a bounded directory search. Absolute host paths are allowed; Unix permissions apply. Does not follow directory symlinks.',
        'grep': 'Search file contents on the Jetson HOST using a bounded regex search. Absolute host paths are allowed; Unix permissions apply. Does not follow directory symlinks.',
        'search_files': 'Search files on the Jetson HOST with a bounded regex. Returns a paged file list by default, or matching lines with mode=matches. Absolute host paths are allowed subject to Unix permissions; does not follow directory symlinks.',
        'list_tree': 'Show a bounded host directory hierarchy and file sizes without reading file bodies. Hidden, generated and symlink paths are excluded.',
        'file_outline': 'Return exact Python AST symbols with line numbers for a host .py/.pyi file, without returning source text. Other languages are explicitly unavailable.',
        'git_status': 'Read bounded, structured staged/unstaged/untracked Git status on the Jetson HOST. Sensitive paths are omitted; no repository mutation.',
        'git_diff': 'Read a bounded file-level Git patch and before/after blob hashes on the Jetson HOST. Requires one relative file path; no repository mutation.',
        'git_log': 'Read up to 20 recent commit hashes, parents, dates and subjects from a Git repository on the Jetson HOST.',
        'compare_files': 'Compare two regular files on the Jetson HOST; reports exact SHA-256 hashes and bounded normalized diff. Maximum 2 MiB per file; no local-container fallback.',
        'verify_hashes': 'Check up to 16 exact SHA-256 assertions for regular files on the Jetson HOST. Maximum 2 MiB per file; no local-container fallback.',
        'inspect_toolchain': 'Inspect fixed Python, Node, Git, compiler, LSP and container CLI versions on the Jetson HOST. Does not execute workspace PATH shims. Network probe is unavailable on the host route; call http_probe separately for a registered endpoint from Odysseus.',
        'run_tests': 'Run a discovered pytest or npm test profile on the Jetson HOST, with a strict deadline, bounded output and exact exit code. Executes project code and is not read-only.',
        'run_lint': 'Run a discovered npm lint profile on the Jetson HOST, with a strict deadline, bounded output and exact exit code. Executes project code and is not read-only.',
        'inspect_process': 'Inspect one process owned by the registered Jetson host user without exposing argv or environment. Returns PID and exact start ticks for later fenced diagnostics.',
        'inspect_port': 'Check whether a fenced PID owns a TCP listening socket on one port on the registered Jetson host. Requires start ticks from inspect_process.',
        'tail_log': 'Read at most 200 lines and 16 KiB from a regular log under the registered Jetson host workspace, fenced to the process PID and start ticks.',
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
    timeout = 3500 if background else 120
    if tool in {'run_tests', 'run_lint'}:
        if not isinstance(content, str) or len(content.encode('utf-8')) > 8192:
            raise ValueError('verification arguments must be JSON')
        args = json.loads(content or '{}')
        if not isinstance(args, dict) or type(args.get('timeout_seconds', 120)) is not int:
            raise ValueError('invalid verification timeout')
        timeout = min(300, max(1, args.get('timeout_seconds', 120)))
    if tool in {'inspect_process', 'inspect_port', 'tail_log'}:
        if not isinstance(content, str) or len(content.encode('utf-8')) > 8192:
            raise ValueError('diagnostic arguments must be bounded JSON')
        timeout = 10
    if tool in {'compare_files', 'verify_hashes', 'inspect_toolchain'}:
        if not isinstance(content, str) or len(content.encode('utf-8')) > 8192:
            raise ValueError('comparison arguments must be bounded JSON')
        args = json.loads(content or '{}')
        if not isinstance(args, dict):
            raise ValueError('comparison arguments must be an object')
        if tool == 'inspect_toolchain' and set(args) - {'endpoint_id'}:
            raise ValueError('unsupported toolchain arguments')
        timeout = 20
    return {'tool': tool, 'content': content,
            'cwd': os.environ.get('ODYSSEUS_HOST_CWD', '/home/xopmc'),
            'timeout': timeout}


def run_request(request):
    # The fixed remote helper bounds stdout to a single small JSON result.
    proc = subprocess.run(ssh_argv(), input=json.dumps(request), capture_output=True,
                          text=True, timeout=int(request['timeout']) + 25)
    if proc.returncode:
        return {'error': 'Jetson host reply was not verified; inspect the host before retrying',
                'code': 'unknown_outcome', 'outcome_unknown': True,
                'retryable': False, 'exit_code': 1}
    result = json.loads(proc.stdout)
    if not isinstance(result, dict) or 'exit_code' not in result:
        raise ValueError('invalid host helper response')
    result['execution_host'] = 'jetson'
    return result


def _archive_host_read(request, first, owner, session_id, run_id=None):
    if not owner or not session_id or first.get('exit_code') != 0 or not first.get('truncated'):
        return first
    try:
        args = json.loads(request['content']) if str(request['content']).lstrip().startswith('{') else {'path': request['content'].split('\n', 1)[0].strip()}
        if any(key in args for key in ('offset', 'limit', 'byte_offset', 'byte_limit')):
            return first
        path = args.get('path')
        size = first.get('size_bytes')
        digest = first.get('sha256')
        if not isinstance(path, str) or type(size) is not int or size > 2 * 1024 * 1024 or size < 0 or not isinstance(digest, str):
            return first
        pieces, offset = [], 0
        while offset < size:
            chunk_request = dict(request, tool='read_file_chunk',
                                 content=json.dumps({'path': path, 'byte_offset': offset}))
            chunk = run_request(chunk_request)
            if (chunk.get('exit_code') != 0 or chunk.get('sha256') != digest
                    or chunk.get('size_bytes') != size or chunk.get('offset') != offset):
                raise ValueError('host file changed during artifact transfer')
            data = base64.b64decode(chunk.get('data_b64', ''), validate=True)
            if not data or chunk.get('next_offset') != offset + len(data):
                raise ValueError('invalid host artifact chunk')
            pieces.append(data)
            offset += len(data)
        full_data = b''.join(pieces)
        if len(full_data) != size or hashlib.sha256(full_data).hexdigest() != digest:
            raise ValueError('host artifact hash mismatch')
        from src.observation_pack import archive
        meta = archive(owner, session_id, tool_name='read_file',
                       tool_call_id=f'{path}:{digest}', text=full_data.decode('utf-8'),
                       force=True, run_id=run_id)
        if meta:
            first['artifact_id'] = meta['id']
            first['output'] = first['output'][:2000] + (
                f"\n[Preview limited to 2000 characters. Full file: call read_tool_artifact "
                f"with id={meta['id']} and offset=0]"
            )
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, binascii.Error):
        first['artifact_unavailable'] = True
    return first


async def execute(tool, content, *, owner=None, session_id=None, run_id=None):
    # Agent/Goal host mutations use the same durable checkpoint transaction as
    # Team. Scope is the authenticated chat session; rollback remains fenced to
    # this owner + scope and the exact after-hash map returned by the mutation.
    if tool in FILE_MUTATION_TOOLS or tool == 'rollback_file_checkpoint':
        if not owner or not session_id:
            return {'error': 'Durable file checkpoints require an owner and chat scope',
                    'code': 'file_checkpoint_scope_required', 'exit_code': 1}
        try:
            if tool == 'rollback_file_checkpoint':
                args = json.loads(content) if isinstance(content, str) else content
                if (not isinstance(args, dict) or set(args) != {'checkpoint_id', 'expected_sha256'}
                        or not isinstance(args.get('checkpoint_id'), str)
                        or not isinstance(args.get('expected_sha256'), dict)):
                    raise ValueError('invalid checkpoint rollback arguments')
                if args['checkpoint_id'].startswith('local_'):
                    from src.local_file_checkpoints import rollback_scoped
                    return await rollback_scoped(owner, session_id, args['checkpoint_id'], args['expected_sha256'])
                runner_args = args
                op = 'file.rollback'
            else:
                request = request_for(tool, content)
                runner_args = {'cwd': request['cwd'], 'tool': tool, 'content': content}
                if isinstance(run_id, str) and run_id:
                    runner_args['run_id'] = run_id[:200]
                op = 'file.call'
            from src.team_host import call as runner_call
            response = await runner_call(op, runner_args, owner, session_id)
            if not response.get('ok'):
                if tool in FILE_MUTATION_TOOLS or tool == 'rollback_file_checkpoint':
                    # The runner may have committed a write or rollback before
                    # the SSH response was lost. Never invite an automatic replay.
                    return {'error': 'Host file checkpoint acknowledgement was not verified; inspect the exact file before retrying',
                            'code': 'unknown_outcome', 'outcome_unknown': True,
                            'retryable': False, 'exit_code': 1}
                return {'error': response.get('error') or 'Durable file checkpoint operation failed',
                        'code': response.get('code') or 'file_checkpoint_unavailable',
                        'exit_code': 1}
            result = response.get('result')
            if not isinstance(result, dict):
                return {'error': 'Durable file checkpoint service returned an invalid result',
                        'code': 'transport_unavailable', 'exit_code': 1}
            return result
        except (ValueError, TypeError, json.JSONDecodeError):
            return {'error': 'Invalid durable file checkpoint arguments',
                    'code': 'invalid_arguments', 'exit_code': 1}
    try:
        request = request_for(tool, content)
    except (ValueError, TypeError, json.JSONDecodeError):
        return {'error': 'Invalid host tool arguments', 'code': 'invalid_arguments', 'exit_code': 1}
    try:
        result = await asyncio.to_thread(run_request, request)
        if tool in {'run_tests', 'run_lint'}:
            full_output = result.pop('full_output', '')
            if full_output and (result.get('exit_code') != 0 or result.get('truncated')):
                try:
                    from src.observation_pack import archive
                    meta = await asyncio.to_thread(
                        archive, owner, session_id, tool_name=tool,
                        tool_call_id=str(request.get('cwd') or '') + ':' + str(result.get('profile') or ''),
                        text=full_output, force=True, run_id=run_id)
                    if meta:
                        result['artifact'] = meta
                except (OSError, ValueError):
                    result['artifact_error'] = 'Verification output could not be archived'
        if tool == 'read_file':
            result = await asyncio.to_thread(_archive_host_read, request, result, owner, session_id, run_id)
        return result
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        # A lost or invalid reply is not evidence that a write/command did not
        # happen. Never send a second copy of the exact action automatically.
        return {'error': 'Jetson host reply was not verified; inspect the host before retrying',
                'code': 'unknown_outcome', 'outcome_unknown': True,
                'retryable': False, 'exit_code': 1}


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
