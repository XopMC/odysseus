"""Fixed SSH stdin JSON -> private user-level runner socket. Never logs input."""
import json
import os
from pathlib import Path
import socket
import sys

MAX_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 12 * 1024 * 1024


def call(request, state=None):
    state = state or os.environ.get('ODYSSEUS_HOST_RUNNER_STATE', str(Path.home() / '.local/state/odysseus-host-runner'))
    payload = json.dumps(request).encode() + b'\n'
    if len(payload) > MAX_REQUEST:
        raise ValueError('request too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(120)
        client.connect(str(Path(state).expanduser() / 'runner.sock'))
        client.sendall(payload)
        with client.makefile('rb') as stream:
            raw = stream.readline(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('response too large')
        return json.loads(raw)


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError('request too large')
        result = call(json.loads(raw))
    except (OSError, ValueError, TypeError) as exc:
        result = {'ok': False, 'error': str(exc)[:2000]}
    print(json.dumps(result))
