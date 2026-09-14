"""Fixed SSH stdin-JSON helper, installed beside host_files.py; no sudo."""
import json
import os
import selectors
import signal
import subprocess
import sys
import time

from host_files import handle as files_handle

MAX_OUTPUT = 60000


def execute(request):
    tool, content = request.get('tool'), request.get('content', '')
    cwd = os.path.expanduser(request.get('cwd') or '~')
    if tool not in {'bash', 'python'}:
        return files_handle(tool, content, cwd)
    if not isinstance(content, str) or len(content.encode()) > 100000:
        return {'error': 'host command must be a string, at most 100000 bytes', 'exit_code': 1}
    import re
    if re.search(r'\bsudo\b', content):
        return {'error': 'sudo requires your explicit one-shot approval at /host-access. Never put passwords in chat/tool arguments.', 'exit_code': 1}
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
                kept = chunk[:max(0, MAX_OUTPUT - size)]
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
        output += '\n[Output truncated at 60000 bytes]'
    if timed_out:
        output += '\n[Host command timed out; process group terminated. Use #!bg for long jobs.]'
    return {'output': output, 'exit_code': 124 if timed_out else proc.returncode,
            'truncated': truncated, 'timed_out': timed_out, 'execution_host': 'jetson'}


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            raise ValueError('request too large')
        result = execute(json.loads(raw))
    except Exception as exc:
        result = {'error': f'host execution: {exc}', 'exit_code': 1}
    print(json.dumps(result))
