import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runner_platform_test', Path(__file__).resolve().parents[1] / 'scripts/runner_platform.py')
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


class PlatformTests(unittest.TestCase):
    def test_darwin_boot_and_memory_are_system_facts(self):
        with patch.object(rp.platform, 'system', return_value='Darwin'), patch.object(rp, 'sysctl', return_value='{ sec = 1234, usec = 567 }'):
            self.assertEqual(rp.identity()['boot_id'], 'darwin:1234:567')
        with patch.object(rp, 'sysctl', return_value='68719476736'):
            result = rp.non_linux_snapshot('darwin', 2)
        self.assertEqual(result['memory']['MemTotal'], 68719476736)
        self.assertIsNone(result['gpu_percent'])
        self.assertIsNone(result['memory']['MemAvailable'])
        self.assertEqual(result['running_jobs'], 2)

    def test_missing_macos_metrics_remain_unknown(self):
        with patch.object(rp.platform, 'system', return_value='Darwin'), patch.object(rp, 'sysctl', return_value=None):
            self.assertIsNone(rp.identity()['boot_id'])
            self.assertIsNone(rp.non_linux_snapshot('darwin', 0)['memory']['MemTotal'])

    def test_discovery_does_not_execute_toolchains(self):
        facts = {'os': 'darwin', 'arch': 'arm64', 'release': 'test', 'boot_id': None}
        with patch.object(rp.shutil, 'which', return_value=None), patch.object(rp.subprocess, 'run', side_effect=AssertionError('must not execute')):
            result = rp.capabilities(facts)
        self.assertEqual(result['protocol_version'], 1)
        self.assertTrue(all(value is None for value in result['toolchains'].values()))


if __name__ == '__main__':
    unittest.main()
