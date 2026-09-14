"""Team runner transport. Only explicit host owner, using fixed pinned SSH."""
import asyncio
import json
import os
import shlex
import subprocess

from src.host_execution import enabled_for, ssh_argv


def _call(op, args, owner, scope):
    if not enabled_for(owner):
        return {'ok': False, 'error': 'host access is not enabled for this owner'}
    path = os.environ.get('ODYSSEUS_HOST_RUNNER_CLIENT', '/home/xopmc/services/odysseus-host/host_runner_client.py')
    if not os.path.isabs(path) or '\n' in path or '\x00' in path:
        return {'ok': False, 'error': 'invalid fixed runner client path'}
    request = json.dumps({'op': op, 'args': args, 'owner': owner, 'scope': scope})
    if len(request.encode()) > 8 * 1024 * 1024:
        return {'ok': False, 'error': 'runner request too large'}
    proc = subprocess.run(ssh_argv('python3 ' + shlex.quote(path)), input=request,
                          text=True, capture_output=True, timeout=135)
    if proc.returncode:
        return {'ok': False, 'error': 'runner SSH failed: ' + proc.stderr[:2000]}
    result = json.loads(proc.stdout)
    if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
        raise ValueError('invalid runner response')
    return result


async def call(op, args, owner, scope):
    """The route MUST authenticate owner/scope; this seam adds exact-owner opt-in.

    Cancellation of this HTTP/SSH call does not stop an accepted daemon job.
    Retry command.start with the same idempotency_key to recover its identifier.
    """
    try:
        return await asyncio.to_thread(_call, op, args, owner, scope)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'error': str(exc)[:2000]}
