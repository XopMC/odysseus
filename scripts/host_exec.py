"""Fixed SSH stdin-JSON helper, installed beside host_files.py; no sudo."""
import json
import os
from pathlib import Path
import stat
import selectors
import signal
import subprocess
import sys
import time

from host_files import handle as files_handle, toolchain as host_toolchain

MAX_OUTPUT = 60000
MAX_VERIFICATION_OUTPUT = 1024 * 1024
_DIAGNOSTIC_TOOLS = frozenset({'inspect_process', 'inspect_port', 'tail_log'})
def _process_identity(pid, proc_root='/proc'):
    if type(pid) is not int or not 1 <= pid <= 2_147_483_647:
        raise ValueError('invalid process id')
    directory = os.path.join(proc_root, str(pid))
    if os.path.islink(directory):
        raise PermissionError('process path is not canonical')
    info = os.stat(directory, follow_symlinks=False)
    if info.st_uid != os.geteuid():
        raise PermissionError('process does not belong to the registered host user')
    with open(os.path.join(directory, 'stat'), encoding='ascii') as stream:
        raw = stream.read(4096)
    marker = raw.rfind(') ')
    if marker < 0:
        raise ValueError('process identity unavailable')
    fields = raw[marker + 2:].split()
    if len(fields) < 22:
        raise ValueError('process identity unavailable')
    start_ticks = int(fields[19])
    if start_ticks <= 0:
        raise ValueError('process identity unavailable')
    return {'pid': pid, 'start_ticks': start_ticks, 'state': fields[0],
            'ppid': int(fields[1]),
            'rss_bytes': max(0, int(fields[21])) * os.sysconf('SC_PAGE_SIZE'),
            'name': raw[raw.find('(') + 1:marker][:80]}


def _fenced_process(pid, expected_start_ticks, proc_root='/proc'):
    identity = _process_identity(pid, proc_root)
    if expected_start_ticks is not None and identity['start_ticks'] != expected_start_ticks:
        raise RuntimeError('stale process identity')
    return identity


