"""Opt-in real toolchain acceptance, not a mocked language server."""
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from src.engineering_lsp import Broker


@unittest.skipUnless(os.environ.get('ODYSSEUS_REAL_LSP_TEST') == '1' and shutil.which('clangd'), 'explicit real clangd test not enabled')
class RealClangdTests(unittest.TestCase):
    def test_cross_file_definition_and_diagnostic_repair(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'engineering'
        with tempfile.TemporaryDirectory(prefix='odysseus-clangd-') as temporary:
            root = Path(temporary).resolve()
            for source in fixtures.glob('navigation.*'):
                shutil.copyfile(source, root / source.name)
            source = root / 'navigation.cpp'
            text = source.read_text()
            with Broker([shutil.which('clangd')], root, lambda operation: True) as broker:
                broker.notify('textDocument/didOpen', {'textDocument': {'uri': source.as_uri(), 'languageId': 'cpp', 'version': 1, 'text': text}})
                position = {'line': 1, 'character': text.splitlines()[1].index('engineering_answer') + 3}
                def diagnostics(version):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        result = broker.published_diagnostics(source.as_uri()).get('result')
                        if result and result.get('version') == version:
                            return result['diagnostics']
                        time.sleep(.05)
                    self.fail('clangd did not publish versioned diagnostics')
                initial = diagnostics(1)
                self.assertTrue(any('deliberately_missing' in item['message'] for item in initial))
                broker.notify('textDocument/didChange', {'textDocument': {'uri': source.as_uri(), 'version': 2}, 'contentChanges': [{'text': text.replace(' + deliberately_missing', '')}]})
                self.assertFalse([item for item in diagnostics(2) if item.get('severity') == 1])
                definition = broker.request('textDocument/definition', {'textDocument': {'uri': source.as_uri()}, 'position': position})
                self.assertTrue(definition['available'])
                self.assertIn('navigation.hpp', str(definition['result']))


if __name__ == '__main__':
    unittest.main()
