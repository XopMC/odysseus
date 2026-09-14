import asyncio
import json
import os
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from src import engineering_hosts as hosts


class HostTests(unittest.TestCase):
    def setUp(self):
        self.item = {'name': 'Mac', 'target': 'alice@localhost', 'port': 2222,
                     'key_path': '/secret/key', 'known_hosts_path': '/secret/known',
                     'client_path': '/Users/alice/runner client.py'}
        self.env = patch.dict(os.environ, {'ODYSSEUS_HOST_ENABLED': '1', 'ODYSSEUS_HOST_OWNER': 'alice',
            'ODYSSEUS_HOST_TARGET': 'alice@192.168.50.6',
            'ODYSSEUS_EXECUTION_HOSTS': json.dumps({'mac': self.item})}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, identity='mac', owner='alice'):
        return asyncio.run(hosts.call(identity, 'runner.capabilities', {}, owner, 'scope'))

    def test_list_is_redacted_no_discovery_and_exact_owner(self):
        with patch.object(hosts.subprocess, 'run', side_effect=AssertionError('no network')):
            result = hosts.public_hosts('alice')
            self.assertEqual(hosts.public_hosts('bob'), [])
        self.assertEqual({item['id'] for item in result}, {'mac', hosts.LEGACY})
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('target', json.dumps(result))

    def test_pinned_argv_fixed_client_and_stdin_request(self):
        result = subprocess.CompletedProcess([], 0, '{"ok":true,"result":{"protocol_version":1}}', '')
        with patch.object(hosts.subprocess, 'run', return_value=result) as run:
            self.assertTrue(self.call()['ok'])
        argv = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertEqual(argv[-2:], ['alice@localhost', "python3 '/Users/alice/runner client.py'"])
        self.assertEqual(argv[argv.index('-p') + 1], '2222')
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['op'], 'runner.capabilities')

    def test_legacy_delegates_but_redacts_failure(self):
        with patch.object(hosts.team_host, 'call', AsyncMock(return_value={'ok': False, 'error': 'private-key-password'})) as call:
            result = self.call(hosts.LEGACY)
        call.assert_awaited_once()
        self.assertNotIn('private', json.dumps(result))

    def test_unknown_and_wrong_owner_do_not_connect(self):
        with patch.object(hosts.subprocess, 'run', side_effect=AssertionError('no network')):
            self.assertFalse(self.call('unknown')['ok'])
            self.assertFalse(self.call(owner='bob')['ok'])

    def test_engineering_runner_operations_reach_transport_but_unknown_ops_do_not(self):
        operations = ('workspace.digest', 'workspace.verification-copy', 'lsp.discover', 'lsp.start', 'lsp.request', 'lsp.diagnostics', 'lsp.stop')
        with patch.object(hosts.team_host, 'call', AsyncMock(return_value={'ok': True, 'result': {}})) as transport:
            for op in operations:
                result = asyncio.run(hosts.call(hosts.LEGACY, op, {}, 'alice', 'project-scope'))
                self.assertTrue(result['ok'], op)
            self.assertEqual(transport.await_count, len(operations))
            denied = asyncio.run(hosts.call(hosts.LEGACY, 'lsp.arbitrary_exec', {}, 'alice', 'project-scope'))
            self.assertFalse(denied['ok'])
            self.assertEqual(transport.await_count, len(operations))

    def test_invalid_config_rejected_without_secret_echo(self):
        for change in ({'port': True}, {'port': 65536}, {'target': '-oProxyCommand=evil'},
                       {'client_path': '/tmp/client\r.py'}, {'key_path': 'relative'},
                       {'known_hosts_path': '/secret/../unsafe'}):
            with self.subTest(change=change), patch.dict(os.environ, {'ODYSSEUS_EXECUTION_HOSTS': json.dumps({'mac': {**self.item, **change}})}):
                self.assertFalse(self.call()['ok'])

    def test_stderr_and_timeout_are_redacted(self):
        for result in (subprocess.CompletedProcess([], 1, '', 'sensitive password'),
                       subprocess.TimeoutExpired(['ssh', 'sensitive password'], 135)):
            with self.subTest(result=type(result).__name__):
                kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
                with patch.object(hosts.subprocess, 'run', **kwargs):
                    response = self.call()
                self.assertFalse(response['ok'])
                self.assertNotIn('sensitive', json.dumps(response))


if __name__ == '__main__':
    unittest.main()
