import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('capability_runner_test', Path(__file__).resolve().parents[1] / 'scripts/host_runner.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CapabilityTests(unittest.TestCase):
    def test_real_local_handshake_and_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = module.Runner(directory)
            try:
                request = {'owner': 'test', 'scope': 'test', 'op': 'runner.capabilities', 'args': {}}
                before = (Path(directory) / 'metadata.json').read_bytes()
                result = runner.handle(request)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['result']['protocol_version'], 1)
                self.assertIn('terminal.create', result['result']['supported_ops'])
                self.assertEqual(before, (Path(directory) / 'metadata.json').read_bytes())
                self.assertTrue(runner.handle({**request, 'op': 'resource.snapshot'})['ok'])
            finally:
                runner.close()

    def test_reboot_and_unknown_boot_never_replay_jobs(self):
        for boot in ('new-boot', None):
            with self.subTest(boot=boot), tempfile.TemporaryDirectory() as directory:
                state = {'jobs': {'job': {'status': 'running', 'owner': 'test', 'scope': 'test'}},
                         'worktrees': {}, 'checkpoints': {}, 'platform': {'boot_id': 'old-boot'}}
                (Path(directory) / 'metadata.json').write_text(json.dumps(state))
                facts = {'os': 'darwin', 'arch': 'arm64', 'release': 'test', 'boot_id': boot}
                with patch.object(module.runner_platform, 'identity', return_value=facts):
                    runner = module.Runner(directory)
                try:
                    job = runner.data['jobs']['job']
                    self.assertEqual(job['status'], 'interrupted')
                    self.assertFalse(runner.processes)
                    self.assertIn('rebooted' if boot else 'restarted', job['reason'])
                    self.assertEqual(runner.data['platform'], facts)
                finally:
                    runner.close()


if __name__ == '__main__':
    unittest.main()
