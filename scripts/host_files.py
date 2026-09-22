"""Owner-authorized host filesystem RPC. No sudo or container path policy here.

OS permissions still apply. The caller must authenticate/authorize every request.
Writes are atomic per file, not transactional across files; special files and
hard-linked writes are rejected. Search never follows directory symlinks.
"""
import base64
import ast
import difflib
import fnmatch
import hashlib
from importlib import metadata
import heapq
import itertools
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time

MAX_FILE = 2 * 1024 * 1024
MAX_OUTPUT = 60000
MAX_ENTRIES = 10000
MAX_HITS = 200
SKIP = {'.git', 'node_modules', '.venv', '__pycache__'}
_MODEL_NORMALIZER = None
_TOOLCHAIN_PATHS = {
    'node': ('/usr/bin/node', '/usr/local/bin/node'),
    'git': ('/usr/bin/git', '/usr/local/bin/git'),
    'clang': ('/usr/bin/clang', '/usr/local/bin/clang'),
    'gcc': ('/usr/bin/gcc', '/usr/local/bin/gcc'),
    'docker': ('/usr/bin/docker', '/usr/local/bin/docker'),
    'podman': ('/usr/bin/podman', '/usr/local/bin/podman'),
    'pyright': ('/usr/bin/pyright', '/usr/local/bin/pyright'),
    'pylsp': ('/usr/bin/pylsp', '/usr/local/bin/pylsp'),
    'typescript-language-server': ('/usr/bin/typescript-language-server',
                                   '/usr/local/bin/typescript-language-server'),
}


def _fixed_version(name):
    for candidate in _TOOLCHAIN_PATHS[name]:
        path = os.path.realpath(candidate)
        if not any(os.path.commonpath((path, root)) == root
                   for root in ('/usr/bin', '/usr/local/bin', '/bin')):
            continue
        try:
            info = os.stat(path)
            if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
                continue
            process = subprocess.Popen([path, '--version'], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError:
            continue
        output = bytearray()
        deadline = time.monotonic() + 2
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError
                    chunk = os.read(process.stdout.fileno(), min(512, 1025 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > 1024:
                        raise ValueError('version output too large')
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if process.returncode:
                continue
            first = output.decode('utf-8', 'replace').splitlines()[0] if output else ''
            first = ''.join(char if 32 <= ord(char) < 127 else ' ' for char in first)[:240].strip()
            return {'status': 'available', 'version': first or 'unknown'}
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
            if process.stdout:
                process.stdout.close()
    return {'status': 'unavailable'}


def toolchain(content):
    try:
        args = json.loads(content or '{}') if not isinstance(content, dict) else content
    except (TypeError, ValueError):
        args = None
    if not isinstance(args, dict) or set(args) - {'endpoint_id'}:
        return {'error': 'Invalid toolchain arguments', 'code': 'invalid_arguments', 'exit_code': 1}
    if 'endpoint_id' in args:
        return {'error': 'Registered endpoint probes run on the Odysseus web host, not Jetson',
                'code': 'not_supported_by_route', 'exit_code': 1}
    tools = {'python': {'status': 'available', 'version': sys.version.split()[0]}}
    for name in _TOOLCHAIN_PATHS:
        tools[name] = _fixed_version(name)
    packages = {}
    for name in ('fastapi', 'sqlalchemy', 'uvicorn', 'pytest', 'playwright'):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    output = '\n'.join(f"{name}: {value.get('version', value['status'])}"
                       for name, value in tools.items())
    return {'tools': tools, 'packages': packages, 'network': {'status': 'not_checked'},
            'output': output[:5000], 'exit_code': 0}


def _path(raw, cwd):
    if not isinstance(raw, str) or '\x00' in raw:
        raise ValueError('path must be a string without NUL')
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(raw)))


def _read(path):
    data, info = _read_bytes(path)
    return data.decode('utf-8'), info


def _read_bytes(path):
    # Reject devices before opening: opening some devices itself has effects.
    # Resolve an explicitly requested symlink, then forbid a last-component
    # symlink swap and verify the opened descriptor again.
    resolved = os.path.realpath(path)
    if not stat.S_ISREG(os.stat(resolved).st_mode):
        raise ValueError('only regular files can be read')
    fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('only regular files can be read')
        if info.st_size > MAX_FILE:
            raise ValueError('file exceeds 2 MiB bound; use a bounded shell command')
        data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError('file exceeds 2 MiB bound')
    return data, info


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check(path, previous):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if previous is not None:
            raise ValueError('file disappeared during edit')
        return
    if previous is None:
        raise ValueError('file already exists')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('writes require a regular, non-symlink, single-link file')
    if _identity(info) != _identity(previous):
        raise ValueError('file changed during edit; read it again')


def _write(path, body, previous):
    data = body.encode('utf-8')
    if len(data) > MAX_FILE:
        raise ValueError('write exceeds 2 MiB bound')
    _check(path, previous)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.odysseus-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'wb') as stream:
            if previous is not None:
                # Preserve owner/group, permissions; fail rather than silently
                # changing ownership when editing another user's writable file.
                os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check(path, previous)
        if previous is None:
            os.link(temporary, path)  # O_EXCL semantics: never clobber new arrivals.
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _diff(old, new, path):
    if len(old) + len(new) > 200000:
        return {'text': '[Diff omitted for large file; read the affected range]',
                'file': os.path.basename(path), 'added': 0, 'removed': 0,
                'new_file': not old, 'truncated': True}
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                    fromfile='a/' + path, tofile='b/' + path, lineterm=''))
    return {'text': '\n'.join(lines[:400])[:MAX_OUTPUT], 'file': os.path.basename(path),
            'added': sum(x.startswith('+') and not x.startswith('+++') for x in lines),
            'removed': sum(x.startswith('-') and not x.startswith('---') for x in lines),
            'new_file': not old}


