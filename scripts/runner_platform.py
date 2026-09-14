"""Read-only platform facts; discovery never executes a discovered toolchain."""
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

PROTOCOL_VERSION = 1
SUPPORTED_OPS = ('runner.capabilities', 'resource.snapshot', 'scope.cancel',
    'terminal.create', 'terminal.poll', 'terminal.input', 'terminal.resize',
    'terminal.interrupt', 'terminal.stop', 'terminal.list', 'command.start',
    'sandbox.command.start',
    'file.call', 'file.upload', 'file.download', 'file.checkpoint.list',
    'file.rollback', 'git.worktree.create', 'git.diff', 'git.integrate', 'git.rollback')
TOOLCHAINS = ('git', 'python3', 'node', 'npm', 'pnpm', 'bun', 'cmake', 'ninja',
              'clang', 'gcc', 'make', 'cargo', 'go', 'swift', 'xcodebuild', 'docker', 'nvcc')


def sysctl(name):
    if name not in {'kern.boottime', 'hw.memsize'}:
        raise ValueError('unsupported system metric')
    try:
        result = subprocess.run(['/usr/sbin/sysctl', '-n', name], capture_output=True,
                                text=True, timeout=2, cwd='/', env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
        return result.stdout.strip()[:1024] if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def identity():
    system = platform.system().lower()
    boot = None
    if system == 'linux':
        try:
            value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if re.fullmatch(r'[a-fA-F0-9-]{36}', value):
                boot = 'linux:' + value
        except OSError:
            pass
    elif system == 'darwin':
        value = sysctl('kern.boottime') or ''
        match = re.search(r'\bsec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)', value)
        if match:
            boot = 'darwin:' + match[1] + ':' + match[2]
    return {'os': system, 'arch': platform.machine(), 'release': platform.release(), 'boot_id': boot}


def capabilities(platform_identity=None):
    facts = platform_identity or identity()
    return {'protocol_version': PROTOCOL_VERSION, 'platform': dict(facts),
            'supported_ops': list(SUPPORTED_OPS),
            'toolchains': {name: shutil.which(name) for name in TOOLCHAINS},
            'telemetry': {'backend': {'linux': 'linux-procfs', 'darwin': 'darwin-sysctl'}.get(facts['os'], 'unsupported')}}


def non_linux_snapshot(system, running_jobs):
    memory = {'MemTotal': None, 'MemAvailable': None, 'SwapTotal': None, 'SwapFree': None}
    if system == 'darwin':
        raw = sysctl('hw.memsize')
        if raw and raw.isdigit():
            memory['MemTotal'] = int(raw)
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = None
    return {'timestamp': time.time(), 'cpu_percent': None, 'cpu_count': os.cpu_count(),
            'load_average': load, 'memory': memory, 'gpu_percent': None,
            'temperatures': [], 'running_jobs': running_jobs,
            'telemetry_backend': 'darwin-sysctl' if system == 'darwin' else 'unsupported'}
