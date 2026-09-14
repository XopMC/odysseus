import asyncio
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from src import team_host


class RunnerTransportTests(unittest.TestCase):
    def test_wrong_owner_never_opens_ssh(self):
        with patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice'}):
            with patch.object(team_host.subprocess, 'run') as command:
                result = asyncio.run(team_host.call('terminal.list', {}, 'bob', 'task'))
                self.assertFalse(result['ok'])
                command.assert_not_called()

    def test_fixed_client_command_and_payload_on_stdin(self):
        env = {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice',
               'ODYSSEUS_HOST_TARGET': 'xopmc@192.168.50.6',
               'ODYSSEUS_HOST_RUNNER_CLIENT': '/home/xopmc/fixed client.py'}
        with patch.dict(os.environ, env):
            response = subprocess.CompletedProcess([], 0, '{"ok":true,"result":{"jobs":[]}}', '')
            with patch.object(team_host.subprocess, 'run', return_value=response) as command:
                data = 'printf "$(this stays remote content)"'
                result = asyncio.run(team_host.call('command.start', {'command': data}, 'alice', 'task'))
                self.assertTrue(result['ok'])
                argv = command.call_args.args[0]
                self.assertEqual(argv[-1], "python3 '/home/xopmc/fixed client.py'")
                self.assertIn('StrictHostKeyChecking=yes', argv)
                request = json.loads(command.call_args.kwargs['input'])
                self.assertEqual(request['args']['command'], data)
                self.assertEqual(request['owner'], 'alice')

    def test_timeout_is_explicit_failure(self):
        with patch.object(team_host, '_call', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            result = asyncio.run(team_host.call('terminal.list', {}, 'alice', 'task'))
            self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
