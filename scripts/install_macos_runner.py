"""Portable user LaunchAgent installer. Dry-run by default; --install is explicit.

No root, passwords, SSH trust changes, model downloads or TCP listener. Existing
installations are refused: upgrades require a separately reviewed stop/backup.
The default state matches host_runner_client.py for a fixed SSH client command.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys

LABEL = 'com.odysseus.host-runner'
COMPONENTS = {'scripts/host_runner.py': 'host_runner.py',
              'scripts/host_runner_client.py': 'host_runner_client.py',
              'scripts/runner_platform.py': 'runner_platform.py',
              'scripts/host_files.py': 'host_files.py',
              'src/team_tool_paths.py': 'team_tool_paths.py',
              'src/engineering_lsp.py': 'engineering_lsp.py'}


def plan(source, home, python):
    source, home, python = Path(source).resolve(), Path(home).resolve(), Path(python)
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Absolute installed executable Python path required')
    for relative in COMPONENTS:
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError('Required verified runner component is missing or symlinked: ' + relative)
    base = home / 'Library/Application Support/Odysseus/HostRunner'
    state = home / '.local/state/odysseus-host-runner'
    agent = home / 'Library/LaunchAgents' / (LABEL + '.plist')
    for target in (base, state, agent):
        if any(item.is_symlink() for item in (target, *target.parents)):
            raise ValueError('Installation paths must not contain symlinks')
    config = {'Label': LABEL, 'ProgramArguments': [str(python), str(base / 'host_runner.py')],
              'WorkingDirectory': str(base), 'RunAtLoad': True, 'KeepAlive': True,
              'ProcessType': 'Background', 'ThrottleInterval': 5,
              'ExitTimeOut': 20, 'AbandonProcessGroup': False, 'Umask': 0o077,
              'EnvironmentVariables': {'ODYSSEUS_HOST_RUNNER_STATE': str(state),
                  'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
              'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}
    return {'source': str(source), 'install_dir': str(base), 'state_dir': str(state),
            'plist_path': str(agent), 'client_path': str(base / 'host_runner_client.py'),
            'label': LABEL, 'components': COMPONENTS, 'plist': config}


def install(configuration):
    if platform.system() != 'Darwin' or os.getuid() == 0:
        raise RuntimeError('Install only as the logged-in macOS user, never root')
    home = Path.home().resolve()
    base, state, agent = (Path(configuration[key]) for key in ('install_dir', 'state_dir', 'plist_path'))
    if any(home not in path.parents for path in (base, state, agent)):
        raise ValueError('Install paths must belong to current user home')
    if base.exists() or agent.exists() or (state / 'runner.sock').exists():
        raise ValueError('Existing runner installation/socket; explicit upgrade procedure required')
    # Refuse to silently replace a loaded agent even if its files were removed.
    target = 'gui/' + str(os.getuid()) + '/' + LABEL
    existing = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, timeout=10)
    if existing.returncode == 0:
        raise ValueError('LaunchAgent already loaded; explicit upgrade required')
    base.mkdir(parents=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    agent.parent.mkdir(parents=True, exist_ok=True)
    for relative, name in COMPONENTS.items():
        with (base / name).open('xb') as out:
            os.chmod(base / name, 0o600)
            out.write((Path(configuration['source']) / relative).read_bytes())
    with agent.open('xb') as out:
        os.chmod(agent, 0o600)
        plistlib.dump(configuration['plist'], out)
    result = subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(agent)],
                            capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('LaunchAgent files created but bootstrap failed; inspect launchctl manually (output suppressed)')
    return {'installed': True, 'label': LABEL, 'client_path': configuration['client_path'],
            'state_dir': str(state), 'acceptance_required': 'Explicit runner.capabilities probe through configured transport'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    configuration = plan(args.source, Path.home(), args.python)
    if not args.install:
        print(json.dumps({'dry_run': True, **configuration}))
        return
    print(json.dumps(install(configuration)))


if __name__ == '__main__':
    main()
