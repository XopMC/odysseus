import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class HostExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('host_transport_test', self.root / 'src/host_execution.py')
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def helper(self, tool, content, timeout=3):
        result = subprocess.run([sys.executable, str(self.root / 'scripts/host_exec.py')],
                                input=json.dumps({'tool': tool, 'content': content,
                                                  'cwd': self.tmp.name, 'timeout': timeout}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_disabled_and_exact_owner_only(self):
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'xopmc'}):
            self.assertTrue(self.host.enabled_for('xopmc'))
            for other in ('XopMC', '', None, 'guest'):
                self.assertFalse(self.host.enabled_for(other))
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '0', 'ODYSSEUS_HOST_OWNER': 'xopmc'}):
            self.assertFalse(self.host.enabled_for('xopmc'))

    def test_host_schema_scope_is_truthful_and_not_global(self):
        schemas = [{'type': 'function', 'function': {'name': 'get_workspace', 'description': 'File tools are confined to it', 'parameters': {}}}]
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'xopmc'}):
            changed = self.host.adapt_schemas(schemas, 'xopmc')
            self.assertIn('NOT a filesystem boundary', changed[0]['function']['description'])
            self.assertEqual(schemas[0]['function']['description'], 'File tools are confined to it')
            self.assertIs(self.host.adapt_schemas(schemas, 'other'), schemas)

    def test_strict_ssh_argv_and_content_never_remote_command(self):
        with patch.dict(os.environ, {'ODYSSEUS_HOST_TARGET': 'xopmc@192.168.50.6',
                                     'ODYSSEUS_HOST_HELPER': '/home/xopmc/a b/helper.py'}):
            argv = self.host.ssh_argv()
            self.assertIn('StrictHostKeyChecking=yes', argv)
            self.assertIn('BatchMode=yes', argv)
            self.assertEqual(argv[-1], "python3 '/home/xopmc/a b/helper.py'")
            payload = 'echo "$(touch /tmp/not-run)"; exit 7'
            request = self.host.request_for('bash', payload)
            with patch.object(self.host.subprocess, 'run', return_value=subprocess.CompletedProcess(argv, 0, '{"exit_code":0,"output":"ok"}', '')) as run:
                self.assertEqual(self.host.run_request(request)['output'], 'ok')
                self.assertNotIn(payload, run.call_args.args[0])
                self.assertEqual(json.loads(run.call_args.kwargs['input'])['content'], payload)
        with patch.dict(os.environ, {'ODYSSEUS_HOST_TARGET': '-oProxyCommand=oops'}):
            with self.assertRaises(ValueError):
                self.host.ssh_argv()

    def test_timeout_is_reported_and_sudo_blocked(self):
        with patch.object(self.host, 'run_request', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            result = asyncio.run(self.host.execute('bash', 'true'))
            self.assertEqual(result['exit_code'], 1)
        with self.assertRaisesRegex(ValueError, '/host-access'):
            self.host.request_for('bash', 'sudo apt update')
        self.assertEqual(self.helper('bash', 'sudo true')['exit_code'], 1)
        with self.assertRaisesRegex(ValueError, 'must be a string'):
            self.host.request_for('bash', {'command': 'true'})

    def test_real_host_shell_python_and_file_seam(self):
        self.assertEqual(self.helper('bash', 'printf hello')['output'], 'hello')
        self.assertEqual(self.helper('python', 'print(6 * 7)')['output'], '42\n')
        self.assertEqual(self.helper('write_file', {'path': 'a', 'content': 'host data'})['exit_code'], 0)
        self.assertEqual((Path(self.tmp.name) / 'a').read_text(), 'host data')
        self.assertEqual(self.helper('read_file', {'path': 'a'})['output'], 'host data')
        self.assertEqual(self.helper('bash', 'exit 9')['exit_code'], 9)

    def test_real_output_cap_and_timeout(self):
        output = self.helper('python', 'print("x" * 100000)')
        self.assertTrue(output['truncated'])
        self.assertLess(len(output['output']), 60200)
        timed = self.helper('bash', 'sleep 20', timeout=1)
        self.assertTrue(timed['timed_out'])
        self.assertEqual(timed['exit_code'], 124)

    def test_background_command_encodes_not_interpolates(self):
        content = 'echo "quoted"; $(not_executed_locally)'
        argv = shlex.split(self.host.background_command('bash', content))
        self.assertEqual(argv[-2], '--background')
        request = json.loads(base64.urlsafe_b64decode(argv[-1]))
        self.assertEqual(request['content'], content)
        self.assertEqual(request['timeout'], 3500)

    def test_channel_close_terminates_host_child(self):
        process = subprocess.Popen([sys.executable, str(self.root / 'scripts/host_exec.py')],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        try:
            process.stdin.write(json.dumps({'tool': 'bash', 'content': 'sleep 30',
                                            'cwd': self.tmp.name, 'timeout': 30}))
            process.stdin.close()
            process.stdout.close()
            self.assertNotEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    unittest.main()
