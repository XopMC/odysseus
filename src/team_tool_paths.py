"""Dedicated MODEL file-tool guards; this is not a shell security sandbox.

The app can perform a lexical preflight. For symlink-safe host enforcement the
runner MUST call the same function with trusted realpath=os.path.realpath.
Never accept a resolver or protected_roots supplied by the model.
"""
import copy
import posixpath
import re

READ_TOOLS = frozenset({'read_file', 'ls', 'glob', 'grep', 'search_files', 'list_tree', 'file_outline', 'git_status', 'git_diff', 'git_log', 'compare_files', 'verify_hashes', 'inspect_toolchain', 'get_workspace'})
WRITE_TOOLS = frozenset({'write_file', 'edit_file', 'apply_patch'})
_SECRET_DIRS = frozenset({'.ssh', '.gnupg', '.aws', '.azure', '.secrets', '.credentials'})
_SECRET_FILES = frozenset({'.app_key', '.netrc', '.git-credentials', '.npmrc', '.pypirc',
                           'credentials', 'credentials.json', 'credentials.yml', 'credentials.yaml',
                           'credentials.toml', 'secrets.json', 'secrets.yaml', 'secrets.yml',
                           'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'})
_ODY_FILES = frozenset({'auth.json', 'settings.json', 'sessions.json', 'user_prefs.json',
                      'app.db', 'app.db-wal', 'app.db-shm', '.app_key'})


def _absolute(raw, cwd):
    if not isinstance(raw, str) or '\x00' in raw or '\n' in raw or '\r' in raw:
        raise PermissionError('file path must be a single-line string without NUL')
    if raw.startswith('~'):
        raise PermissionError('use an explicit absolute host path instead of ~')
    if '\\' in raw:
        raise PermissionError('host file paths must use POSIX separators')
    return posixpath.normpath(raw if raw.startswith('/') else posixpath.join(cwd, raw))


