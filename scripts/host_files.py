"""Owner-authorized host filesystem RPC. No sudo or container path policy here.

OS permissions still apply. The caller must authenticate/authorize every request.
Writes are atomic per file, not transactional across files; special files and
hard-linked writes are rejected. Search never follows directory symlinks.
"""
import difflib
import fnmatch
import json
import os
import re
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


def _path(raw, cwd):
    if not isinstance(raw, str) or '\x00' in raw:
        raise ValueError('path must be a string without NUL')
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(raw)))


def _read(path):
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
    return data.decode('utf-8'), info


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


def _search(tool, args, cwd, path_guard=None):
    root = _path(args.get('path', ''), cwd)
    pattern = args.get('pattern', '')
    if not isinstance(pattern, str) or not pattern or len(pattern) > 1000:
        raise ValueError('pattern required, at most 1000 characters')
    expression = re.compile(pattern, re.I if args.get('ignore_case') else 0) if tool == 'grep' else None
    cap = max(1, min(MAX_HITS, int(args.get('max_results') or MAX_HITS)))
    result, pending, visited, scanned = [], [root], 0, 0
    deadline = time.monotonic() + 15
    truncated = False
    while pending:
        current = pending.pop()
        if not _model_path_allowed(current, cwd, path_guard):
            continue
        if os.path.isdir(current) and not os.path.islink(current):
            with os.scandir(current) as entries:
                for entry in entries:
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
                    result.append(f'{current}:{number}:{line[:400]}')
                if len(result) >= cap:
                    break
        if len(result) >= cap:
            truncated = True
            break
    output = '\n'.join(result) or f'No matches under {root}'
    if truncated:
        output += '\n[Search truncated by time, bytes, entries, or result limit; narrow the path.]'
    return {'output': output[:MAX_OUTPUT], 'exit_code': 0, 'truncated': truncated}


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
        if tool == 'get_workspace':
            return {'output': f'{cwd}\nHost filesystem mode; Unix user permissions apply. No sudo in file RPC.', 'exit_code': 0}
        if tool in {'glob', 'grep'}:
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
        if not raw and tool != 'ls':
            raise ValueError('path required')
        path = _path(raw, cwd)
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
            text, _ = _read(path)
            offset, limit = max(1, int(args.get('offset') or 1)), max(0, int(args.get('limit') or 0))
            lines = text.splitlines(keepends=True)
            text = ''.join(lines[offset-1:offset-1+limit if limit else None])
            truncated = len(text) > MAX_OUTPUT
            return {'output': text[:MAX_OUTPUT] + ('\n[Read truncated]' if truncated else ''), 'exit_code': 0, 'truncated': truncated}
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
