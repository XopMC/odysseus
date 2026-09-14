"""Opt-in, read-only LSP broker, not complete IDE/language support.

Operator-selected argv is trusted configuration; never accept model executables.
An authorization callback receives the operation and must return exactly True.
Discovery does not spawn. Callers own broker lifecycle; use a context manager.
"""
import json
import os
from pathlib import Path
import queue
import select
import shutil
import signal
import subprocess
import threading
import time

MAX_FRAME = 2 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(4)
LANGUAGES = {
    'python': ('pyright-langserver', '--stdio'),
    'typescript': ('typescript-language-server', '--stdio'),
    'c_cpp': ('clangd',), 'swift': ('sourcekit-lsp',), 'go': ('gopls',),
    'rust': ('rust-analyzer',), 'solidity': ('nomicfoundation-solidity-language-server', '--stdio'),
    'metal': None,
}
METHODS = {'textDocument/definition': 'definitionProvider',
           'textDocument/references': 'referencesProvider',
           'textDocument/hover': 'hoverProvider',
           'textDocument/documentSymbol': 'documentSymbolProvider',
           'textDocument/diagnostic': 'diagnosticProvider'}
NOTIFICATIONS = {'initialized', 'textDocument/didOpen', 'textDocument/didChange', 'textDocument/didClose', 'exit'}


def discover():
    result = []
    for language, command in LANGUAGES.items():
        path = shutil.which(command[0]) if command else None
        result.append({'language': language, 'available': bool(path),
                       'argv': [path, *command[1:]] if path else None,
                       'reason': None if path else ('no_lsp' if command is None else 'not_installed')})
    return result


def _frame(stream):
    length = None
    for _ in range(16):
        line = stream.readline(8193)
        if not line:
            raise EOFError('LSP stream closed')
        if len(line) > 8192 or not line.endswith(b'\r\n'):
            raise ValueError('Malformed LSP header')
        if line == b'\r\n':
            break
        name, separator, value = line.partition(b':')
        if not separator:
            raise ValueError('Malformed LSP header')
        if name.lower() == b'content-length':
            if length is not None or not value.strip().isdigit():
                raise ValueError('Invalid LSP content length')
            length = int(value.strip())
    else:
        raise ValueError('Too many LSP headers')
    if length is None or not 0 < length <= MAX_FRAME:
        raise ValueError('Invalid LSP content length')
    body = stream.read(length)
    if len(body) != length:
        raise EOFError('Truncated LSP body')
    message = json.loads(body)
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        raise ValueError('Invalid LSP JSON-RPC')
    return message


