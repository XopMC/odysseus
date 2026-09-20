"""User-level durable host runner. Unix-socket authentication is the OS UID.

The application must authorize owner/scope before forwarding. Worktrees are
ownership leases, NOT sandboxes. No commands or terminal input are persisted.
Use systemd KillMode=control-group: restart interrupts work, never replays it.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import selectors
import shutil
import signal
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
from urllib.parse import urlsplit, unquote

# Also works for importlib-based callers whose sys.path omits this directory.
import importlib.util
_platform_spec = importlib.util.spec_from_file_location('odysseus_runner_platform', Path(__file__).with_name('runner_platform.py'))
runner_platform = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(runner_platform)

MAX_REQUEST = 8 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
MAX_ACTIVE_JOBS = 16
MAX_OUTPUT_JOBS = 128
MAX_VERIFICATION_COPIES = 128
MAX_FILE_CHECKPOINT_BYTES = 128 * 1024 * 1024
MAX_FILE_CHECKPOINTS = 2048
# This is deliberately a server-owned immutable image, not a request argument.
# Updating it is an operator release action and requires a new runner build.
ISOLATED_IMAGE = 'alpine@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11'


class RunnerError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def atomic_json(path, value):
    temporary = str(path) + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Runner:
    def __init__(self, state):
        self.state = Path(state).expanduser().resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        self.lock = threading.RLock()
        self.processes = {}
        self.stop_event = threading.Event()
        self.cpu_previous = None
        self.lsp_sessions = {}
        self.lsp_module = None
        metadata = self.state / 'metadata.json'
        self.data = json.loads(metadata.read_text()) if metadata.exists() else {'jobs': {}, 'worktrees': {}, 'checkpoints': {}}
        self.data.setdefault('cancelled_scopes', {})
        self.data.setdefault('file_checkpoints', {})
        self.platform_identity = runner_platform.identity()
        previous_boot = self.data.get('platform', {}).get('boot_id')
        current_boot = self.platform_identity['boot_id']
        rebooted = bool(previous_boot and current_boot and previous_boot != current_boot)
        for record in self.data['jobs'].values():
            if record['status'] == 'running':
                record.update(status='interrupted', exit_code=None, ended_at=time.time(),
                              reason='host rebooted; not replayed' if rebooted else 'runner restarted; not replayed')
        for record in self.data['worktrees'].values():
            if record.get('kind') == 'verification-copy' and record['status'] == 'preparing':
                record.update(status='failed', reason='preparation interrupted; not replayed')
        self.data['platform'] = self.platform_identity
        self.save()

    def save(self):
        atomic_json(self.state / 'metadata.json', self.data)

    def record(self, category, identity, owner, scope):
        record = self.data[category].get(identity)
        if not record or record['owner'] != owner or record['scope'] != scope:
            raise ValueError('not found in this owner/scope')
        return record

    def safe_cwd(self, raw):
        path = Path(raw or str(Path.home())).expanduser().resolve()
        if not path.is_dir():
            raise ValueError('cwd must be an existing host directory')
        return str(path)

    def lease(self, path, owner, scope, parent_scope=None):
        path = Path(path).expanduser().resolve()
        for record in self.data['worktrees'].values():
            root = Path(record['path'])
            if path == root or root in path.parents:
                if record['owner'] != owner or record['scope'] not in {scope, parent_scope}:
                    raise ValueError('managed worktree belongs to another owner/scope')
                return record['id']
        return None

    def busy(self, path):
        root = Path(path).resolve()
        return any(r['status'] == 'running' and (Path(r['cwd']) == root or root in Path(r['cwd']).parents)
                   for r in self.data['jobs'].values())

    def prune_outputs(self, reserve=0):
        """Discard old completed output, never the durable idempotency claim."""
        retained = [record for record in self.data['jobs'].values()
                    if not record.get('output_pruned') and (self.state / (record['id'] + '.output')).exists()]
        completed = sorted((record for record in retained if record['status'] != 'running'),
                           key=lambda record: record.get('ended_at', record.get('created_at', 0)))
        while len(retained) + reserve > MAX_OUTPUT_JOBS and completed:
            record = completed.pop(0)
            (self.state / (record['id'] + '.output')).unlink(missing_ok=True)
            record.update(output_pruned=True, output_start=record['output_end'])
            retained.remove(record)
        self.save()

    def _create(self, op, args, owner, scope):
        if digest([owner, scope]) in self.data['cancelled_scopes']:
            raise ValueError('scope has been cancelled; new jobs are forbidden')
        key = args.get('idempotency_key')
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError('idempotency_key required (1..200 characters)')
        isolated = op == 'sandbox.command.start'
        # The last two fields are authenticated check-evidence bindings created
        # by the server.  They are metadata, never Docker options or host paths.
        if isolated and set(args) - {'cwd', 'command', 'timeout', 'idempotency_key',
                                    'expected_workspace_hash', 'check_run_id',
                                    'sealed_environment', 'expected_environment_hash'}:
            raise ValueError('isolated command accepts only fixed check arguments')
        cwd = self.safe_cwd(args.get('cwd'))
        lease = self.lease(cwd, owner, scope)
        if lease and self.data['worktrees'][lease].get('kind') == 'verification-copy' and self.data['worktrees'][lease]['status'] != 'ready':
            raise ValueError('verification copy is not ready; inspect retained files')
        signature = digest([op, args])
        for record in self.data['jobs'].values():
            if (record['owner'], record['scope'], record['key']) == (owner, scope, key):
                if record['signature'] != signature:
                    raise ValueError('idempotency key already used with different arguments')
                return self.public_job(record)
        if sum(record['status'] == 'running' for record in self.data['jobs'].values()) >= MAX_ACTIVE_JOBS:
            raise ValueError('active job limit reached (16); finish or stop another job')
        self.prune_outputs(reserve=1)
        if lease and self.busy(self.data['worktrees'][lease]['path']):
            raise ValueError('worktree already has an active job; poll or stop it before another writer')
        command = args.get('command', '')
        terminal = op == 'terminal.create'
        if not terminal and (not isinstance(command, str) or not command or len(command.encode()) > 100000):
            raise ValueError('command string required, maximum 100000 bytes')
        if not terminal and re.search(r'\bsudo\b', command):
            raise ValueError('sudo requires explicit one-shot approval at /host-access; never include passwords in task commands')
        if isolated:
            if self.platform_identity.get('os') != 'linux':
                raise ValueError('isolated execution is currently supported only on Linux runners')
            if not lease or self.data['worktrees'][lease].get('kind') != 'verification-copy':
                raise ValueError('isolated execution requires a ready verification copy, never a user workspace')
            if not shutil.which('docker'):
                raise ValueError('isolated execution is unavailable: docker is not installed for this runner')
            sealed_raw = args.get('sealed_environment')
            sealed_hash = args.get('expected_environment_hash')
            if (not isinstance(sealed_raw, str) or not sealed_raw.startswith('/')
                    or not isinstance(sealed_hash, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', sealed_hash)):
                raise ValueError('isolated execution requires a sealed environment and its SHA-256')
            sealed_environment = self.safe_cwd(sealed_raw)
            if sealed_environment == cwd or Path(sealed_environment) in Path(cwd).parents or Path(cwd) in Path(sealed_environment).parents:
                raise ValueError('sealed environment must be separate from the candidate workspace')
            if self.workspace_digest(sealed_environment, owner, scope)['sha256'] != sealed_hash:
                raise ValueError('sealed environment changed before check dispatch')
        timeout = min(86400, max(1, int(args.get('timeout', 86400 if terminal else 3600))))
        if lease and self.data['worktrees'][lease].get('kind') == 'verification-copy':
            verification = self.data['worktrees'][lease]
            if self.workspace_digest(verification['source'], owner, scope)['sha256'] != verification['source_sha256']:
                raise ValueError('source changed after verification copy; command not started')
        check_evidence = None
        if 'expected_workspace_hash' in args:
            if terminal:
                raise ValueError('workspace check evidence requires noninteractive command')
            before = self.workspace_digest(cwd, owner, scope)['sha256']
            if before != args['expected_workspace_hash']:
                raise ValueError('workspace changed before check dispatch')
            toolchain = '/bin/bash sha256:' + hashlib.sha256(Path('/bin/bash').read_bytes()).hexdigest()
            if isolated:
                toolchain = 'docker image:' + ISOLATED_IMAGE + ' shell:/bin/sh'
            check_evidence = {'workspace_hash': before, 'workspace_hash_after': None,
                              'command_hash': hashlib.sha256(command.encode()).hexdigest(),
                              'toolchain': toolchain,
                              'protocol': 1}
            if isolated:
                check_evidence['environment_hash'] = sealed_hash
            if 'check_run_id' in args:
                if not isinstance(args['check_run_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', args['check_run_id']):
                    raise ValueError('Invalid check run identity')
                check_evidence['run_id'] = args['check_run_id']
        identity = uuid.uuid4().hex
        record = {'id': identity, 'owner': owner, 'scope': scope, 'key': key,
                  'boot_id': self.platform_identity['boot_id'],
                  'signature': signature, 'kind': op, 'cwd': cwd, 'created_at': time.time(),
                  'status': 'running', 'exit_code': None, 'output_start': 0, 'output_end': 0,
                  'timeout': timeout}
        if isolated:
            record['isolation'] = {'image': ISOLATED_IMAGE, 'network': 'none',
                                   'host_mounts': [{'target': '/workspace', 'mode': 'rw'},
                                                   {'target': '/heldout', 'mode': 'ro'}],
                                   'read_only_root': True, 'capabilities_dropped': True,
                                   'no_new_privileges': True, 'pids_limit': 64, 'memory_limit': '512m'}
            record['sealed_environment'] = sealed_environment
        if check_evidence is not None:
            record['check_evidence'] = check_evidence
        # Persist the claim before process creation: a daemon crash cannot replay
        # a command whose acknowledgement was lost to the caller.
        self.data['jobs'][identity] = record
        self.save()
        master = slave = None
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith('ODYSSEUS_')}
            if lease and self.data['worktrees'][lease].get('kind') == 'verification-copy':
                env = {k: v for k, v in env.items() if not k.startswith('GIT_')}
                env['GIT_CEILING_DIRECTORIES'] = str(Path(self.data['worktrees'][lease]['path']).parent)
            env['TERM'] = 'xterm-256color'
            if terminal:
                master, slave = pty.openpty()
                os.set_blocking(master, False)
                self._resize(master, args)
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--pty-child'],
                                        stdin=slave, stdout=slave, stderr=slave,
                                        cwd=cwd, env=env, start_new_session=True)
                os.close(slave)
                slave = None
                fd = master
            else:
                argv = ['/bin/bash', '-c', command]
                if isolated:
                    # The request controls only the shell program *inside* this
                    # fixed container. It cannot inject flags, mounts, image, or
                    # daemon access into the host Docker invocation.
                    argv = [shutil.which('docker'), 'run', '--rm', '--network', 'none', '--read-only',
                            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64',
                            '--memory', '512m', '--user', f'{os.getuid()}:{os.getgid()}',
                            '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
                            '--volume', f'{cwd}:/workspace:rw',
                            '--volume', f'{sealed_environment}:/heldout:ro', '--workdir', '/workspace',
                            ISOLATED_IMAGE, '/bin/sh', '-ceu', command]
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        cwd=cwd, env=env, start_new_session=True)
                fd = proc.stdout.fileno()
            self.processes[identity] = (proc, fd)
            (self.state / (identity + '.output')).touch(mode=0o600)
            threading.Thread(target=self._monitor, args=(identity,), daemon=True).start()
        except Exception:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)
            record.update(status='failed', ended_at=time.time(), reason='process creation failed')
            self.save()
            raise
        return self.public_job(record)

    @staticmethod
    def _resize(fd, args):
        rows, cols = int(args.get('rows', 30)), int(args.get('cols', 100))
        if not 1 <= rows <= 500 or not 1 <= cols <= 1000:
            raise ValueError('invalid terminal size')
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    @staticmethod
    def _kill(proc, sig=signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            # The child may have exited/reaped between ``poll`` and the
            # group-kill (or the test/runner may run under a different UID).
            # Treat that as already stopped; the monitor still records the
            # terminal state and removes the process entry.
            pass

    def _monitor(self, identity):
        proc, fd = self.processes[identity]
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + self.data['jobs'][identity]['timeout']
        timed_out = False
        try:
            while selector.get_map() or proc.poll() is None:
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    timed_out = not self.stop_event.is_set()
                    self._kill(proc)
                    break
                for key, _ in selector.select(.1):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError:
                        chunk = b''  # PTY returns EIO on shell exit.
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    with self.lock:
                        record = self.data['jobs'][identity]
                        path = self.state / (identity + '.output')
                        previous = path.read_bytes()
                        retained = (previous + chunk)[-MAX_OUTPUT:]
                        path.write_bytes(retained)
                        record['output_end'] += len(chunk)
                        record['output_start'] = record['output_end'] - len(retained)
                        self.save()
            proc.wait(timeout=5)
        finally:
            self._kill(proc)
            proc.wait(timeout=5)
            selector.close()
            if proc.stdout:
                proc.stdout.close()
            else:
                os.close(fd)
            with self.lock:
                record = self.data['jobs'][identity]
                record.update(status='interrupted' if self.stop_event.is_set() else 'timed_out' if timed_out else 'exited',
                              exit_code=124 if timed_out else proc.returncode, ended_at=time.time())
                self.processes.pop(identity, None)
                if record.get('check_evidence'):
                    try:
                        record['check_evidence']['workspace_hash_after'] = self.workspace_digest(
                            record['cwd'], record['owner'], record['scope'])['sha256']
                        if record.get('sealed_environment'):
                            record['check_evidence']['environment_hash_after'] = self.workspace_digest(
                                record['sealed_environment'], record['owner'], record['scope'])['sha256']
                    except (OSError, ValueError):
                        record['check_evidence']['digest_unavailable'] = True
                self.save()

    @staticmethod
    def public_job(record):
        return {key: value for key, value in record.items()
                if key not in {'key', 'signature', 'owner', 'scope', 'sealed_environment'}}

    def workspace_digest(self, cwd, owner, scope, *, _copy_to=None):
        """Bounded source-tree digest; no Git hooks or project code executed.

        Only VCS metadata and Python test/interpreter caches are excluded.
        Other generated artifacts remain part of the digest: a modifying check
        is stale, not silently accepted. Refuse links/devices and racing trees.
        This detects changes, not an isolation boundary against external writers.
        """
        root = Path(self.safe_cwd(cwd))
        self.lease(root, owner, scope)
        if self.busy(root):
            raise ValueError('workspace has an active runner job')
        excluded = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
        hashed = hashlib.sha256(b'odysseus-source-v1\0')
        observed, total, count = [], 0, 0
        directory_modes = []
        def identity(st):
            return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        def traversal_error(error):
            raise error
        for current, dirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
            dirs[:] = sorted(name for name in dirs if name not in excluded)
            path = Path(current)
            observed.append((path, identity(path.lstat())))
            for name in sorted(dirs + [name for name in files if name not in excluded]):
                path = Path(current) / name
                before = path.lstat()
                if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
                    raise ValueError('workspace digest refuses symlinks and special files')
                count += 1
                total += before.st_size if stat.S_ISREG(before.st_mode) else 0
                if count > 10000 or total > 128 * 1024 * 1024:
                    raise ValueError('workspace digest exceeds 10000 entries or 128 MiB')
                hashed.update(json.dumps([str(path.relative_to(root)), stat.S_IMODE(before.st_mode),
                                          'file' if stat.S_ISREG(before.st_mode) else 'dir',
                                          before.st_size if stat.S_ISREG(before.st_mode) else 0]).encode() + b'\0')
                destination = Path(_copy_to) / path.relative_to(root) if _copy_to is not None else None
                if destination is not None and stat.S_ISDIR(before.st_mode):
                    destination.mkdir(mode=0o700)
                    directory_modes.append((destination, stat.S_IMODE(before.st_mode)))
                if stat.S_ISREG(before.st_mode):
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as stream:
                        if identity(os.fstat(stream.fileno())) != identity(before):
                            raise ValueError('workspace changed during digest')
                        output = destination.open('xb') if destination is not None else None
                        try:
                            remaining = before.st_size
                            while remaining:
                                chunk = stream.read(min(65536, remaining))
                                if not chunk:
                                    raise ValueError('workspace changed during digest')
                                hashed.update(chunk)
                                if output is not None:
                                    output.write(chunk)
                                remaining -= len(chunk)
                            if identity(os.fstat(stream.fileno())) != identity(before):
                                raise ValueError('workspace changed during digest')
                            if output is not None:
                                output.flush()
                                os.fchmod(output.fileno(), stat.S_IMODE(before.st_mode))
                                os.fsync(output.fileno())
                        finally:
                            if output is not None:
                                output.close()
                observed.append((path, identity(before)))
        if any(identity(path.lstat()) != expected for path, expected in observed):
            raise ValueError('workspace changed during digest')
        for destination, mode in reversed(directory_modes):
            destination.chmod(mode)
        return {'sha256': hashed.hexdigest(), 'scheme': 'odysseus-source-v1',
                'files_and_dirs': count, 'bytes': total, 'excluded_names': sorted(excluded)}

    def workspace_git_state(self, cwd, owner, scope):
        """Return a fixed, non-hook Git identity for one exact worktree root."""
        root = self.safe_cwd(cwd)
        self.lease(root, owner, scope)
        if self.busy(root):
            raise ValueError('workspace has an active runner job')
        top = self._git(root, 'rev-parse', '--show-toplevel').decode().strip()
        if os.path.realpath(top) != os.path.realpath(root):
            raise ValueError('workspace must point to the Git root')
        head = self._git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip().lower()
        status = self._git(root, 'status', '--porcelain=v1', '--untracked-files=all').decode()
        if not re.fullmatch(r'[0-9a-f]{40,64}', head):
            raise ValueError('workspace HEAD is invalid')
        return {'root': root, 'head': head, 'clean': not bool(status.strip())}

    def verification_copy(self, args, owner, scope):
        """Private byte copy, not a sandbox. Retain failed/finished copies."""
        if set(args) != {'source', 'expected_source_sha256', 'idempotency_key'}:
            raise ValueError('source, expected_source_sha256 and idempotency_key required')
        if digest([owner, scope]) in self.data['cancelled_scopes']:
            raise ValueError('scope has been cancelled')
        raw = args['source']
        if not isinstance(raw, str) or not raw.startswith('/') or Path(raw).is_symlink():
            raise ValueError('source must be an absolute ordinary directory')
        source = Path(self.safe_cwd(raw))
        if source == self.state or source in self.state.parents:
            raise ValueError('runner state must not be inside source')
        self.lease(source, owner, scope)
        expected, key = args['expected_source_sha256'], args['idempotency_key']
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('expected_source_sha256 must be SHA-256')
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError('idempotency_key required (1..200 characters)')
        def public(record):
            return {k: v for k, v in record.items() if k not in {'owner', 'scope', 'key'}}
        for record in self.data['worktrees'].values():
            if (record['owner'], record['scope'], record['key']) == (owner, scope, key):
                if (record.get('kind') != 'verification-copy' or record['source'] != str(source)
                        or record['source_sha256'] != expected):
                    raise ValueError('idempotency key belongs to a different source/hash')
                if record['status'] != 'ready':
                    raise ValueError('copy preparation is not ready; retained files require inspection and a new request identity')
                if self.workspace_digest(source, owner, scope)['sha256'] != expected:
                    raise ValueError('source changed since verification copy')
                if self.workspace_digest(record['path'], owner, scope)['sha256'] != expected:
                    raise ValueError('verification copy changed; automatic replacement forbidden')
                return public(record)
        before = self.workspace_digest(source, owner, scope)
        if before['sha256'] != expected:
            raise ValueError('source changed before verification copy')
        if sum(r.get('kind') == 'verification-copy' for r in self.data['worktrees'].values()) >= MAX_VERIFICATION_COPIES:
            raise ValueError('verification copy retention limit reached; operator review required, no automatic deletion')
        identity = uuid.uuid4().hex
        parent = self.state / 'verification-copies'
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or parent.resolve() != parent:
            raise ValueError('verification storage must be an ordinary private directory')
        target = parent / identity
        record = {'id': identity, 'kind': 'verification-copy', 'owner': owner, 'scope': scope, 'key': key,
                  'source': str(source), 'path': str(target), 'status': 'preparing',
                  'source_sha256': expected, 'copy_sha256': None, 'scheme': before['scheme'],
                  'bytes': before['bytes'],
                  'excluded_names': before['excluded_names'], 'git_metadata_present': False,
                  'created_at': time.time()}
        self.data['worktrees'][identity] = record
        self.save()  # durable preparing claim before filesystem effects
        try:
            target.mkdir(mode=0o700)
            copied = self.workspace_digest(source, owner, scope, _copy_to=target)
            after_source = self.workspace_digest(source, owner, scope)
            after_copy = self.workspace_digest(target, owner, scope)
            if any(item['sha256'] != expected for item in (copied, after_source, after_copy)):
                raise ValueError('source/copy changed during preparation')
            record.update(status='ready', copy_sha256=after_copy['sha256'], ready_at=time.time())
            self.save()
            return public(record)
        except BaseException:
            record.update(status='failed', reason='copy preparation failed; retained files were not deleted')
            self.save()
            raise

    def _job(self, op, args, owner, scope):
        if op == 'terminal.list':
            return {'jobs': [self.public_job(r) for r in self.data['jobs'].values()
                             if (r['owner'], r['scope']) == (owner, scope)]}
        record = self.record('jobs', args.get('id'), owner, scope)
        identity = record['id']
        if op == 'terminal.poll':
            requested = max(0, int(args.get('offset', 0)))
            start = max(requested, record['output_start'])
            limit = min(60000, max(1, int(args.get('limit', 60000))))
            path = self.state / (identity + '.output')
            data = path.read_bytes() if path.exists() else b''
            chunk = data[start-record['output_start']:start-record['output_start']+limit]
            return {**self.public_job(record), 'output': chunk.decode('utf-8', errors='replace'),
                    'output_base64': base64.b64encode(chunk).decode(), 'offset': start,
                    'next_offset': start + len(chunk), 'truncated': requested < record['output_start'],
                    'notice': 'Output pruned; completed job is retained and will not be replayed.' if record.get('output_pruned') else None}
        if record['status'] != 'running' or identity not in self.processes:
            raise ValueError('job is not running')
        proc, fd = self.processes[identity]
        if op in {'terminal.input', 'terminal.resize'} and record['kind'] != 'terminal.create':
            raise ValueError('operation requires a PTY terminal')
        if op == 'terminal.input':
            data = args.get('data')
            if not isinstance(data, str) or len(data.encode()) > 16384:
                raise ValueError('terminal input must be a string of at most 16384 bytes')
            if args.get('secret') is True:
                flags = termios.tcgetattr(fd)
                flags[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, flags)
            # Never log/persist input. ECHO stays disabled after secret=true;
            # the caller may explicitly restore it with terminal.input echo=true.
            elif args.get('echo') is True:
                flags = termios.tcgetattr(fd)
                flags[3] |= termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, flags)
            try:
                written = os.write(fd, data.encode())
            except BlockingIOError:
                written = 0
            return {**self.public_job(record), 'bytes_written': written,
                    'input_complete': written == len(data.encode())}
        elif op == 'terminal.resize':
            self._resize(fd, args)
        elif op == 'terminal.interrupt':
            if record['kind'] == 'terminal.create':
                os.write(fd, b'\x03')
            else:
                self._kill(proc, signal.SIGINT)
        elif op == 'terminal.stop':
            self._kill(proc)
        else:
            raise ValueError('unknown terminal operation')
        return self.public_job(record)

    def _git(self, cwd, *args, env=None, input=None):
        # Git uses exit 128 for several unrelated failures; keep its diagnostics
        # in a fixed locale internally and expose a narrow machine-readable code
        # rather than requiring API clients to parse localized human text.
        git_env = dict(os.environ if env is None else env, LC_ALL='C', LANG='C', LANGUAGE='C')
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-C', str(cwd), *args],
                                env=git_env, input=input, capture_output=True, timeout=30)
        if result.returncode:
            detail = result.stderr.decode(errors='replace')[:2000]
            if result.returncode == 128 and detail.startswith('fatal: not a git repository'):
                raise RunnerError('Git: ' + detail, 'not_git_repository')
            raise RunnerError('Git: ' + detail, 'git_error')
        if len(result.stdout) > 8 * 1024 * 1024:
            raise ValueError('Git result exceeds 8 MiB limit')
        return result.stdout

    def _snapshot(self, source):
        fd, index = tempfile.mkstemp(prefix='index-', dir=self.state)
        os.close(fd)
        os.unlink(index)
        env = dict(os.environ, GIT_INDEX_FILE=index)
        try:
            self._git(source, 'read-tree', 'HEAD', env=env)
            self._git(source, 'add', '-A', '--', '.', env=env)
            tree = self._git(source, 'write-tree', env=env).decode().strip()
            entries = self._git(source, 'ls-tree', '-r', tree)
            if any(line.startswith(b'160000 ') for line in entries.splitlines()):
                raise ValueError('submodule snapshots are not supported; use an ordinary checkout')
            return tree
        finally:
            for path in (index, index + '.lock'):
                if os.path.exists(path):
                    os.unlink(path)

    def _git_action(self, op, args, owner, scope):
        if op == 'git.worktree.create':
            source = self.safe_cwd(args.get('source'))
            source = self._git(source, 'rev-parse', '--show-toplevel').decode().strip()
            parent_scope = args.get('parent_scope')
            if parent_scope is not None and (not isinstance(parent_scope, str) or not parent_scope or len(parent_scope) > 200):
                raise ValueError('invalid parent_scope')
            # parent_scope is a server-verified team membership capability, not
            # a value the application may forward directly from a model/user.
            self.lease(source, owner, scope, parent_scope)
            key = args.get('idempotency_key')
            if not isinstance(key, str) or not key or len(key) > 200:
                raise ValueError('idempotency_key required')
            for rec in self.data['worktrees'].values():
                if (rec['owner'], rec['scope'], rec['key']) == (owner, scope, key):
                    if rec.get('kind') == 'verification-copy':
                        raise ValueError('idempotency key belongs to an ordinary verification copy, not Git')
                    if rec['source'] != source or rec.get('parent_scope') != parent_scope:
                        raise ValueError('idempotency key belongs to another source')
                    return {k: v for k, v in rec.items() if k not in {'owner', 'scope', 'key'}}
            if self.busy(source):
                raise ValueError('source has active runner jobs')
            tree = self._snapshot(source)
            parent = self._git(source, 'rev-parse', 'HEAD').decode().strip()
            env = dict(os.environ, GIT_AUTHOR_NAME='Odysseus checkpoint', GIT_AUTHOR_EMAIL='checkpoint@localhost',
                       GIT_COMMITTER_NAME='Odysseus checkpoint', GIT_COMMITTER_EMAIL='checkpoint@localhost')
            commit = self._git(source, 'commit-tree', tree, '-p', parent, env=env,
                               input=b'Odysseus isolated working-tree snapshot\n').decode().strip()
            if self._snapshot(source) != tree:
                raise ValueError('source changed while snapshotting; retry')
            identity = uuid.uuid4().hex
            path = str(self.state / 'worktrees' / identity)
            self._git(source, 'worktree', 'add', '--detach', path, commit)
            record = {'id': identity, 'owner': owner, 'scope': scope, 'key': key,
                      'source': source, 'path': path, 'base_commit': commit,
                      'parent_scope': parent_scope,
                      'source_tree': tree, 'created_at': time.time(), 'status': 'ready'}
            self.data['worktrees'][identity] = record
            self.save()
            return {k: v for k, v in record.items() if k not in {'owner', 'scope', 'key'}}
        if op == 'git.rollback':
            record = self.record('checkpoints', args.get('checkpoint_id'), owner, scope)
            source = record['source']
            if self.busy(source):
                raise ValueError('source has active runner jobs')
            current = self._snapshot(source)
            if current != args.get('expected_source_tree') or current != record['after_tree']:
                raise ValueError('source changed since checkpoint; rollback refused')
            patch = self._git(source, 'diff', '--binary', '--no-ext-diff', '--no-textconv', current, record['before_tree'])
            self._git(source, 'apply', '--check', '--binary', '-', input=patch)
            self._git(source, 'apply', '--binary', '-', input=patch)
            record['rolled_back_at'] = time.time()
            self.save()
            return {'checkpoint_id': record['id'], 'source_tree': self._snapshot(source)}
        record = self.record('worktrees', args.get('id'), owner, scope)
        if record.get('kind') == 'verification-copy':
            raise ValueError('ordinary verification copies have no Git metadata')
        source, worktree = record['source'], record['path']
        integration_key = args.get('idempotency_key') if op == 'git.integrate' else None
        selected_paths = args.get('paths') if op == 'git.integrate' else None
        if selected_paths is not None:
            if not isinstance(selected_paths, list) or len(selected_paths) > 1000:
                raise ValueError('paths must be a list of at most 1000 literal repository-relative file paths')
            for path in selected_paths:
                if (not isinstance(path, str) or not path or path.startswith(('/', ':'))
                        or any(char in path for char in '\\*?[]\x00\r\n')
                        or any(part in {'', '.', '..'} for part in path.split('/'))):
                    raise ValueError('selected paths must be literal repository-relative files without traversal or wildcards')
            selected_paths = sorted(set(selected_paths))
        retry_checkpoint = None
        # The source guard is a refreshed concurrency condition, not the action
        # identity: another independent worker may merge before a retry. The
        # exact target worktree + reviewed tree remain sealed and hash-validated.
        signature_args = [record['id'], args.get('expected_worktree_tree')]
        if selected_paths is not None:
            signature_args.append(selected_paths)
        integration_signature = digest(signature_args)
        if integration_key is not None:
            if not isinstance(integration_key, str) or not integration_key or len(integration_key) > 200:
                raise ValueError('invalid integration idempotency key')
            for checkpoint in self.data['checkpoints'].values():
                if (checkpoint.get('owner'), checkpoint.get('scope'), checkpoint.get('integration_key')) != (owner, scope, integration_key):
                    continue
                if checkpoint.get('integration_signature') != integration_signature:
                    raise ValueError('integration idempotency key already used for different arguments')
                if checkpoint.get('rolled_back_at'):
                    raise ValueError('integration was rolled back; a fresh reviewed action is required')
                if checkpoint.get('status') == 'applied':
                    return checkpoint['result']
                if checkpoint.get('status') != 'prepared':
                    raise ValueError('integration outcome requires manual reconciliation')
                observed = self._snapshot(source)
                if observed == checkpoint['after_tree']:
                    checkpoint['status'] = 'applied'
                    self.save()
                    return checkpoint['result']
                if observed != checkpoint['before_tree']:
                    raise ValueError('prepared integration source diverged; manual reconciliation required')
                retry_checkpoint = checkpoint['id']
                break

        def no_change(tree):
            result = {'checkpoint_id': None, 'source_tree': tree, 'changed': False}
            if integration_key:
                identity = retry_checkpoint or uuid.uuid4().hex
                self.data['checkpoints'][identity] = {'id': identity, 'owner': owner, 'scope': scope,
                    'source': source, 'before_tree': tree, 'after_tree': tree, 'status': 'applied',
                    'integration_key': integration_key, 'integration_signature': integration_signature,
                    'result': result}
                self.save()
            return result

        if self.busy(source) or self.busy(worktree):
            raise ValueError('source/worktree has active runner jobs')
        current = self._snapshot(worktree)
        patch = self._git(worktree, 'diff', '--binary', '--no-renames', '--no-ext-diff', '--no-textconv', record['source_tree'], current)
        changed_files = self._git(worktree, 'diff', '--name-only', '-z', '--no-renames',
                                 '--no-ext-diff', '--no-textconv', record['source_tree'], current)
        changed_files = [path.decode('utf-8') for path in changed_files.split(b'\x00') if path]
        if op == 'git.diff':
            return {'patch': patch.decode('utf-8', errors='replace'), 'source_tree': self._snapshot(source),
                    'worktree_tree': current, 'files': changed_files, 'truncated': False}
        if op != 'git.integrate':
            raise ValueError('unknown Git operation')
        before = self._snapshot(source)
        if before != args.get('expected_source_tree') or current != args.get('expected_worktree_tree'):
            raise ValueError('source/worktree version changed; integration refused')
        if selected_paths is not None:
            if any(path not in changed_files for path in selected_paths):
                raise ValueError('selected path is not a changed file in the reviewed worktree')
            patch = self._git(worktree, '--literal-pathspecs', 'diff', '--binary', '--no-renames',
                              '--no-ext-diff', '--no-textconv', record['source_tree'], current,
                              '--', *selected_paths) if selected_paths else b''
        if not patch:
            return no_change(before)
        # Merge in an isolated temporary index. --3way uses the patch's recorded
        # base blobs, preserving already-integrated independent worker changes.
        # A conflict leaves ONLY the temporary index changed, never the source.
        fd, index = tempfile.mkstemp(prefix='merge-index-', dir=self.state)
        os.close(fd)
        os.unlink(index)
        env = dict(os.environ, GIT_INDEX_FILE=index)
        try:
            self._git(source, 'read-tree', before, env=env)
            self._git(source, 'apply', '--cached', '--3way', '--binary', '-', env=env, input=patch)
            expected = self._git(source, 'write-tree', env=env).decode().strip()
        finally:
            for item in (index, index + '.lock'):
                if os.path.exists(item):
                    os.unlink(item)
        if expected == before:
            return no_change(before)
        patch = self._git(source, 'diff', '--binary', '--no-ext-diff', '--no-textconv', before, expected)
        self._git(source, 'apply', '--check', '--binary', '-', input=patch)
        if self._snapshot(source) != before:
            raise ValueError('source changed while preparing merge; integration refused')
        # Store the recovery checkpoint BEFORE writes; index is never rewritten.
        identity = retry_checkpoint or uuid.uuid4().hex
        result = {'checkpoint_id': identity, 'source_tree': expected, 'changed': True}
        checkpoint = {'id': identity, 'owner': owner, 'scope': scope, 'source': source,
                      'before_tree': before, 'after_tree': expected, 'status': 'prepared',
                      'integration_key': integration_key, 'integration_signature': integration_signature,
                      'result': result}
        self.data['checkpoints'][identity] = checkpoint
        self.save()
        self._git(source, 'apply', '--binary', '-', input=patch)
        after = self._snapshot(source)
        if after != expected:
            checkpoint['status'] = 'concurrent_change_detected'
            self.save()
            raise ValueError(f'patch applied but concurrent source changes detected; checkpoint {identity}; inspect manually, automatic rollback refused')
        checkpoint.update(after_tree=after, status='applied')
        self.save()
        return result

    def _file_state(self, path, with_data=False):
        path = Path(path).absolute()
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError('file checkpoints reject symlink paths and parents')
        try:
            info = path.stat()
        except FileNotFoundError:
            return {'exists': False, 'sha256': None, 'size': 0}, None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('checkpoint targets must be regular single-link files')
        if info.st_size > 2 * 1024 * 1024:
            raise ValueError('checkpoint target exceeds 2 MiB')
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('checkpoint target changed while opening')
            data = stream.read(2 * 1024 * 1024 + 1)
            final = os.fstat(stream.fileno())
            if self._file_identity(final) != self._file_identity(info):
                raise ValueError('checkpoint target changed while hashing')
        if len(data) > 2 * 1024 * 1024:
            raise ValueError('checkpoint target grew beyond 2 MiB')
        return {'exists': True, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                'identity': self._file_identity(info)}, data if with_data else None

    @staticmethod
    def _file_identity(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid, info.st_nlink]

    def _check_file_identity(self, path, state):
        # This closes the stale-baseline window; POSIX offers no atomic
        # compare-and-replace against arbitrary non-cooperating writers.
        if any(item.is_symlink() for item in (Path(path), *Path(path).parents)):
            raise ValueError('file path changed during rollback')
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            if state['exists']:
                raise ValueError('file disappeared during rollback')
            return
        if not state['exists'] or self._file_identity(current) != state.get('identity'):
            raise ValueError('file changed during rollback')

    def _begin_file_checkpoint(self, paths, owner, scope, model_policy=None):
        paths = sorted(set(os.path.abspath(path) for path in paths))
        if not 1 <= len(paths) <= 32:
            raise ValueError('file checkpoint requires 1..32 exact paths')
        if len(self.data['file_checkpoints']) >= MAX_FILE_CHECKPOINTS:
            raise ValueError('file checkpoint metadata limit reached; operator archival required')
        prepared, size = [], 0
        for path in paths:
            state, contents = self._file_state(path, with_data=True)
            size += state['size']
            if size > 8 * 1024 * 1024:
                raise ValueError('file checkpoint exceeds 8 MiB')
            prepared.append((path, state, contents))
        retained = sum(record.get('bytes', 0) for record in self.data['file_checkpoints'].values())
        if retained + size > MAX_FILE_CHECKPOINT_BYTES:
            raise ValueError('file checkpoint storage limit reached; operator archival required')
        identity = uuid.uuid4().hex
        directory = self.state / 'file-checkpoints' / identity
        directory.mkdir(parents=True, mode=0o700)
        files = []
        for index, (path, state, contents) in enumerate(prepared):
            blob = None
            if contents is not None:
                blob = str(index) + '.before'
                with (directory / blob).open('xb') as stream:
                    os.chmod(directory / blob, 0o600)
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
            files.append({'path': path, 'before': state, 'blob': blob, 'after': None})
        record = {'id': identity, 'owner': owner, 'scope': scope, 'created_at': time.time(),
                  'status': 'prepared', 'files': files, 'bytes': size,
                  'model_policy': model_policy}
        self.data['file_checkpoints'][identity] = record
        self.save()  # originals and claim durable BEFORE changing the target
        return record

    def _finish_file_checkpoint(self, record, success):
        changed = False
        try:
            for item in record['files']:
                item['after'], _ = self._file_state(item['path'])
                changed |= item['after']['sha256'] != item['before']['sha256'] or item['after']['exists'] != item['before']['exists']
            record['status'] = ('applied' if success else 'partial') if changed else 'no_change'
            record['finished_at'] = time.time()
        except (OSError, ValueError):
            record['status'] = 'uncertain'
        self.save()

    @staticmethod
    def _public_file_checkpoint(record):
        return {'id': record['id'], 'status': record['status'], 'created_at': record['created_at'],
                'files': [{'path': item['path'], 'before_sha256': item['before']['sha256'],
                           'after_sha256': item['after']['sha256'] if item['after'] else None,
                           'before_exists': item['before']['exists'],
                           'after_exists': item['after']['exists'] if item['after'] else None}
                          for item in record['files']]}

    def _file_rollback(self, args, owner, scope):
        from host_files import _check, _model_args
        record = self.record('file_checkpoints', args.get('checkpoint_id'), owner, scope)
        if record['status'] not in {'applied', 'partial'}:
            raise ValueError('checkpoint is not a known applied file change')
        expected = args.get('expected_sha256')
        paths = {item['path'] for item in record['files']}
        if not isinstance(expected, dict) or set(expected) != paths:
            raise ValueError('expected_sha256 must contain every exact checkpoint path')
        prepared = []
        for item in record['files']:
            path = item['path']
            target_parent = Path(path).parent
            if any(job['status'] == 'running' and (
                    (job.get('owner'), job.get('scope')) == (owner, scope)
                    or Path(job['cwd']) == target_parent
                    or Path(job['cwd']) in target_parent.parents
                    or target_parent in Path(job['cwd']).parents)
                   for job in self.data['jobs'].values()):
                raise ValueError('overlapping terminal or command is active; rollback refused')
            lease = self.lease(path, owner, scope)
            if lease and self.busy(self.data['worktrees'][lease]['path']):
                raise ValueError('worktree has active jobs; rollback refused')
            if record.get('model_policy'):
                _model_args('write_file', {'path': path, 'content': ''}, record['model_policy']['cwd'], record['model_policy'])
            current, _ = self._file_state(path)
            # Older durable checkpoints predate stat identity; still pin the
            # current hashed identity for every mutation below.
            if current['sha256'] != expected[path] or any(current.get(key) != value for key, value in item['after'].items()):
                raise ValueError('file changed after checkpoint; rollback refused')
            contents = None
            if item['before']['exists']:
                contents = (self.state / 'file-checkpoints' / record['id'] / item['blob']).read_bytes()
                if hashlib.sha256(contents).hexdigest() != item['before']['sha256']:
                    raise ValueError('checkpoint original integrity check failed')
            prepared.append((item, current, contents))
        completed = []
        try:
            for item, current, contents in prepared:
                path = item['path']
                now, _ = self._file_state(path)
                if now != current:
                    raise ValueError('file changed during rollback')
                self._check_file_identity(path, current)
                if not item['before']['exists']:
                    if current['exists']:
                        os.unlink(path)  # exact created file, validated SHA; never recursive
                else:
                    info = os.lstat(path) if current['exists'] else None
                    _check(path, info)
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary = tempfile.mkstemp(prefix='.odysseus-restore-', dir=str(Path(path).parent))
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            os.fchown(stream.fileno(), item['before']['uid'], item['before']['gid'])
                            os.fchmod(stream.fileno(), item['before']['mode'])
                            stream.write(contents)
                            stream.flush()
                            os.fsync(stream.fileno())
                        self._check_file_identity(path, current)
                        if info is None:
                            os.link(temporary, path)
                        else:
                            os.replace(temporary, path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                completed.append(path)
        except (OSError, ValueError) as exc:
            record.update(status='rollback_partial', rollback_completed=completed)
            self.save()
            raise ValueError(f'partial rollback; completed paths={completed!r}; {exc}') from exc
        record.update(status='rolled_back', rolled_back_at=time.time())
        self.save()
        return {'checkpoint_id': record['id'], 'status': 'rolled_back', 'files': completed}

    def _file(self, op, args, owner, scope):
        from host_files import handle, _path, _read, _check, _write, _model_args, _model_path_allowed
        if op == 'file.checkpoint.list':
            return {'checkpoints': [self._public_file_checkpoint(record) for record in self.data['file_checkpoints'].values()
                                    if (record['owner'], record['scope']) == (owner, scope)]}
        if op == 'file.rollback':
            return self._file_rollback(args, owner, scope)
        cwd = self.safe_cwd(args.get('cwd'))
        if op == 'file.call':
            tool, content = args.get('tool'), args.get('content', {})
            if tool not in {'read_file', 'write_file', 'edit_file', 'apply_patch', 'ls', 'glob', 'grep', 'get_workspace'}:
                raise ValueError('unknown file tool')
            parsed = json.loads(content) if isinstance(content, str) and content.strip().startswith('{') else content
            path_guard = None
            if 'model_policy' in args:
                policy = args['model_policy']
                if not isinstance(policy, dict):
                    raise PermissionError('model_policy must be a server-supplied object')
                path_guard = {'cwd': policy.get('cwd'), 'write_scope': policy.get('write_scope')}
                content = parsed = _model_args(tool, parsed, cwd, path_guard)
            # Arbitrary patches may name many paths. Check every header, then let
            # the strict underlying parser validate the complete patch.
            if tool == 'apply_patch':
                text = (parsed.get('patch_text') or parsed.get('patchText') or parsed.get('patch') or '') if isinstance(parsed, dict) else parsed
                paths = re.findall(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$', text, re.M)
            else:
                paths = [parsed.get('path', '') if isinstance(parsed, dict) else str(parsed).split('\n', 1)[0]]
            for raw in paths:
                path = _path(raw, cwd)
                lease = self.lease(path, owner, scope)
                if lease and tool in {'write_file', 'edit_file', 'apply_patch'} and self.busy(self.data['worktrees'][lease]['path']):
                    raise ValueError('worktree has an active writer')
            checkpoint = self._begin_file_checkpoint([_path(raw, cwd) for raw in paths], owner, scope, path_guard) if tool in {'write_file', 'edit_file', 'apply_patch'} else None
            try:
                result = handle(tool, content, cwd, path_guard=path_guard)
            except Exception:
                if checkpoint:
                    self._finish_file_checkpoint(checkpoint, False)
                raise
            if checkpoint:
                self._finish_file_checkpoint(checkpoint, result.get('exit_code') == 0)
                result['checkpoint_id'] = checkpoint['id']
            if tool == 'ls' and result.get('exit_code') == 0:
                directory = _path(paths[0], cwd)
                entries = []
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if not _model_path_allowed(entry.path, cwd, path_guard):
                            continue
                        if len(entries) >= 200:
                            result['truncated'] = True
                            break
                        entries.append({'name': entry.name, 'path': entry.path,
                                        'is_dir': entry.is_dir(follow_symlinks=False),
                                        'is_symlink': entry.is_symlink()})
                result['entries'] = sorted(entries, key=lambda e: (not e['is_dir'], e['name'].lower()))
            return result
        return self._binary_file(op, args, owner, scope)

    def resource_snapshot(self):
        """Best-effort read-only Linux telemetry. Missing sensors remain null."""
        if self.platform_identity['os'] != 'linux':
            return runner_platform.non_linux_snapshot(self.platform_identity['os'],
                sum(r['status'] == 'running' for r in self.data['jobs'].values()))
        def read(path, limit=16384):
            try:
                with open(path, encoding='utf-8') as stream:
                    return stream.read(limit)
            except OSError:
                return ''
        cpu = None
        line = read('/proc/stat').splitlines()
        if line and line[0].startswith('cpu '):
            ticks = [int(value) for value in line[0].split()[1:9]]
            total = sum(ticks)
            idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
            if self.cpu_previous:
                old_total, old_idle = self.cpu_previous
                if total > old_total:
                    cpu = max(0, min(100, 100 * (1 - (idle-old_idle)/(total-old_total))))
            self.cpu_previous = (total, idle)
        memory = {}
        for line in read('/proc/meminfo').splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in {'MemTotal:', 'MemAvailable:', 'SwapTotal:', 'SwapFree:'}:
                memory[parts[0].rstrip(':')] = int(parts[1]) * 1024
        temperatures = []
        import glob
        for directory in glob.glob('/sys/class/thermal/thermal_zone*')[:64]:
            value = read(directory + '/temp', 100).strip()
            if value.lstrip('-').isdigit():
                temperatures.append({'name': read(directory + '/type', 100).strip(), 'celsius': int(value)/1000})
        gpu_load = None
        for path in ['/sys/devices/gpu.0/load', '/sys/devices/platform/17000000.gpu/load',
                     '/sys/devices/platform/gpu.0/load']:
            value = read(path, 100).strip()
            if value.isdigit():
                gpu_load = int(value)/10  # Tegra load is permille.
                break
        try:
            load_average = list(os.getloadavg())
        except OSError:
            load_average = None
        return {'timestamp': time.time(), 'cpu_percent': cpu, 'cpu_count': os.cpu_count(),
                'load_average': load_average, 'memory': memory,
                'gpu_percent': gpu_load, 'temperatures': temperatures,
                'running_jobs': sum(r['status'] == 'running' for r in self.data['jobs'].values())}

    def _binary_file(self, op, args, owner, scope):
        from host_files import _path, _check
        cwd = self.safe_cwd(args.get('cwd'))
        path = _path(args.get('path', ''), cwd)
        lease = self.lease(path, owner, scope)
        if op == 'file.download':
            if not stat.S_ISREG(os.stat(path).st_mode):
                raise ValueError('only regular files can be downloaded')
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('only regular files can be downloaded')
                data = stream.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('download exceeds 2 MiB')
            return {'data_base64': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
        if op != 'file.upload':
            raise ValueError('unknown file operation')
        if lease and self.busy(self.data['worktrees'][lease]['path']):
            raise ValueError('worktree has an active writer')
        data = base64.b64decode(args.get('data_base64', ''), validate=True)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError('upload exceeds 2 MiB')
        try:
            info = os.lstat(path)
            _check(path, info)
            if info.st_size > 2 * 1024 * 1024:
                raise ValueError('existing upload target exceeds 2 MiB')
            old = Path(path).read_bytes()
            if hashlib.sha256(old).hexdigest() != args.get('expected_sha256'):
                raise ValueError('existing file needs matching expected_sha256')
        except FileNotFoundError:
            info = None
            if args.get('expected_sha256'):
                raise ValueError('expected existing file is missing')
        _check(path, info)
        checkpoint = self._begin_file_checkpoint([path], owner, scope)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.odysseus-upload-', dir=str(Path(path).parent))
        success = False
        try:
            with os.fdopen(fd, 'wb') as stream:
                if info:
                    os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                    os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check(path, info)
            if info:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
            success = True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
            self._finish_file_checkpoint(checkpoint, success)
        return {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'checkpoint_id': checkpoint['id']}

    def _load_lsp(self):
        if self.lsp_module is None:
            path = Path(__file__).with_name('engineering_lsp.py')
            if not path.is_file():
                path = Path(__file__).resolve().parents[1] / 'src' / 'engineering_lsp.py'
            spec = importlib.util.spec_from_file_location('odysseus_runner_lsp', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.lsp_module = module
        return self.lsp_module

    @staticmethod
    def _lsp_paths(value, cwd, depth=0):
        if depth > 40:
            raise ValueError('LSP payload nesting limit')
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower().endswith('uri') and isinstance(item, str):
                    parsed = urlsplit(item)
                    if parsed.scheme != 'file' or parsed.netloc not in {'', 'localhost'} or parsed.query or parsed.fragment:
                        raise ValueError('LSP requires local workspace file URIs')
                    path = Path(unquote(parsed.path)).resolve()
                    if not parsed.path.startswith('/') or (path != cwd and cwd not in path.parents):
                        raise ValueError('LSP URI outside assigned workspace')
                Runner._lsp_paths(item, cwd, depth + 1)
        elif isinstance(value, list):
            for item in value:
                Runner._lsp_paths(item, cwd, depth + 1)

    def _lsp(self, op, args, owner, scope):
        module = self._load_lsp()
        if op == 'lsp.discover':
            return {'languages': [{k: entry[k] for k in ('language', 'available', 'reason')}
                                  for entry in module.discover()]}
        if op == 'lsp.start':
            if args.get('execution_authorized') is not True or digest([owner, scope]) in self.data['cancelled_scopes']:
                raise ValueError('Current project execution authorization required')
            raw = args.get('cwd')
            if not isinstance(raw, str) or not os.path.isabs(raw):
                raise ValueError('Absolute workspace cwd required')
            cwd = self.safe_cwd(raw)
            self.lease(cwd, owner, scope)
            key = args.get('idempotency_key')
            if not isinstance(key, str) or not 0 < len(key) <= 200:
                raise ValueError('LSP idempotency key required')
            signature = digest([args.get('language'), cwd])
            for record in self.lsp_sessions.values():
                if (record['owner'], record['scope'], record['key']) == (owner, scope, key):
                    if record['signature'] != signature:
                        raise ValueError('LSP idempotency key arguments changed')
                    return self._lsp_public(record)
            if len(self.lsp_sessions) >= 128:
                raise ValueError('LSP session record limit reached')
            selected = next((entry for entry in module.discover() if entry['language'] == args.get('language')), None)
            if not selected or not selected['available']:
                return {'available': False, 'reason': 'language_server_unavailable'}
            record = {'id': uuid.uuid4().hex, 'owner': owner, 'scope': scope, 'key': key,
                      'signature': signature, 'language': selected['language'], 'cwd': cwd,
                      'authorized': True, 'status': 'starting'}
            broker = module.Broker(selected['argv'], cwd, lambda operation: record['authorized'], timeout=10)
            record['broker'] = broker
            self.lsp_sessions[record['id']] = record
            try:
                broker.start()
                record['status'] = 'running'
                return self._lsp_public(record)
            except Exception:
                broker.close()
                record['status'] = 'failed'
                raise ValueError('LSP initialization failed; inspect installed server configuration') from None
        record = self.lsp_sessions.get(args.get('id'))
        if not record or (record['owner'], record['scope']) != (owner, scope):
            raise ValueError('LSP session not found in owner/scope')
        broker = record['broker']
        if op == 'lsp.stop':
            record['authorized'] = False
            broker.close()
            record['status'] = 'stopped'
            return self._lsp_public(record)
        record['authorized'] = args.get('execution_authorized') is True and digest([owner, scope]) not in self.data['cancelled_scopes']
        if not record['authorized']:
            broker.close()
            record['status'] = 'stopped'
            raise ValueError('Current project execution authorization required')
        if record['status'] != 'running':
            raise ValueError('LSP session is not running; no automatic restart')
        cwd = Path(record['cwd'])
        self.lease(cwd, owner, scope)
        try:
            if op == 'lsp.request':
                method, params = args.get('method'), args.get('params', {})
                if not isinstance(params, dict):
                    raise ValueError('LSP params object required')
                document = params.get('textDocument')
                if not isinstance(document, dict) or not isinstance(document.get('uri'), str):
                    raise ValueError('Explicit workspace document URI required')
                self._lsp_paths(params, cwd)
                if method in {'textDocument/didOpen', 'textDocument/didChange', 'textDocument/didClose'}:
                    broker.notify(method, params)
                    result = {'available': True, 'notified': True}
                else:
                    result = broker.request(method, params)
            elif op == 'lsp.diagnostics':
                self._lsp_paths({'uri': args.get('uri')}, cwd)
                if not isinstance(args.get('uri'), str):
                    raise ValueError('Document URI required')
                result = broker.published_diagnostics(args['uri'])
            else:
                raise ValueError('Unknown LSP operation')
            self._lsp_paths(result, cwd)
            return self._lsp_redact_metadata(result)
        except Exception:
            if broker.proc is None:
                record['status'] = 'failed'
            raise ValueError('LSP request rejected or failed; no outside-workspace result returned') from None

    @staticmethod
    def _lsp_redact_metadata(value):
        # Opaque resolve tokens can encode server-private paths. Read-only
        # definition/hover/diagnostics do not require returning those tokens.
        if isinstance(value, dict):
            return {key: Runner._lsp_redact_metadata(item) for key, item in value.items()
                    if key not in {'data', 'serverInfo'}}
        if isinstance(value, list):
            return [Runner._lsp_redact_metadata(item) for item in value]
        return value

    def _lsp_public(self, record):
        # Never expose operator argv or server-specific initialize metadata.
        if record['status'] == 'running' and (record['broker'].proc is None or record['broker'].proc.poll() is not None):
            record['status'] = 'failed'
        return {'id': record['id'], 'language': record['language'], 'cwd': record['cwd'],
                'status': record['status'], 'available': record['status'] == 'running',
                'capabilities': {method: record['broker'].capabilities.get(feature) not in (None, False)
                                 for method, feature in self._load_lsp().METHODS.items()}}

    def handle(self, request):
        try:
            if not isinstance(request, dict):
                raise ValueError('request object required')
            owner, scope, op = request.get('owner'), request.get('scope'), request.get('op')
            args = request.get('args', {})
            if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in (owner, scope, op)) or not isinstance(args, dict):
                raise ValueError('owner, scope and argument object required')
            with self.lock:
                if op == 'scope.cancel':
                    key = digest([owner, scope])
                    previous = key in self.data['cancelled_scopes']
                    self.data['cancelled_scopes'][key] = {'owner': owner, 'scope': scope, 'cancelled_at': time.time()}
                    self.save()  # durable fence before terminating accepted jobs
                    for session in self.lsp_sessions.values():
                        if (session['owner'], session['scope']) == (owner, scope):
                            session['authorized'] = False
                            session['broker'].close()
                            session['status'] = 'stopped'
                    jobs = []
                    for identity, record in self.data['jobs'].items():
                        if (record['owner'], record['scope']) == (owner, scope) and identity in self.processes:
                            jobs.append(identity)
                            self._kill(self.processes[identity][0])
                    result = {'cancelled': True, 'scope': scope, 'job_ids': jobs, 'already_cancelled': previous}
                elif op in {'terminal.create', 'command.start', 'sandbox.command.start'}:
                    result = self._create(op, args, owner, scope)
                elif op.startswith('terminal.'):
                    result = self._job(op, args, owner, scope)
                elif op.startswith('git.'):
                    result = self._git_action(op, args, owner, scope)
                elif op.startswith('file.'):
                    result = self._file(op, args, owner, scope)
                elif op == 'resource.snapshot':
                    result = self.resource_snapshot()
                elif op == 'workspace.digest':
                    result = self.workspace_digest(args.get('cwd'), owner, scope)
                elif op == 'workspace.git-state':
                    if set(args) != {'cwd'}:
                        raise ValueError('workspace.git-state accepts only cwd')
                    result = self.workspace_git_state(args.get('cwd'), owner, scope)
                elif op == 'workspace.verification-copy':
                    result = self.verification_copy(args, owner, scope)
                elif op == 'runner.capabilities':
                    result = runner_platform.capabilities(self.platform_identity)
                    for supported in ('lsp.discover', 'lsp.start', 'lsp.request', 'lsp.diagnostics', 'lsp.stop',
                                      'workspace.digest', 'workspace.git-state', 'workspace.verification-copy',
                                      'sandbox.command.start'):
                        if supported not in result['supported_ops']:
                            result['supported_ops'].append(supported)
                elif op.startswith('lsp.'):
                    result = self._lsp(op, args, owner, scope)
                else:
                    raise ValueError('unknown runner operation')
            return {'ok': True, 'result': result}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            response = {'ok': False, 'error': str(exc)[:2000]}
            if isinstance(exc, RunnerError):
                response['code'] = exc.code
            return response

    def close(self):
        self.stop_event.set()
        for record in self.lsp_sessions.values():
            record['broker'].close()
        for proc, _ in list(self.processes.values()):
            self._kill(proc)
        deadline = time.monotonic() + 5
        while self.processes and time.monotonic() < deadline:
            time.sleep(.05)


def serve(state):
    state = Path(state).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockfile = open(state / 'runner.lock', 'a')
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    runner = Runner(state)
    socket_path = runner.state / 'runner.sock'
    if socket_path.exists():
        socket_path.unlink()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(60)
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST:
                response = {'ok': False, 'error': 'request too large'}
            else:
                try:
                    response = runner.handle(json.loads(raw))
                except (ValueError, TypeError):
                    response = {'ok': False, 'error': 'invalid request'}
            try:
                self.wfile.write(json.dumps(response).encode() + b'\n')
            except (BrokenPipeError, ConnectionResetError):
                pass  # Accepted daemon jobs are independent of the RPC client.

    server = socketserver.ThreadingUnixStreamServer(str(socket_path), Handler)
    server.daemon_threads = True
    os.chmod(socket_path, 0o600)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(.1)
    finally:
        server.server_close()
        runner.close()
        socket_path.unlink(missing_ok=True)
        lockfile.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--pty-child']:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execv('/bin/bash', ['/bin/bash', '--noprofile', '--norc', '-i'])
    else:
        serve(os.environ.get('ODYSSEUS_HOST_RUNNER_STATE', str(Path.home() / '.local/state/odysseus-host-runner')))
