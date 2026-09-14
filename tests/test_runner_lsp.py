import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_engineering_lsp import SERVER

spec = importlib.util.spec_from_file_location('runner_lsp_test', Path(__file__).resolve().parents[1] / 'scripts/host_runner.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RunnerLSPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runner = module.Runner(self.root / 'state')
        self.addCleanup(self.runner.close)
        lsp = self.runner._load_lsp()
        # Trusted test fixture executable selection, not a model argument.
        self.discovery = patch.object(lsp, 'discover', return_value=[{
            'language': 'python', 'available': True, 'reason': None,
            'argv': [sys.executable, '-u', '-c', SERVER]}])
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def call(self, op, args=None, owner='alice', scope='task'):
        return self.runner.handle({'op': op, 'args': args or {}, 'owner': owner, 'scope': scope})

    def start(self):
        return self.call('lsp.start', {'language': 'python', 'cwd': str(self.root),
            'idempotency_key': 'start', 'execution_authorized': True})

    def test_protocol_scope_readonly_and_cleanup_after_revocation(self):
        result = self.start()
        self.assertTrue(result['ok'], result)
        identity = result['result']['id']
        self.assertEqual(identity, self.start()['result']['id'])
        args = {'id': identity, 'method': 'textDocument/hover',
                'params': {'textDocument': {'uri': (self.root / 'test.py').as_uri()}},
                'execution_authorized': True}
        self.assertTrue(self.call('lsp.request', args)['ok'])
        self.assertFalse(self.call('lsp.request', args, owner='bob')['ok'])
        self.assertFalse(self.call('lsp.request', {**args, 'method': 'workspace/applyEdit'})['ok'])
        proc = self.runner.lsp_sessions[identity]['broker'].proc
        self.assertFalse(self.call('lsp.request', {**args, 'execution_authorized': False})['ok'])
        self.assertIsNotNone(proc.poll())
        self.assertTrue(self.call('lsp.stop', {'id': identity})['ok'])
        self.assertEqual(self.start()['result']['status'], 'stopped')

    def test_explicit_authorization_uri_escape_and_discovery_redaction(self):
        with patch.object(self.runner._load_lsp(), 'Broker', side_effect=AssertionError('discovery must not spawn')):
            result = self.call('lsp.discover')
            self.assertTrue(result['ok'])
            self.assertNotIn('argv', repr(result))
            self.assertFalse(self.call('lsp.start', {'language': 'python', 'cwd': str(self.root), 'idempotency_key': 'x'})['ok'])
        identity = self.start()['result']['id']
        for uri in ('file:///etc/passwd', 'https://example.com/file.py'):
            result = self.call('lsp.request', {'id': identity, 'method': 'textDocument/hover',
                'params': {'textDocument': {'uri': uri}}, 'execution_authorized': True})
            self.assertFalse(result['ok'], result)
        (self.root / 'outside').symlink_to('/etc', target_is_directory=True)
        self.assertFalse(self.call('lsp.diagnostics', {'id': identity, 'uri': (self.root / 'outside/passwd').as_uri(), 'execution_authorized': True})['ok'])

    def test_cancel_closes_session_and_restart_does_not_replay(self):
        identity = self.start()['result']['id']
        proc = self.runner.lsp_sessions[identity]['broker'].proc
        self.assertTrue(self.call('scope.cancel')['ok'])
        self.assertIsNotNone(proc.poll())
        self.assertFalse(self.start()['ok'])
        restarted = module.Runner(self.root / 'state')
        try:
            self.assertFalse(restarted.lsp_sessions)
            self.assertNotIn(identity, (self.root / 'state/metadata.json').read_text())
        finally:
            restarted.close()

    def test_structural_outside_result_is_not_exposed(self):
        identity = self.start()['result']['id']
        broker = self.runner.lsp_sessions[identity]['broker']
        with patch.object(broker, 'request', return_value={'available': True, 'result': {'uri': 'file:///private/secret'}}):
            result = self.call('lsp.request', {'id': identity, 'method': 'textDocument/definition',
                'params': {'textDocument': {'uri': (self.root / 'test.py').as_uri()}}, 'execution_authorized': True})
        self.assertFalse(result['ok'])
        self.assertNotIn('/private/secret', repr(result))


if __name__ == '__main__':
    unittest.main()