def _within(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def _credential(path, protected_roots):
    parts = [p.casefold() for p in path.split('/') if p]
    if any(part in _SECRET_DIRS for part in parts):
        return True
    if parts and (parts[-1] in _SECRET_FILES or parts[-1] == '.env' or parts[-1].startswith('.env.')):
        return True
    joined = '/' + '/'.join(parts)
    if '/.config/gcloud/' in joined or '/.kube/config' == joined[-len('/.kube/config'):]:
        return True
    if '/odysseus-host/' in joined and parts[-1] in {'id_ed25519', 'known_hosts'}:
        return True
    # Deployed Odysseus data and its backups may contain credentials in both JSON
    # and the SQLite DB. Do not infer arbitrary application settings are secrets.
    if parts and parts[-1] in _ODY_FILES:
        if any('odysseus' in part for part in parts[:-1]) or joined.startswith('/app/data/'):
            return True
    return any(_within(path, root) for root in protected_roots)


def _glob_match(path, pattern):
    # Unlike fnmatch, a single * must not cross a directory boundary.
    regex, i = [], 0
    while i < len(pattern):
        if pattern[i:i+3] == '**/':
            regex.append('(?:.*/)?')
            i += 3
        elif pattern[i:i+2] == '**':
            regex.append('.*')
            i += 2
        elif pattern[i] == '*':
            regex.append('[^/]*')
            i += 1
        elif pattern[i] == '?':
            regex.append('[^/]')
            i += 1
        else:
            regex.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch(''.join(regex), path) is not None


def normalize_file_args(tool, args, cwd, write_scope=None, realpath=None, protected_roots=()):
    """Return normalized copied args or raise PermissionError.

    None write_scope means the assigned cwd; [] means no assigned write paths.
    Scope strings are cwd-relative or absolute directory/file paths; * ? **
    patterns are supported. Reads can reach ordinary host paths outside cwd.
    protected_roots adds administrator-selected credential/state directories.
    """
    if tool not in READ_TOOLS | WRITE_TOOLS or not isinstance(args, dict):
        raise PermissionError('known dedicated file tool and argument object required')
    if not isinstance(cwd, str) or not cwd.startswith('/'):
        raise PermissionError('assigned host cwd must be absolute')
    cwd = _absolute(cwd, '/')
    canonical_cwd = _absolute(realpath(cwd), '/') if realpath else cwd
    denied_roots = [_absolute(root, '/') for root in protected_roots]
    if write_scope is not None and (not isinstance(write_scope, (list, tuple)) or not all(isinstance(x, str) and x for x in write_scope)):
        raise PermissionError('write_scope must be a list of assigned paths')
    scopes = [_absolute(item, cwd) for item in write_scope] if write_scope is not None else None

    def check(raw, writing):
        path = _absolute(raw, cwd)
        resolved = _absolute(realpath(path), '/') if realpath else path
        if _credential(path, denied_roots) or _credential(resolved, denied_roots):
            raise PermissionError('model file tools cannot access known credential/state paths')
        if writing:
            if not _within(path, cwd) or not _within(resolved, canonical_cwd):
                raise PermissionError('write is outside the assigned worker checkout')
            if scopes is not None:
                matched = False
                for pattern in scopes:
                    wildcard = '*' in pattern or '?' in pattern
                    if (_glob_match(path, pattern) if wildcard else _within(path, pattern)):
                        if realpath and wildcard:
                            canonical_pattern = canonical_cwd + pattern[len(cwd):] if _within(pattern, cwd) else pattern
                            if not _glob_match(resolved, canonical_pattern):
                                continue
                        if realpath and not wildcard:
                            scope_resolved = _absolute(realpath(pattern), '/')
                            if not _within(resolved, scope_resolved):
                                continue
                        matched = True
                        break
                if not matched:
                    raise PermissionError('write is outside the worker write_scope')
        return path

    result = copy.deepcopy(args)
    if tool == 'get_workspace':
        check(cwd, False)
        return result
    if tool == 'inspect_toolchain':
        if set(result) - {'endpoint_id'}:
            raise PermissionError('unsupported toolchain arguments')
        check(cwd, False)
        return result
    if tool == 'compare_files':
        if set(result) != {'before', 'after'}:
            raise PermissionError('two comparison paths required')
        result['before'] = check(result['before'], False)
        result['after'] = check(result['after'], False)
        return result
    if tool == 'verify_hashes':
        files = result.get('files')
        if set(result) != {'files'} or not isinstance(files, list) or not 1 <= len(files) <= 16:
            raise PermissionError('bounded hash assertions required')
        for item in files:
            if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
                raise PermissionError('invalid hash assertion')
            item['path'] = check(item['path'], False)
        return result
    if tool == 'apply_patch':
        key = next((key for key in ('patch_text', 'patchText', 'patch') if key in result), 'patch_text')
        text = result.get(key)
        if not isinstance(text, str) or not text.strip().startswith('*** Begin Patch') or not text.strip().endswith('*** End Patch'):
            raise PermissionError('valid bounded patch_text required')
        if len(text.encode()) > 2 * 1024 * 1024:
            raise PermissionError('patch exceeds model tool bound')
        expected = result.get('expected_sha256_by_path', {})
        if (not isinstance(expected, dict) or len(expected) > 32
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in expected.items())):
            raise PermissionError('expected_sha256_by_path must be a bounded path-to-hash object')
        if any(value != 'missing' and not re.fullmatch(r'[0-9a-fA-F]{64}', value)
               for value in expected.values()):
            raise PermissionError('expected_sha256_by_path values must be SHA-256 or missing')
        validate_syntax = result.get('validate_syntax', True)
        if validate_syntax is not True:
            raise PermissionError('validation_required: syntax checks cannot be disabled')
        rewritten, count, mapped_hashes = [], 0, {}
        for line in text.splitlines():
            match = re.fullmatch(r'(\*\*\* (?:Add File|Update File|Delete File|Move to): )(.+)', line)
            if match:
                count += 1
                raw_path = match.group(2).strip()
                normalized_path = check(raw_path, True)
                rewritten.append(match.group(1) + normalized_path)
                if raw_path in expected:
                    mapped_hashes[normalized_path] = expected[raw_path]
                elif normalized_path in expected:
                    mapped_hashes[normalized_path] = expected[normalized_path]
            else:
                rewritten.append(line)
        if not 1 <= count <= 32:
            raise PermissionError('patch needs 1..32 validated file paths')
        if not expected or len(mapped_hashes) != count:
            raise PermissionError('precondition_required: every patch path needs a current SHA-256 or missing marker')
        if len(mapped_hashes) != len(expected):
            raise PermissionError('hash precondition path is not part of this patch')
        result[key] = '\n'.join(rewritten) + ('\n' if text.endswith('\n') else '')
        if expected:
            result['expected_sha256_by_path'] = mapped_hashes
        result['validate_syntax'] = validate_syntax
        return result
    raw = result.get('path', '')
    if not raw and tool in {'read_file', 'write_file', 'edit_file'}:
        raise PermissionError('path required')
    result['path'] = check(raw, tool in WRITE_TOOLS)
    if tool == 'edit_file':
        expected = result.get('expected_sha256')
        if expected is None:
            raise PermissionError('precondition_required: read_file and pass its sha256 as expected_sha256')
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
            raise PermissionError('expected_sha256 must be a SHA-256 digest')
        if result.get('validate_syntax', True) is not True:
            raise PermissionError('validation_required: syntax checks cannot be disabled')
    return result
