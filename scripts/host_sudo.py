"""One-shot human-authorized sudo RPC. Secrets travel only through stdin."""
import json
import subprocess
import sys
import tempfile


def execute(request):
    argv = request.get('argv')
    password = request.get('password')
    if (not isinstance(argv, list) or not argv or len(argv) > 256
            or any(not isinstance(x, str) or '\0' in x for x in argv)
            or not isinstance(password, str) or not password or '\n' in password):
        return {'error': 'Invalid command or password', 'exit_code': 1}
    # No shell expansion here. Explicit bash -lc is available to the human.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(['/usr/bin/sudo', '-k', '-S', '-p', '', '--', *argv],
                                stdin=subprocess.PIPE, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            proc.communicate((password + '\n').encode(), timeout=30)
        except subprocess.TimeoutExpired:
            # Killing root descendants as the unprivileged caller is not
            # guaranteed: never claim that timeout cancelled the root job.
            return {'error': 'Sudo timed out; the command may still be running. Inspect the host before retrying.', 'exit_code': 124}
        output.seek(0)
        text = output.read(60001).decode('utf-8', errors='replace')
    text = text.replace(password, '[redacted]')
    return {'output': text[:60000], 'truncated': len(text) > 60000, 'exit_code': proc.returncode}


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.buffer.read(65537))
        print(json.dumps(execute(payload)))
    except Exception:
        print(json.dumps({'error': 'Host sudo request failed', 'exit_code': 1}))
