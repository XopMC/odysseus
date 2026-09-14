import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest


class HostFilesTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('host_files_test', Path(__file__).resolve().parents[1] / 'scripts/host_files.py')
        self.rpc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.rpc)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def call(self, tool, content):
        return self.rpc.handle(tool, content, str(self.root))

    def test_create_read_edit_and_preserve_mode(self):
        self.assertEqual(self.call('write_file', {'path': 'a/x.py', 'content': 'one\ntwo\n'})['exit_code'], 0)
        path = self.root / 'a/x.py'
        path.chmod(0o751)
        self.assertEqual(self.call('read_file', {'path': str(path), 'offset': 2, 'limit': 1})['output'], 'two\n')
        self.assertEqual(self.call('edit_file', {'path': str(path), 'old_string': 'two', 'new_string': 'three'})['exit_code'], 0)
        self.assertEqual(path.read_text(), 'one\nthree\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o751)

    def test_ambiguous_edit_and_missing_content_do_not_destroy(self):
        path = self.root / 'a'
        path.write_text('aa aa')
        self.assertEqual(self.call('edit_file', {'path': 'a', 'old_string': 'aa', 'new_string': 'b'})['exit_code'], 1)
        self.assertEqual(self.call('write_file', {'path': 'a'})['exit_code'], 1)
        self.assertEqual(path.read_text(), 'aa aa')
        self.assertEqual(self.call('edit_file', {'path': 'a', 'old_string': 'aa', 'new_string': 'b', 'replace_all': True})['exit_code'], 0)
        self.assertEqual(path.read_text(), 'b b')

    def test_special_files_rejected_without_blocking(self):
        os.mkfifo(self.root / 'fifo')
        self.assertEqual(self.call('read_file', 'fifo')['exit_code'], 1)
        self.assertEqual(self.call('write_file', {'path': 'fifo', 'content': 'x'})['exit_code'], 1)
        self.assertEqual(self.call('read_file', '/dev/zero')['exit_code'], 1)

    def test_links_readable_but_not_replaceable(self):
        path = self.root / 'a'
        path.write_text('old')
        (self.root / 'link').symlink_to(path)
        self.assertEqual(self.call('read_file', 'link')['output'], 'old')
        self.assertEqual(self.call('write_file', {'path': 'link', 'content': 'new'})['exit_code'], 1)
        os.link(path, self.root / 'hard')
        self.assertEqual(self.call('write_file', {'path': 'hard', 'content': 'new'})['exit_code'], 1)
        self.assertEqual(path.read_text(), 'old')

    def test_patch_prevalidates_all_paths_and_full_line_context(self):
        (self.root / 'a').write_text('foobar\n')
        bad = '*** Begin Patch\n*** Add File: new\n+x\n*** Update File: a\n@@\n-foo\n+bar\n*** End Patch'
        self.assertEqual(self.call('apply_patch', bad)['exit_code'], 1)
        self.assertFalse((self.root / 'new').exists())
        good = bad.replace('-foo\n', '-foobar\n')
        self.assertEqual(self.call('apply_patch', good)['exit_code'], 0)
        self.assertEqual((self.root / 'a').read_text(), 'bar\n')
        self.assertEqual((self.root / 'new').read_text(), 'x\n')

    def test_patch_delete_and_existing_add_guard(self):
        (self.root / 'a').write_text('a')
        patch = '*** Begin Patch\n*** Add File: a\n+b\n*** End Patch'
        self.assertEqual(self.call('apply_patch', {'patch_text': patch})['exit_code'], 1)
        self.assertEqual((self.root / 'a').read_text(), 'a')
        patch = '*** Begin Patch\n*** Delete File: a\n*** End Patch'
        self.assertEqual(self.call('apply_patch', {'patch': patch})['exit_code'], 0)
        self.assertFalse((self.root / 'a').exists())

    def test_search_real_subprocess_hidden_files_and_caps(self):
        (self.root / '.hidden.py').write_text('Token\nToken\n')
        (self.root / 'x.txt').write_text('irrelevant')
        found = self.call('grep', {'pattern': 'token', 'ignore_case': True, 'max_results': 1})
        self.assertEqual(found['exit_code'], 0)
        self.assertIn('.hidden.py:1:Token', found['output'])
        self.assertTrue(found['truncated'])
        self.assertIn('.hidden.py', self.call('glob', {'pattern': '**/*.py'})['output'])
        self.assertEqual(self.call('grep', {'pattern': '['})['exit_code'], 1)
        self.assertIn('.hidden.py', self.call('ls', {})['output'])

    def test_search_does_not_follow_directory_links(self):
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'loop').symlink_to(self.root, target_is_directory=True)
        (self.root / 'sub' / 'x').write_text('needle')
        result = self.call('grep', {'pattern': 'needle'})
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['output'].count(':1:needle'), 1)

    def test_bounds_and_malformed_arguments(self):
        (self.root / 'large').write_bytes(b'x' * (self.rpc.MAX_FILE + 1))
        self.assertEqual(self.call('read_file', 'large')['exit_code'], 1)
        self.assertEqual(self.call('write_file', {'path': 'x', 'content': 'x' * (self.rpc.MAX_FILE + 1)})['exit_code'], 1)
        self.assertFalse((self.root / 'x').exists())
        self.assertEqual(self.call('read_file', '{bad')['exit_code'], 1)
        self.assertEqual(self.call('read_file', {'path': 'x\0y'})['exit_code'], 1)
        self.assertIn('Host filesystem', self.call('get_workspace', {})['output'])

    def test_atomic_write_checks_concurrent_change(self):
        path = self.root / 'a'
        path.write_text('original')
        _, info = self.rpc._read(str(path))
        path.write_text('other writer')
        with self.assertRaises(ValueError):
            self.rpc._write(str(path), 'clobber', info)
        self.assertEqual(path.read_text(), 'other writer')


if __name__ == '__main__':
    unittest.main()
