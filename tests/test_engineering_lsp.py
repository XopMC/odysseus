import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from src.engineering_lsp import Broker, _frame, discover

SERVER = r'''
import sys,json,time
def send(value):
 b=json.dumps(value).encode();sys.stdout.buffer.write(('Content-Length: %d\r\n\r\n'%len(b)).encode()+b);sys.stdout.buffer.flush()
while True:
 line=sys.stdin.buffer.readline()
 if not line:break
 n=int(line.split(b':')[1]);sys.stdin.buffer.readline();m=json.loads(sys.stdin.buffer.read(n));method=m.get('method')
 if method=='exit':break
 if 'id' not in m:continue
 if method=='initialize':
  send({'jsonrpc':'2.0','method':'window/logMessage','params':{'message':'notification before response'}})
  send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{'uri':'file:///fixture.py','diagnostics':[]}})
  value={'capabilities':{'hoverProvider':True,'definitionProvider':True,'referencesProvider':True,'documentSymbolProvider':True}}
 elif method=='shutdown':value=None
 elif m.get('params',{}).get('timeout'):time.sleep(10);continue
 elif m.get('params',{}).get('malformed'):
  sys.stdout.buffer.write(b'Content-Length: 999999999\r\n\r\n');sys.stdout.buffer.flush();continue
 else:value={'method':method}
 send({'jsonrpc':'2.0','id':m['id'],'result':value})
'''


class LSPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.granted = True
        self.operations = []
        def authorize(operation):
            self.operations.append(operation)
            return self.granted
        # Explicit test-only authorization grants execution of this protocol
        # fixture, never project code or an installed language server.
        self.broker = Broker([sys.executable, '-u', '-c', SERVER], self.temp.name, authorize, timeout=.5)
        self.addCleanup(self.broker.close)

    def test_protocol_matching_notifications_capabilities_and_shutdown(self):
        self.broker.start()
        for method in ('hover', 'definition', 'references', 'documentSymbol'):
            result = self.broker.request('textDocument/' + method, {})
            self.assertEqual(result['result']['method'], 'textDocument/' + method)
        self.assertTrue(self.broker.published_diagnostics('file:///fixture.py')['available'])
        self.assertFalse(self.broker.request('textDocument/diagnostic', {})['available'])
        proc = self.broker.proc
        self.broker.shutdown()
        self.assertIsNotNone(proc.poll())
        self.assertIn('spawn', self.operations)
        self.assertIn('initialize', self.operations)

    def test_revoke_before_spawn_or_request(self):
        self.granted = False
        with self.assertRaises(PermissionError):self.broker.start()
        self.assertIsNone(self.broker.proc)
        self.granted = True
        self.broker.start()
        proc = self.broker.proc
        self.granted = False
        with self.assertRaises(PermissionError):self.broker.request('textDocument/hover', {})
        self.assertIsNotNone(proc.poll())

    def test_timeout_and_malformed_length_terminate_process(self):
        for params in ({'timeout': True}, {'malformed': True}):
            with self.subTest(params=params):
                broker = Broker([sys.executable, '-u', '-c', SERVER], self.temp.name, lambda _: True, timeout=.1)
                broker.start();proc=broker.proc
                with self.assertRaises((RuntimeError, TimeoutError, __import__('queue').Empty)):
                    broker.request('textDocument/hover', params)
                self.assertIsNotNone(proc.poll())
                broker.close()

    def test_readonly_and_discovery_does_not_spawn(self):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('no spawn')):
            entries = discover()
        self.assertFalse(next(e for e in entries if e['language'] == 'metal')['available'])
        self.broker.start()
        with self.assertRaises(PermissionError):self.broker.request('workspace/applyEdit', {})

    def test_frame_rejects_duplicate_length_and_truncated_payload(self):
        for payload in (b'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}',
                        b'Content-Length: 100\r\n\r\n{}'):
            with self.assertRaises((ValueError, EOFError)):_frame(io.BytesIO(payload))


if __name__ == '__main__':
    unittest.main()