class Broker:
    def __init__(self, argv, workspace, authorize, timeout=10):
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(x, str) and x and '\x00' not in x for x in argv):
            raise ValueError('Trusted operator argv required')
        if not os.path.isabs(argv[0]) or not callable(authorize):
            raise ValueError('Absolute operator executable and authorization callback required')
        self.argv, self.workspace, self.authorize = list(argv), Path(workspace).resolve(), authorize
        if not self.workspace.is_dir():
            raise ValueError('Workspace must exist')
        self.timeout = max(.05, min(float(timeout), 60))
        self.proc = None
        self.reader = None
        self.messages = queue.Queue(maxsize=16)
        self.diagnostics = {}
        self.capabilities = {}
        self.counter = 0
        self.lock = threading.RLock()
        self.slot = False

    def _authorized(self, operation):
        try:
            allowed = self.authorize(operation) is True
        except BaseException:
            self.close()
            raise
        if not allowed:
            self.close()
            raise PermissionError('LSP authorization required or revoked')

    def start(self):
        with self.lock:
            if self.proc is not None:
                raise RuntimeError('LSP already started')
            self._authorized('spawn')
            if not _SLOTS.acquire(blocking=False):
                raise RuntimeError('LSP process limit reached')
            self.slot = True
            try:
                self.proc = subprocess.Popen(self.argv, cwd=self.workspace, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
                os.set_blocking(self.proc.stdin.fileno(), False)
                self.reader = threading.Thread(target=self._read, daemon=True)
                self.reader.start()
                result = self._request('initialize', {'processId': os.getpid(), 'rootUri': self.workspace.as_uri(),
                    'capabilities': {'textDocument': {'publishDiagnostics': {}},
                                     'workspace': {'applyEdit': False}},
                    'workspaceFolders': [{'uri': self.workspace.as_uri(), 'name': self.workspace.name}]})
                self.capabilities = result.get('capabilities', {}) if isinstance(result, dict) else {}
                self.notify('initialized', {})
                return {'available': True, 'capabilities': self.capabilities}
            except BaseException:
                self.close()
                raise

    def _read(self):
        try:
            while True:
                message = _frame(self.proc.stdout)
                if message.get('method') == 'textDocument/publishDiagnostics' and 'id' not in message:
                    params = message.get('params', {})
                    uri = params.get('uri')
                    if isinstance(uri, str):
                        if len(self.diagnostics) >= 32 and uri not in self.diagnostics:
                            self.diagnostics.pop(next(iter(self.diagnostics)))
                        self.diagnostics[uri] = params
                    continue
                self.messages.put_nowait(message)
        except Exception:
            try:
                self.messages.put_nowait({'broker_error': True})
            except queue.Full:
                pass

    def _send(self, message):
        body = json.dumps(message, allow_nan=False).encode()
        if len(body) > MAX_FRAME:
            raise ValueError('LSP request too large')
        payload = ('Content-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body
        fd = self.proc.stdin.fileno()
        deadline = time.monotonic() + self.timeout
        while payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError('LSP write timed out')
            try:
                count = os.write(fd, payload)
                payload = payload[count:]
            except BlockingIOError:
                continue

    def _request(self, method, params):
        self._authorized(method)
        if self.proc is None:
            raise RuntimeError('LSP is not running')
        self.counter += 1
        identity = self.counter
        try:
            self._send({'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params})
            deadline = time.monotonic() + self.timeout
            while True:
                message = self.messages.get(timeout=max(.001, deadline - time.monotonic()))
                if message.get('broker_error'):
                    raise RuntimeError('LSP protocol failed')
                if 'method' in message:
                    if 'id' in message:
                        # Never honor workspace/applyEdit or server commands.
                        self._send({'jsonrpc': '2.0', 'id': message['id'],
                                    'error': {'code': -32601, 'message': 'Client method unsupported'}})
                elif message.get('id') == identity:
                    if 'error' in message:
                        raise RuntimeError('LSP request failed')
                    return message.get('result')
                if time.monotonic() >= deadline:
                    raise TimeoutError('LSP request timed out')
        except queue.Empty:
            self.close()
            raise TimeoutError('LSP request timed out') from None
        except BaseException:
            self.close()
            raise

    def request(self, method, params):
        with self.lock:
            self._authorized(method)
            if method not in METHODS:
                raise PermissionError('LSP method is not read-only/allowed')
            advertised = self.capabilities.get(METHODS[method])
            if advertised is None or advertised is False:
                return {'available': False, 'reason': 'unsupported_feature', 'method': method}
            return {'available': True, 'result': self._request(method, params)}

    def notify(self, method, params):
        with self.lock:
            self._authorized(method)
            if method not in NOTIFICATIONS:
                raise PermissionError('LSP notification not allowed')
            try:
                self._send({'jsonrpc': '2.0', 'method': method, 'params': params})
            except BaseException:
                self.close()
                raise

    def published_diagnostics(self, uri):
        self._authorized('diagnostics')
        value = self.diagnostics.get(uri)
        return {'available': value is not None, 'result': value}

    def shutdown(self):
        try:
            with self.lock:
                if self.proc is not None and self.proc.poll() is None:
                    self._request('shutdown', None)
                    self.notify('exit', None)
        finally:
            self.close()

    def close(self):
        proc = self.proc
        if proc is not None:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()
            self.proc = None
        if self.slot:
            self.slot = False
            _SLOTS.release()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.shutdown()