def _diagnostic(tool, content, cwd, proc_root='/proc'):
    if not os.path.isdir(proc_root):
        return {'error': 'Process diagnostics are unavailable on this host',
                'code': 'not_supported_by_route', 'exit_code': 1}
    try:
        if not isinstance(content, str) or len(content.encode('utf-8')) > 8192:
            raise ValueError('invalid diagnostic arguments')
        args = json.loads(content or '{}')
        if not isinstance(args, dict):
            raise ValueError('invalid diagnostic arguments')
        allowed = {
            'inspect_process': {'pid', 'start_ticks'},
            'inspect_port': {'pid', 'start_ticks', 'port'},
            'tail_log': {'pid', 'start_ticks', 'path', 'lines'},
        }[tool]
        if set(args) - allowed:
            raise ValueError('invalid diagnostic arguments')
        pid = args.get('pid')
        start_ticks = args.get('start_ticks')
        if start_ticks is not None and (type(start_ticks) is not int or start_ticks <= 0):
            raise ValueError('invalid process identity')
        if tool != 'inspect_process' and start_ticks is None:
            raise ValueError('process start_ticks required')
        identity = _fenced_process(pid, start_ticks, proc_root)
        if tool == 'inspect_process':
            identity['output'] = f"PID {pid} {identity['name']} state={identity['state']} rss={identity['rss_bytes']} start_ticks={identity['start_ticks']}"
            identity['exit_code'] = 0
            return identity
        if tool == 'inspect_port':
            port = args.get('port')
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('invalid port')
            inodes = set()
            for name in ('tcp', 'tcp6'):
                try:
                    # Use the target's network namespace, not the helper's.
                    with open(os.path.join(proc_root, str(pid), 'net', name), encoding='ascii') as stream:
                        for line in stream:
                            parts = line.split()
                            if len(parts) >= 10 and parts[3] == '0A':
                                try:
                                    match = int(parts[1].rsplit(':', 1)[1], 16) == port
                                except (IndexError, ValueError):
                                    match = False
                                if match:
                                    inodes.add(parts[9])
                except FileNotFoundError:
                    continue
            found = False
            inaccessible = False
            fd_dir = os.path.join(proc_root, str(pid), 'fd')
            deadline = time.monotonic() + 2
            with os.scandir(fd_dir) as entries:
                for index, fd in enumerate(entries):
                    if index >= 4096 or time.monotonic() > deadline:
                        raise TimeoutError('port inspection bound exceeded')
                    try:
                        target = os.readlink(fd.path)
                        if target.startswith('socket:[') and target.endswith(']'):
                            found |= target[8:-1] in inodes
                    except PermissionError:
                        inaccessible = True
                    except OSError:
                        continue
            if inaccessible and not found:
                raise PermissionError('process socket descriptors are not readable')
            _fenced_process(pid, start_ticks, proc_root)
            return {'pid': pid, 'start_ticks': start_ticks, 'port': port,
                    'listening': found, 'output': f"PID {pid} listening on TCP port {port}: {found}",
                    'exit_code': 0}
        raw_path = args.get('path')
        lines = args.get('lines', 50)
        if (not isinstance(raw_path, str) or not raw_path or len(raw_path) > 1024
                or '\x00' in raw_path or '\n' in raw_path or '\r' in raw_path
                or type(lines) is not int or not 1 <= lines <= 200):
            raise ValueError('invalid log arguments')
        root = os.path.realpath(cwd)
        requested_path = os.path.join(root, raw_path)
        path = os.path.realpath(requested_path)
        if os.path.commonpath((root, path)) != root or os.path.islink(requested_path):
            raise PermissionError('log is outside the registered host workspace')
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise PermissionError('log must be a regular file owned by the host user')
            size = info.st_size
            os.lseek(fd, max(0, size - 65536), os.SEEK_SET)
            data = os.read(fd, 65536)
            after = os.fstat(fd)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RuntimeError('log changed during inspection')
        finally:
            os.close(fd)
        _fenced_process(pid, start_ticks, proc_root)
        available_lines = data.decode('utf-8', 'replace').splitlines()
        selected_text = '\n'.join(available_lines[-lines:])
        text = selected_text[-16384:]
        return {'pid': pid, 'start_ticks': start_ticks, 'path': path,
                'lines': lines, 'output': text,
                'truncated': size > len(data) or len(available_lines) > lines or len(selected_text) > 16384,
                'exit_code': 0}
    except RuntimeError:
        return {'error': 'Process or log changed; inspect again', 'code': 'stale_revision', 'exit_code': 1}
    except PermissionError:
        return {'error': 'Diagnostic target is outside the registered host scope',
                'code': 'permission_denied', 'exit_code': 1}
    except FileNotFoundError:
        return {'error': 'Diagnostic target no longer exists', 'code': 'not_found', 'exit_code': 1}
    except TimeoutError:
        return {'error': 'Diagnostic inspection timed out', 'code': 'timeout', 'exit_code': 124}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {'error': 'Invalid or unavailable diagnostic target',
                'code': 'invalid_arguments', 'exit_code': 1}


