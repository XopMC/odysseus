import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mac_installer_test', ROOT / 'scripts/install_macos_runner.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class MacInstallerTests(unittest.TestCase):
    def test_generated_plist_protocol_dependencies_and_private_socket(self):
        with tempfile.TemporaryDirectory() as home:
            result = installer.plan(ROOT, home, sys.executable)
            config = plistlib.loads(plistlib.dumps(result['plist']))
            self.assertEqual(config['ProgramArguments'][0], sys.executable)
            self.assertEqual(config['Umask'], 0o077)
            self.assertNotIn('Sockets', config)
            self.assertNotIn('UserName', config)
            self.assertFalse(config['AbandonProcessGroup'])
            self.assertTrue({'runner_platform.py', 'engineering_lsp.py', 'team_tool_paths.py'} <= set(result['components'].values()))
            self.assertEqual(config['EnvironmentVariables']['ODYSSEUS_HOST_RUNNER_STATE'], str(Path(home).resolve() / '.local/state/odysseus-host-runner'))

    def test_default_dryrun_performs_no_process_or_filesystem_writes(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(installer.Path, 'home', return_value=Path(directory)), \
             patch.object(installer.subprocess, 'run', side_effect=AssertionError('must not launch')), contextlib.redirect_stdout(output):
            installer.main(['--source', str(ROOT)])
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(json.loads(output.getvalue())['dry_run'])

    def test_missing_dependency_and_relative_interpreter_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):installer.plan(directory, directory, sys.executable)
            with self.assertRaises(ValueError):installer.plan(ROOT, directory, 'python3')

    def test_existing_install_refuses_before_launchctl(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            config = installer.plan(ROOT, home, sys.executable)
            Path(config['install_dir']).mkdir(parents=True)
            with patch.object(installer.platform, 'system', return_value='Darwin'), patch.object(installer.os, 'getuid', return_value=501), \
                 patch.object(installer.Path, 'home', return_value=home), patch.object(installer.subprocess, 'run', side_effect=AssertionError('no launch')):
                with self.assertRaises(ValueError):installer.install(config)

    def test_explicit_install_copies_all_siblings_in_temp_home_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            config = installer.plan(ROOT, home, sys.executable)
            responses = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
            with patch.object(installer.platform, 'system', return_value='Darwin'), patch.object(installer.os, 'getuid', return_value=501), \
                 patch.object(installer.Path, 'home', return_value=home), patch.object(installer.subprocess, 'run', side_effect=responses) as run:
                result = installer.install(config)
            self.assertTrue(result['installed'])
            self.assertEqual(run.call_args.args[0][:3], ['/bin/launchctl', 'bootstrap', 'gui/501'])
            for relative, name in installer.COMPONENTS.items():
                copied = Path(config['install_dir']) / name
                self.assertEqual(copied.read_bytes(), (ROOT / relative).read_bytes())
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(config['plist_path']).stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