def _patch(text, cwd):
    lines = text.strip().splitlines()
    if not lines or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        raise ValueError('expected *** Begin Patch / *** End Patch')
    prepared, seen = [], set()
    i = 1
    while i < len(lines) - 1:
        match = re.fullmatch(r'\*\*\* (Add|Update|Delete) File: (.+)', lines[i])
        if not match:
            raise ValueError('unsupported patch operation (Move/rename is not supported)')
        kind, raw = match.groups()
        path = _path(raw, cwd)
        if path in seen:
            raise ValueError('duplicate patch path')
        seen.add(path)
        if len(seen) > 32:
            raise ValueError('patch exceeds 32 files')
        i += 1
        body = []
        while i < len(lines) - 1 and not lines[i].startswith('*** '):
            body.append(lines[i])
            i += 1
        if kind == 'Add':
            _check(path, None)
            if any(not line.startswith('+') for line in body):
                raise ValueError('Add lines must start with +')
            old, info = '', None
            new = ''.join(line[1:] + '\n' for line in body)
        else:
            old, info = _read(path)
            _check(path, info)
            new = old
            if kind == 'Delete':
                if body:
                    raise ValueError('Delete must not contain a body')
                new = ''
            else:
                hunks, current = [], []
                for line in body:
                    if line.startswith('@@'):
                        if current:
                            hunks.append(current)
                        current = []
                    elif line and line[0] in ' +-':
                        current.append(line)
                    else:
                        raise ValueError('invalid hunk line')
                if current:
                    hunks.append(current)
                if not hunks:
                    raise ValueError('Update needs hunks')
                for hunk in hunks:
                    before = [x[1:] for x in hunk if x[0] in ' -']
                    after = [x[1:] for x in hunk if x[0] in ' +']
                    existing = new.splitlines()
                    hits = [n for n in range(len(existing) - len(before) + 1)
                            if existing[n:n + len(before)] == before] if before else []
                    if len(hits) != 1:
                        raise ValueError('hunk context must match exactly once on full lines')
                    n = hits[0]
                    existing[n:n + len(before)] = after
                    new = '\n'.join(existing) + ('\n' if new.endswith('\n') else '')
        prepared.append((kind, path, old, new, info))
    if not prepared:
        raise ValueError('empty patch')
    # Validate all paths before any write; runtime OS errors may still leave a
    # partial patch, reported with exact completed paths instead of false success.
    changed = []
    try:
        for kind, path, old, new, info in prepared:
            _check(path, info)
            if kind == 'Delete':
                os.unlink(path)
            else:
                _write(path, new, info)
            changed.append(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f'partial patch; completed paths={changed!r}; {exc}') from exc
    diffs = [_diff(old, new, path) for _, path, old, new, _ in prepared]
    return {'output': f'Applied patch to {len(changed)} file(s)', 'exit_code': 0,
            'diff': {'file': 'patch', 'text': '\n'.join(d['text'] for d in diffs)[:MAX_OUTPUT],
                     'added': sum(d['added'] for d in diffs),
                     'removed': sum(d['removed'] for d in diffs),
                     'new_file': any(d['new_file'] for d in diffs)}}