def _verification_command(tool, content, default_cwd):
    if not isinstance(content, str) or len(content.encode('utf-8')) > 8192:
        raise ValueError('verification arguments must be a bounded JSON object')
    args = json.loads(content or '{}')
    if (not isinstance(args, dict) or set(args) - {'path', 'profile', 'timeout_seconds'}
            or not isinstance(args.get('path', ''), str)
            or not isinstance(args.get('profile', ''), str)
            or type(args.get('timeout_seconds', 120)) is not int
            or not 1 <= args.get('timeout_seconds', 120) <= 300):
        raise ValueError('invalid verification arguments')
    raw_path = args.get('path') or default_cwd
    if '\x00' in raw_path or '\n' in raw_path or '\r' in raw_path:
        raise ValueError('invalid verification path')
    root = os.path.realpath(os.path.join(default_cwd, raw_path))
    if not os.path.isdir(root):
        raise ValueError('verification directory unavailable')
    directory = Path(root)
    profiles = {}
    if any((directory / name).is_file() for name in ('pytest.ini', 'pyproject.toml', 'setup.cfg')) or (directory / 'tests').is_dir():
        project_python = directory / '.venv' / 'bin' / 'python'
        python = str(project_python) if project_python.is_file() and os.access(project_python, os.X_OK) else sys.executable
        profiles['pytest'] = [python, '-m', 'pytest', '-q']
    package = directory / 'package.json'
    if package.is_file() and not package.is_symlink() and package.stat().st_size <= 262144:
        try:
            scripts = json.loads(package.read_text(encoding='utf-8')).get('scripts', {})
        except (UnicodeError, ValueError, OSError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for kind in ('test', 'lint'):
                if isinstance(scripts.get(kind), str) and scripts[kind].strip():
                    profiles['npm_' + kind] = ['npm', 'run', kind]
    profile = args.get('profile') or (('pytest' if 'pytest' in profiles else 'npm_test')
                                      if tool == 'run_tests' else 'npm_lint')
    allowed = {'pytest', 'npm_test'} if tool == 'run_tests' else {'npm_lint'}
    if profile not in allowed or profile not in profiles:
        return None, root, profile, args['timeout_seconds'] if 'timeout_seconds' in args else 120, sorted(set(profiles) & allowed)
    return profiles[profile], root, profile, args.get('timeout_seconds', 120), sorted(set(profiles) & allowed)


def execute(request):
    tool, content = request.get('tool'), request.get('content', '')
    cwd = os.path.expanduser(request.get('cwd') or '~')
    if tool == 'inspect_toolchain':
        return host_toolchain(content)
    if tool in _DIAGNOSTIC_TOOLS:
        return _diagnostic(tool, content, cwd)
    verification = tool in {'run_tests', 'run_lint'}
    if not verification and tool not in {'bash', 'python'}:
        return files_handle(tool, content, cwd)
    if verification:
        try:
            command, cwd, profile, timeout, available = _verification_command(tool, content, cwd)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {'error': 'Invalid verification arguments or path', 'code': 'invalid_arguments', 'exit_code': 1}
        if command is None:
            return {'error': 'Verification profile is unavailable', 'code': 'not_found',
                    'available_profiles': available, 'exit_code': 1}
    elif not isinstance(content, str) or len(content.encode()) > 100000:
        return {'error': 'host command must be a string, at most 100000 bytes', 'exit_code': 1}
    import re
    if not verification and re.search(r'\bsudo\b', content):
        return {'error': 'sudo requires your explicit one-shot approval at /host-access. Never put passwords in chat/tool arguments.', 'exit_code': 1}
    if not verification:
        timeout = min(3600, max(1, int(request.get('timeout', 120))))
        command = ['/bin/bash', '-c', content] if tool == 'bash' else [sys.executable, '-c', content]
    proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    def stop(*_):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, stop)
    captured, size, truncated, timed_out = [], 0, False, False
    output_limit = MAX_VERIFICATION_OUTPUT if verification else MAX_OUTPUT
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    heartbeat = time.monotonic() + 1
    try:
        while selector.get_map() or proc.poll() is None:
            if time.monotonic() >= heartbeat:
                # JSON permits leading whitespace. Detect a closed SSH channel
                # promptly so killing the tracked SSH job also kills its host
                # process group, instead of leaving an orphan until timeout.
                try:
                    sys.stdout.write(' ')
                    sys.stdout.flush()
                except BrokenPipeError:
                    stop()
                    raise
                heartbeat = time.monotonic() + 1
            if time.monotonic() >= deadline:
                timed_out = True
                stop()
                break
            for key, _ in selector.select(min(.2, max(0, deadline-time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                kept = chunk[:max(0, output_limit - size)]
                if kept:
                    captured.append(kept)
                size += len(kept)
                truncated |= len(kept) != len(chunk)
        # A command can close stdout and keep running; the deadline still applies.
        try:
            proc.wait(timeout=max(.01, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            stop()
            proc.wait(timeout=5)
    finally:
        stop()  # do not leak grandchildren when the shell exits first
        proc.wait(timeout=5)
        proc.stdout.close()
        selector.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    output = b''.join(captured).decode('utf-8', errors='replace')
    if truncated:
        output += '\n[Output truncated at %d bytes]' % output_limit
    if timed_out:
        output += '\n[Host command timed out; process group terminated. Use #!bg for long jobs.]'
    result = {'output': output[:12000] if verification else output,
              'exit_code': 124 if timed_out else proc.returncode,
              'truncated': truncated or (verification and len(output) > 12000),
              'timed_out': timed_out, 'execution_host': 'jetson'}
    if verification:
        result.update(profile=profile, command=command,
                      code='timeout' if timed_out else ('failed' if proc.returncode else 'ok'),
                      full_output=output)
    return result


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise ValueError('request too large')
        result = execute(json.loads(raw))
    except Exception as exc:
        result = {'error': f'host execution: {exc}', 'exit_code': 1}
    print(json.dumps(result))