def _model_args(tool, args, cwd, path_guard):
    """Only the trusted caller supplies path_guard; never take it from content."""
    global _MODEL_NORMALIZER
    if _MODEL_NORMALIZER is None:
        try:
            from team_tool_paths import normalize_file_args
            _MODEL_NORMALIZER = normalize_file_args
        except ModuleNotFoundError:
        # Local checkout tests/development; production deploys the same module
        # beside this fixed helper. Neither path comes from tool arguments.
            import importlib.util
            module_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src', 'team_tool_paths.py')
            if not os.path.isfile(module_path):
                raise ValueError('required model path policy module is not installed')
            spec = importlib.util.spec_from_file_location('_host_model_path_policy', module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _MODEL_NORMALIZER = module.normalize_file_args
    if not isinstance(path_guard, dict) or not isinstance(path_guard.get('cwd'), str):
        raise ValueError('model path policy requires assigned cwd')
    if os.path.realpath(cwd) != os.path.realpath(path_guard['cwd']):
        raise PermissionError('model policy cwd differs from assigned host cwd')
    return _MODEL_NORMALIZER(tool, args, path_guard['cwd'],
                               write_scope=path_guard.get('write_scope'), realpath=os.path.realpath)


def _model_path_allowed(path, cwd, path_guard):
    if path_guard is None:
        return True
    try:
        _model_args('read_file', {'path': path}, cwd, path_guard)
        return True
    except PermissionError:
        return False


def _git_small(cwd, *arguments):
    result = subprocess.run(
        ['git', '--no-optional-locks', '-C', cwd, *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
        env={**os.environ, 'GIT_PAGER': 'cat', 'GIT_OPTIONAL_LOCKS': '0'},
    )
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError('Git repository unavailable')
    return result.stdout.decode('utf-8', 'replace').strip()


def _git_bounded(cwd, arguments, maximum):
    process = subprocess.Popen(
        ['git', '--no-optional-locks', '-C', cwd, '-c', 'core.fsmonitor=false', *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, 'GIT_PAGER': 'cat', 'GIT_OPTIONAL_LOCKS': '0'},
    )
    pieces, size, truncated = [], 0, False
    deadline = time.monotonic() + 8
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired('git inspection', 8)
                chunk = os.read(process.stdout.fileno(), min(8192, maximum + 1 - size))
                if not chunk:
                    break
                pieces.append(chunk)
                size += len(chunk)
                if size > maximum:
                    truncated = True
                    break
        if truncated:
            process.kill()
        process.wait(timeout=max(.1, deadline - time.monotonic()))
        if not truncated and process.returncode:
            raise ValueError('Git inspection unavailable')
        return b''.join(pieces)[:maximum], truncated
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        process.stdout.close()


def _git_hash(cwd, *arguments):
    try:
        value = _git_small(cwd, *arguments)
    except ValueError:
        return None
    return value if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) else None


def _git_inspect(tool, args, cwd, path_guard):
    allowed = {'git_status': {'path'}, 'git_diff': {'path', 'file', 'staged'},
               'git_log': {'path', 'file', 'limit'}}[tool]
    if set(args) - allowed or not isinstance(args.get('path', ''), str):
        raise ValueError('invalid Git arguments')
    requested = _path(args.get('path') or cwd, cwd)
    if not os.path.isdir(requested) or not _model_path_allowed(requested, cwd, path_guard):
        raise ValueError('Git directory unavailable')
    root = os.path.realpath(_git_small(requested, 'rev-parse', '--show-toplevel'))
    if not _model_path_allowed(root, cwd, path_guard):
        raise PermissionError('Git repository is outside the allowed host paths')
    head = _git_hash(root, 'rev-parse', '--verify', 'HEAD')

    def safe_file(raw):
        if (not isinstance(raw, str) or not raw or len(raw) > 300 or raw.startswith('/')
                or '..' in raw.split('/') or '\\' in raw
                or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
            raise ValueError('invalid relative Git file path')
        target = os.path.join(root, raw)
        if (os.path.islink(target) or not os.path.commonpath((root, os.path.realpath(target))) == root
                or not _model_path_allowed(target, cwd, path_guard)):
            raise PermissionError('Git file is outside allowed host paths')
        return raw

    if tool == 'git_status':
        raw, truncated = _git_bounded(root, ['status', '--porcelain=v1', '-z',
                                             '--untracked-files=all'], 256 * 1024)
        records = raw.split(b'\0')
        if records and records[-1] == b'':
            records.pop()
        else:
            records = records[:-1]
            truncated = True
        files, index = [], 0
        names = {'M': 'modified', 'A': 'added', 'D': 'deleted', 'R': 'renamed',
                 'C': 'copied', 'U': 'unmerged', 'T': 'type_changed'}
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4 or record[2:3] != b' ':
                truncated = True
                continue
            x, y = chr(record[0]), chr(record[1])
            path = record[3:].decode('utf-8', 'replace')
            previous = None
            if x in 'RC' or y in 'RC':
                if index >= len(records):
                    truncated = True
                    break
                previous = records[index].decode('utf-8', 'replace')
                index += 1
            try:
                safe_file(path)
                if previous:
                    safe_file(previous)
            except (PermissionError, ValueError):
                continue
            untracked = x == '?' and y == '?'
            entry = {'path': path, 'staged': None if untracked else names.get(x),
                     'unstaged': None if untracked else names.get(y), 'untracked': untracked}
            if previous:
                entry['previous_path'] = previous
            files.append(entry)
            if len(files) >= 100:
                truncated = truncated or index < len(records)
                break
        output = '\n'.join(('untracked: ' + item['path']) if item['untracked'] else
                           f"{item['staged'] or '-'} / {item['unstaged'] or '-'}: {item['path']}"
                           for item in files)[:6000] or '(clean)'
        if truncated:
            output += '\n[Status truncated]'
        return {'repository': root, 'head': head, 'files': files, 'output': output,
                'truncated': truncated, 'exit_code': 0}
    if tool == 'git_diff':
        if type(args.get('staged', False)) is not bool:
            raise ValueError('invalid staged flag')
        relative = safe_file(args.get('file'))
        staged = args.get('staged', False)
        command = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                   '--no-color', '--unified=3']
        if staged:
            command.append('--cached')
        raw, truncated = _git_bounded(root, [*command, '--', relative], 32 * 1024)
        patch = raw.decode('utf-8', 'replace')
        before = _git_hash(root, 'rev-parse', '--verify',
                           f'HEAD:{relative}' if staged else f':{relative}')
        after = (_git_hash(root, 'rev-parse', '--verify', f':{relative}') if staged else
                 _git_hash(root, 'hash-object', '--no-filters', '--', relative)
                 if os.path.isfile(os.path.join(root, relative)) else None)
        return {'repository': root, 'head': head, 'file': relative, 'staged': staged,
                'before_hash': before, 'after_hash': after, 'patch': patch,
                'output': patch or '(no diff)', 'truncated': truncated, 'exit_code': 0}
    limit = args.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('invalid Git log limit')
    command = ['log', '--no-show-signature', f'--max-count={limit}',
               '--pretty=format:%H%x1f%P%x1f%aI%x1f%s%x1e']
    if 'file' in args:
        command.extend(('--', safe_file(args['file'])))
    if head is None:
        return {'repository': root, 'head': None, 'commits': [],
                'output': '(no commits)', 'truncated': False, 'exit_code': 0}
    raw, truncated = _git_bounded(root, command, 16 * 1024)
    commits = []
    for record in raw.decode('utf-8', 'replace').split('\x1e'):
        fields = record.strip('\r\n').split('\x1f')
        if len(fields) != 4 or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', fields[0]):
            continue
        commits.append({'hash': fields[0], 'parents': fields[1].split() if fields[1] else [],
                        'author_date': fields[2],
                        'subject': re.sub(r'[\x00-\x1f\x7f]', ' ', fields[3])[:200]})
    output = '\n'.join(item['hash'][:12] + ' ' + item['subject'] for item in commits)
    if truncated:
        output += '\n[Log truncated]'
    return {'repository': root, 'head': head, 'commits': commits,
            'output': output or '(no commits)', 'truncated': truncated, 'exit_code': 0}


def _git_ignored_paths(root, paths):
    if not paths:
        return set()
    try:
        result = subprocess.run(['git', '-C', root, 'check-ignore', '-z', '--stdin'],
                                input=('\0'.join(paths) + '\0').encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode not in (0, 1):
        return set()
    return {item for item in result.stdout.decode('utf-8', 'replace').split('\0') if item}


def _search(tool, args, cwd, path_guard=None):
    root = _path(args.get('path', ''), cwd)
    pattern = args.get('pattern', '')
    if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
        raise ValueError('pattern required, at most 1000 characters')
    search_v2 = tool == 'search_files'
    mode = args.get('mode', 'files') if search_v2 else None
    if search_v2 and mode not in ('files', 'matches'):
        raise ValueError('mode must be files or matches')
    cursor, page_size = args.get('cursor', 0), args.get('page_size', 25)
    if search_v2 and (type(cursor) is not int or not 0 <= cursor < 1000
                      or type(page_size) is not int or not 1 <= page_size <= 50):
        raise ValueError('invalid cursor or page_size')
    expression = re.compile(pattern, re.I if args.get('ignore_case') else 0) if tool in ('grep', 'search_files') else None
    cap = min(1000, cursor + page_size + 1) if search_v2 else max(1, min(MAX_HITS, int(args.get('max_results') or MAX_HITS)))
    result, pending, visited, scanned = [], [root], 0, 0
    deadline = time.monotonic() + 15
    truncated = False
    while pending:
        current = pending.pop()
        if not _model_path_allowed(current, cwd, path_guard):
            continue
        if os.path.isdir(current) and not os.path.islink(current):
            with os.scandir(current) as entries:
                remaining = max(0, MAX_ENTRIES - visited)
                children = list(itertools.islice(entries, remaining + 1))
                if len(children) > remaining:
                    truncated = True
                    children.pop()
                for entry in sorted(children, key=lambda value: value.name, reverse=True):
                    visited += 1
                    if visited > MAX_ENTRIES or time.monotonic() > deadline:
                        truncated = True
                        break
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in SKIP:
                            pending.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        pending.append(entry.path)
            if truncated:
                break
            continue
        if time.monotonic() > deadline or scanned >= 32 * MAX_FILE:
            truncated = True
            break
        relative = os.path.relpath(current, root)
        name = os.path.basename(current)
        if tool == 'glob':
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern) or (pattern.startswith('**/') and fnmatch.fnmatch(relative, pattern[3:])):
                result.append(current)
        else:
            glob = args.get('glob') or '*'
            if not (fnmatch.fnmatch(relative, glob) or fnmatch.fnmatch(name, glob)):
                continue
            try:
                text, _ = _read(current)
            except (OSError, ValueError, UnicodeError):
                continue
            scanned += len(text.encode('utf-8'))
            for number, line in enumerate(text.splitlines(), 1):
                if expression.search(line):
                    result.append(current if search_v2 and mode == 'files'
                                  else f'{current}:{number}:{line[:400]}')
                    if search_v2 and mode == 'files':
                        break
                if len(result) >= cap:
                    break
        if len(result) >= cap:
            truncated = True
            break
    if search_v2:
        selected = result[cursor:cursor + page_size]
        next_cursor = cursor + page_size if len(result) > cursor + page_size else None
        return {'output': ('\n'.join(selected) or f'No matches under {root}')[:MAX_OUTPUT],
                'exit_code': 0, 'mode': mode,
                'files' if mode == 'files' else 'matches': selected,
                'next_cursor': next_cursor, 'result_limit': 1000,
                'truncated': truncated or len(result) >= 1000}
    output = '\n'.join(result) or f'No matches under {root}'
    if truncated:
        output += '\n[Search truncated by time, bytes, entries, or result limit; narrow the path.]'
    return {'output': output[:MAX_OUTPUT], 'exit_code': 0, 'truncated': truncated}


def _comparison_path(raw, cwd):
    if (not isinstance(raw, str) or not raw or len(raw) > 500
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise ValueError('invalid comparison path')
    return _path(raw, cwd)


def _comparison_bytes(raw, cwd):
    path = _comparison_path(raw, cwd)
    data, before = _read_bytes(path)
    after = os.stat(os.path.realpath(path))
    if _identity(before) != _identity(after):
        raise ValueError('file changed while being compared')
    return data


def _compare_files(args, cwd):
    if set(args) != {'before', 'after'}:
        raise ValueError('two comparison paths required')
    left = _comparison_bytes(args['before'], cwd)
    right = _comparison_bytes(args['after'], cwd)
    identical = left == right
    binary = b'\x00' in left or b'\x00' in right
    normalize = lambda value: value.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
    normalized_equal = None if binary else normalize(left) == normalize(right)
    diff, truncated = '', False
    if not binary and not identical and not normalized_equal:
        before_lines = normalize(left[:64 * 1024]).decode('utf-8', 'replace').splitlines(keepends=True)
        after_lines = normalize(right[:64 * 1024]).decode('utf-8', 'replace').splitlines(keepends=True)
        truncated = len(left) > 64 * 1024 or len(right) > 64 * 1024
        chunks, used = [], 0
        for line in difflib.unified_diff(before_lines[:2000], after_lines[:2000],
                                         fromfile='before', tofile='after', lineterm='\n'):
            if used + len(line) > 16000:
                truncated = True
                break
            chunks.append(line)
            used += len(line)
        diff = ''.join(chunks)
    if binary and not identical:
        truncated = True
    return {'before_sha256': hashlib.sha256(left).hexdigest(),
            'after_sha256': hashlib.sha256(right).hexdigest(),
            'before_size_bytes': len(left), 'after_size_bytes': len(right),
            'identical': identical, 'normalized_equal': normalized_equal,
            'binary': binary, 'diff': diff, 'diff_truncated': truncated,
            'output': diff or ('Exact bytes match' if identical else
                               'Hashes differ; text diff is empty or unavailable'),
            'exit_code': 0}


def _verify_hashes(args, cwd):
    files = args.get('files')
    if set(args) != {'files'} or not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise ValueError('bounded hash assertions required')
    results, total = [], 0
    for item in files:
        if (not isinstance(item, dict) or set(item) != {'path', 'sha256'}
                or not isinstance(item['sha256'], str)
                or not re.fullmatch('[0-9a-fA-F]{64}', item['sha256'])):
            raise ValueError('invalid hash assertion')
        data = _comparison_bytes(item['path'], cwd)
        total += len(data)
        expected = item['sha256'].lower()
        actual = hashlib.sha256(data).hexdigest()
        results.append({'path': item['path'], 'expected_sha256': expected,
                        'actual_sha256': actual, 'size_bytes': len(data),
                        'matches': actual == expected})
    verified = all(item['matches'] for item in results)
    return {'files': results, 'verified': verified, 'total_bytes': total,
            'output': 'All hashes match' if verified else 'Hash assertion failed',
            'code': 'ok' if verified else 'hash_mismatch',
            'exit_code': 0 if verified else 1}


def handle(tool: str, content, cwd: str, path_guard=None) -> dict:
    try:
        if isinstance(content, dict):
            args = content
        elif isinstance(content, str):
            if len(content.encode('utf-8')) > 3 * MAX_FILE:
                raise ValueError('request too large')
            if content.strip().startswith('{'):
                args = json.loads(content)
            elif tool == 'apply_patch':
                args = {'patch_text': content}
            elif tool in {'glob', 'grep'}:
                args = {'pattern': content.strip()}
            else:
                pieces = content.split('\n', 1)
                args = {'path': pieces[0].strip(), 'content': pieces[1] if len(pieces) > 1 else ''}
        else:
            raise ValueError('arguments must be an object or string')
        if not isinstance(args, dict):
            raise ValueError('arguments must be an object')
        cwd = _path(cwd, os.getcwd())
        if path_guard is not None:
            args = _model_args(tool, args, cwd, path_guard)
        if tool == 'inspect_toolchain':
            return toolchain(args)
        if tool == 'get_workspace':
            return {'output': f'{cwd}\nHost filesystem mode; Unix user permissions apply. No sudo in file RPC.', 'exit_code': 0}
        if tool in {'git_status', 'git_diff', 'git_log'}:
            return _git_inspect(tool, args, cwd, path_guard)
        if tool in {'compare_files', 'verify_hashes'}:
            try:
                return (_compare_files(args, cwd) if tool == 'compare_files'
                        else _verify_hashes(args, cwd))
            except (OSError, ValueError, TypeError, UnicodeError):
                return {'error': 'Comparison unavailable or path is not allowed',
                        'code': 'invalid_arguments', 'exit_code': 1}
        if tool in {'glob', 'grep', 'search_files'}:
            # Regex can have pathological runtime. Isolate and enforce a hard
            # process deadline even when called from a threaded host server.
            proc = subprocess.run([sys.executable, os.path.abspath(__file__), '--search'],
                                  input=json.dumps([tool, args, cwd, path_guard]), capture_output=True,
                                  text=True, timeout=20)
            if proc.returncode:
                raise ValueError('search worker failed: ' + proc.stderr[:1000])
            return json.loads(proc.stdout)
        if tool == 'apply_patch':
            return _patch(args.get('patch_text') or args.get('patchText') or args.get('patch') or '', cwd)
        raw = args.get('path', '')
        if not raw and tool not in {'ls', 'list_tree'}:
            raise ValueError('path required')
        path = _path(raw, cwd)
        if tool == 'list_tree':
            depth, limit = args.get('max_depth', 2), args.get('max_entries', 100)
            if type(depth) is not int or not 1 <= depth <= 6 or type(limit) is not int or not 1 <= limit <= 200:
                raise ValueError('invalid tree bounds')
            if not os.path.isdir(path):
                raise ValueError('path is not a directory')
            rows, pending, truncated = [], [(path, 0)], False
            while pending and len(rows) < limit:
                current, level = pending.pop()
                if level >= depth:
                    continue
                if not _model_path_allowed(current, cwd, path_guard):
                    continue
                with os.scandir(current) as entries:
                    candidates = []
                    for index, entry in enumerate(entries):
                        if index >= MAX_ENTRIES:
                            truncated = True
                            break
                        if (entry.name.startswith('.') or entry.name in SKIP
                                or entry.is_symlink() or not _model_path_allowed(entry.path, cwd, path_guard)):
                            continue
                        candidates.append(entry)
                    ignored = _git_ignored_paths(path, [entry.path for entry in candidates])
                    ordered = heapq.nsmallest(limit + 1,
                                              (entry for entry in candidates if entry.path not in ignored),
                                              key=lambda entry: entry.name.casefold())
                if len(ordered) > limit:
                    truncated = True
                descend = []
                for entry in ordered[:limit]:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if not is_dir and not stat.S_ISREG(info.st_mode):
                        continue
                    relative = os.path.relpath(entry.path, path).replace(os.sep, '/')
                    rows.append({'path': relative, 'kind': 'directory' if is_dir else 'file',
                                 'size_bytes': 0 if is_dir else info.st_size})
                    if is_dir:
                        descend.append((entry.path, level + 1))
                    if len(rows) >= limit:
                        truncated = True
                        break
                pending.extend(reversed(descend))
            if pending:
                truncated = True
            output = '\n'.join(row['path'] + ('/' if row['kind'] == 'directory' else f" ({row['size_bytes']} B)") for row in rows)
            return {'output': (output or '(empty)')[:MAX_OUTPUT], 'entries': rows,
                    'truncated': truncated, 'exit_code': 0}
        if tool == 'file_outline':
            maximum = args.get('max_symbols', 100)
            if type(maximum) is not int or not 1 <= maximum <= 200:
                raise ValueError('invalid outline bound')
            if not path.endswith(('.py', '.pyi')):
                return {'error': 'file_outline: unavailable for this file type',
                        'code': 'unsupported_language', 'exit_code': 1}
            raw_bytes, _ = _read_bytes(path)
            try:
                tree = ast.parse(raw_bytes.decode('utf-8'), filename=path)
            except (SyntaxError, UnicodeError):
                return {'error': 'file_outline: unable to parse requested Python file', 'exit_code': 1}
            symbols = []
            def visit(body, prefix=''):
                for node in body:
                    if isinstance(node, ast.ClassDef):
                        name = prefix + node.name
                        symbols.append({'kind': 'class', 'name': name,
                                        'line': node.lineno, 'end_line': node.end_lineno})
                        visit(node.body, name + '.')
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append({'kind': 'method' if prefix else (
                            'async_function' if isinstance(node, ast.AsyncFunctionDef) else 'function'),
                            'name': prefix + node.name, 'line': node.lineno,
                            'end_line': node.end_lineno})
            visit(tree.body)
            selected = symbols[:maximum]
            output = '\n'.join(f"{item['line']}: {item['kind']} {item['name']}" for item in selected)
            return {'output': (output or '(no symbols)')[:MAX_OUTPUT], 'symbols': selected,
                    'truncated': len(symbols) > maximum, 'parser': 'python_ast', 'exit_code': 0}
        if tool == 'read_file_chunk':
            # Private transport operation, not a model-advertised tool.  The
            # server uses it to archive a bounded host file outside the prompt.
            offset = args.get('byte_offset', 0)
            if type(offset) is not int or offset < 0:
                raise ValueError('invalid artifact byte offset')
            raw_bytes, _ = _read_bytes(path)
            start = min(offset, len(raw_bytes))
            end = min(start + 262_144, len(raw_bytes))
            return {'exit_code': 0, 'data_b64': base64.b64encode(raw_bytes[start:end]).decode('ascii'),
                    'sha256': hashlib.sha256(raw_bytes).hexdigest(),
                    'size_bytes': len(raw_bytes), 'offset': start, 'next_offset': end}
        if tool == 'ls':
            rows = []
            with os.scandir(path) as entries:
                for entry in entries:
                    if not _model_path_allowed(entry.path, cwd, path_guard):
                        continue
                    if len(rows) >= MAX_HITS:
                        rows.append('[Listing truncated at 200 entries]')
                        break
                    info = entry.stat(follow_symlinks=False)
                    suffix = '/' if stat.S_ISDIR(info.st_mode) else '@' if stat.S_ISLNK(info.st_mode) else f' ({info.st_size} B)'
                    rows.append(entry.name + suffix)
            return {'output': (path + ':\n' + '\n'.join(sorted(rows)))[:MAX_OUTPUT], 'exit_code': 0}
        if tool == 'read_file':
            raw, _ = _read_bytes(path)
            offset, limit = args.get('offset'), args.get('limit')
            byte_offset, byte_limit = args.get('byte_offset'), args.get('byte_limit')
            numbered = args.get('line_numbers', False)
            if (any(type(value) is not int or value < 0 for value in (offset, limit) if value is not None)
                    or any(type(value) is not int or value < 0 for value in (byte_offset, byte_limit) if value is not None)
                    or (byte_limit is not None and byte_limit < 1)
                    or type(numbered) is not bool
                    or ((byte_offset is not None or byte_limit is not None) and (offset or limit or numbered))):
                raise ValueError('invalid or conflicting read range arguments')
            binary = b'\x00' in raw[:8192]
            byte_range = None
            if byte_offset is not None or byte_limit is not None:
                start = min(byte_offset or 0, len(raw))
                end = min(len(raw), start + min(byte_limit or MAX_OUTPUT, MAX_OUTPUT))
                selected = raw[start:end]
                byte_range = [start, end]
                truncated = end < len(raw)
                budget_truncated = (byte_limit or MAX_OUTPUT) > MAX_OUTPUT and truncated
            else:
                start = max(1, offset or 1)
                lines = raw.splitlines(keepends=True)
                selected_lines = lines[start-1:start-1+limit if limit else None]
                if numbered:
                    selected_lines = [f'{start+i}: '.encode() + line for i, line in enumerate(selected_lines)]
                selected = b''.join(selected_lines)
                truncated = len(selected) > MAX_OUTPUT
                budget_truncated = truncated
                selected = selected[:MAX_OUTPUT]
            binary = binary or b'\x00' in selected
            try:
                output = selected.decode('utf-8')
            except UnicodeDecodeError:
                binary, output = True, ''
            if binary:
                output = f'[Binary file: {len(raw)} bytes; content omitted]'
            elif budget_truncated:
                output += '\n[Read truncated]'
            result = {'output': output, 'exit_code': 0, 'truncated': truncated,
                      'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw),
                      'encoding': 'binary' if binary else 'utf-8', 'is_binary': binary}
            if byte_range is not None:
                result['byte_range'] = byte_range
            return result
        if tool not in {'write_file', 'edit_file'}:
            raise ValueError('unsupported filesystem tool')
        try:
            old, info = _read(path)
        except FileNotFoundError:
            if tool == 'edit_file':
                raise
            old, info = '', None
        if tool == 'write_file':
            if 'content' not in args or not isinstance(args['content'], str):
                raise ValueError('content string required')
            new = args['content']
        else:
            before, after = args.get('old_string'), args.get('new_string')
            if not isinstance(before, str) or not before or not isinstance(after, str) or before == after:
                raise ValueError('distinct old_string and new_string required')
            count = old.count(before)
            if not count or (count != 1 and args.get('replace_all') is not True):
                raise ValueError(f'old_string matched {count} times; use unique context or replace_all=true')
            new = old.replace(before, after)
        _write(path, new, info)
        return {'output': f'Wrote {path}', 'exit_code': 0, 'diff': _diff(old, new, path)}
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return {'error': f'{tool}: {exc}', 'exit_code': 1}


if __name__ == '__main__' and sys.argv[1:] == ['--search']:
    try:
        response = _search(*json.loads(sys.stdin.read(3 * MAX_FILE)))
    except (OSError, ValueError, TypeError) as exc:
        response = {'error': str(exc), 'exit_code': 1}
    print(json.dumps(response))
