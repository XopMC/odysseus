import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
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
        read = self.call('read_file', {'path': str(path), 'offset': 2, 'limit': 1})
        self.assertEqual(read['output'], 'two\n')
        self.assertEqual(self.call('edit_file', {'path': str(path), 'old_string': 'two', 'new_string': 'three',
                                                 'expected_sha256': read['sha256']})['exit_code'], 0)
        self.assertEqual(path.read_text(), 'one\nthree\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o751)

    def test_host_edit_rejects_stale_hash_and_preserves_original(self):
        path = self.root / 'stale.py'
        path.write_text('def f():\n    return 1\n')
        result = self.call('edit_file', {
            'path': str(path), 'old_string': 'return 1', 'new_string': 'return 2',
            'expected_sha256': '0' * 64,
        })
        self.assertEqual(result['code'], 'stale_revision')
        self.assertEqual(path.read_text(), 'def f():\n    return 1\n')

    def test_host_edit_requires_hash_precondition(self):
        path = self.root / 'no-hash.txt'
        path.write_text('before\n')
        result = self.call('edit_file', {'path': str(path), 'old_string': 'before', 'new_string': 'after'})
        self.assertEqual(result['code'], 'precondition_required')
        self.assertEqual(path.read_text(), 'before\n')

    def test_host_patch_requires_hash_for_every_path(self):
        path = self.root / 'no-hash.txt'
        original = 'before\n'
        path.write_text(original)
        patch = f'*** Begin Patch\n*** Update File: {path}\n@@\n-before\n+after\n*** End Patch'
        result = self.call('apply_patch', {'patch_text': patch})
        self.assertEqual(result['code'], 'precondition_required')
        self.assertEqual(path.read_text(), original)

    def test_host_edit_syntax_gate_preserves_original(self):
        path = self.root / 'syntax.py'
        original = 'def f():\n    return 1\n'
        path.write_text(original)
        import hashlib
        result = self.call('edit_file', {
            'path': str(path), 'old_string': 'return 1', 'new_string': 'return )',
            'expected_sha256': hashlib.sha256(original.encode()).hexdigest(),
        })
        self.assertEqual(result['code'], 'syntax_error')
        self.assertEqual(path.read_text(), original)

    def test_host_edit_cannot_disable_required_syntax_gate(self):
        path = self.root / 'syntax-disabled.py'
        original = 'def f():\n    return 1\n'
        path.write_text(original)
        result = self.call('edit_file', {
            'path': str(path), 'old_string': 'return 1', 'new_string': 'return )',
            'expected_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'validate_syntax': False,
        })
        self.assertEqual(result['code'], 'validation_required')
        self.assertEqual(path.read_text(), original)

    def test_host_patch_preflights_hashes_and_syntax_before_any_write(self):
        import hashlib
        first, second = self.root / 'first.txt', self.root / 'settings.json'
        first_raw, second_raw = b'old\n', b'{"ok": true}\n'
        first.write_bytes(first_raw)
        second.write_bytes(second_raw)
        patch = (
            '*** Begin Patch\n'
            f'*** Update File: {first}\n@@\n-old\n+new\n'
            f'*** Update File: {second}\n@@\n-{{"ok": true}}\n+{{"ok":\n'
            '*** End Patch'
        )
        result = self.call('apply_patch', {
            'patch_text': patch,
            'expected_sha256_by_path': {
                str(first): hashlib.sha256(first_raw).hexdigest(),
                str(second): hashlib.sha256(second_raw).hexdigest(),
            },
        })
        self.assertEqual(result['code'], 'syntax_error')
        self.assertEqual(first.read_bytes(), first_raw)
        self.assertEqual(second.read_bytes(), second_raw)

    def test_host_patch_io_failure_rolls_back_prior_file(self):
        first, second = self.root / 'first.txt', self.root / 'second.txt'
        first_raw, second_raw = b'old-a\n', b'old-b\n'
        first.write_bytes(first_raw)
        second.write_bytes(second_raw)
        patch = (
            '*** Begin Patch\n'
            f'*** Update File: {first}\n@@\n-old-a\n+new-a\n'
            f'*** Update File: {second}\n@@\n-old-b\n+new-b\n'
            '*** End Patch'
        )
        real_write = self.rpc._write
        calls = 0

        def fail_second(path, text, previous):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected host replace failure')
            return real_write(path, text, previous)

        self.rpc._write = fail_second
        result = self.call('apply_patch', {
            'patch_text': patch,
            'expected_sha256_by_path': {
                str(first): hashlib.sha256(first_raw).hexdigest(),
                str(second): hashlib.sha256(second_raw).hexdigest(),
            },
        })
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['code'], 'patch_commit_failed')
        self.assertEqual(first.read_bytes(), first_raw)
        self.assertEqual(second.read_bytes(), second_raw)

    def test_read_file_v2_metadata_line_numbers_and_binary(self):
        import hashlib
        path = self.root / 'read-v2.txt'
        raw = b'one\ntwo\nthree\n'
        path.write_bytes(raw)
        result = self.call('read_file', {'path': str(path), 'offset': 2,
                                         'limit': 1, 'line_numbers': True})
        self.assertEqual(result['output'], '2: two\n')
        self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['size_bytes'], len(raw))
        self.assertEqual(result['encoding'], 'utf-8')
        self.assertFalse(result['is_binary'])
        chunk = self.call('read_file', {'path': str(path), 'byte_offset': 4,
                                        'byte_limit': 3})
        self.assertEqual(chunk['output'], 'two')
        self.assertEqual(chunk['byte_range'], [4, 7])
        beyond = self.call('read_file', {'path': str(path), 'byte_offset': 100})
        self.assertEqual(beyond['byte_range'], [len(raw), len(raw)])
        self.assertEqual(beyond['output'], '')
        self.assertEqual(self.call('read_file', {'path': str(path), 'byte_offset': 1,
                                                 'offset': 2})['exit_code'], 1)
        binary = self.root / 'read-v2.bin'
        binary.write_bytes(b'a\x00b')
        result = self.call('read_file', {'path': str(binary)})
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['is_binary'])
        self.assertNotIn('\x00', result['output'])

    def test_private_read_file_chunk_is_bounded_and_exact(self):
        import base64
        import hashlib
        path = self.root / 'large.txt'
        raw = b'a' * 300_000
        path.write_bytes(raw)
        first = self.call('read_file_chunk', {'path': str(path), 'byte_offset': 0})
        self.assertEqual(first['exit_code'], 0)
        self.assertEqual(first['next_offset'], 262_144)
        self.assertEqual(base64.b64decode(first['data_b64']), raw[:262_144])
        self.assertEqual(first['sha256'], hashlib.sha256(raw).hexdigest())
        second = self.call('read_file_chunk', {'path': str(path), 'byte_offset': first['next_offset']})
        self.assertEqual(base64.b64decode(second['data_b64']), raw[262_144:])
        self.assertEqual(self.call('read_file_chunk', {'path': str(path), 'byte_offset': -1})['exit_code'], 1)

    def test_host_compare_and_verify_hashes_are_read_only_and_exact(self):
        import hashlib
        left = self.root / 'left.txt'
        right = self.root / 'right.txt'
        left.write_bytes(b'one\r\ntwo\r\n')
        right.write_bytes(b'one\ntwo\n')
        compared = self.call('compare_files', {'before': str(left), 'after': str(right)})
        self.assertEqual(compared['exit_code'], 0)
        self.assertFalse(compared['identical'])
        self.assertTrue(compared['normalized_equal'])
        self.assertEqual(compared['before_sha256'], hashlib.sha256(left.read_bytes()).hexdigest())
        asserted = self.call('verify_hashes', {'files': [{
            'path': str(right), 'sha256': hashlib.sha256(right.read_bytes()).hexdigest()}]})
        self.assertEqual(asserted['exit_code'], 0)
        self.assertTrue(asserted['verified'])
        wrong = self.call('verify_hashes', {'files': [{'path': str(right), 'sha256': '0' * 64}]})
        self.assertEqual(wrong['code'], 'hash_mismatch')
        self.assertEqual(wrong['exit_code'], 1)

    def test_host_comparison_guard_checks_every_path(self):
        left = self.root / 'safe.txt'
        left.write_text('safe')
        forbidden = self.root / '.env'
        forbidden.write_text('private')
        guard = {'cwd': str(self.root)}
        result = self.rpc.handle('compare_files', {'before': str(left), 'after': str(forbidden)},
                                 str(self.root), path_guard=guard)
        self.assertEqual(result['exit_code'], 1)
        result = self.rpc.handle('verify_hashes', {'files': [{'path': str(forbidden), 'sha256': '0' * 64}]},
                                 str(self.root), path_guard=guard)
        self.assertEqual(result['exit_code'], 1)

    def test_team_host_toolchain_uses_fixed_inventory_and_rejects_url(self):
        guard = {'cwd': str(self.root)}
        result = self.rpc.handle('inspect_toolchain', {}, str(self.root), path_guard=guard)
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('python', result['tools'])
        self.assertEqual(result['network']['status'], 'not_checked')
        denied = self.rpc.handle('inspect_toolchain', {'url': 'http://127.0.0.1'},
                                 str(self.root), path_guard=guard)
        self.assertEqual(denied['exit_code'], 1)

    def test_search_files_pages_distinct_host_paths(self):
        for number in range(5):
            (self.root / f'search-{number}.txt').write_text('needle\nneedle\n')
        first = self.call('search_files', {'pattern': 'needle', 'path': str(self.root),
                                           'page_size': 2})
        self.assertEqual(first['exit_code'], 0)
        self.assertEqual(len(first['files']), 2)
        self.assertEqual(first['next_cursor'], 2)
        self.assertNotIn(':1:needle', first['output'])
        second = self.call('search_files', {'pattern': 'needle', 'path': str(self.root),
                                            'page_size': 2, 'cursor': 2})
        third = self.call('search_files', {'pattern': 'needle', 'path': str(self.root),
                                           'page_size': 2, 'cursor': 4})
        paths = first['files'] + second['files'] + third['files']
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(set(paths)), 5)
        self.assertIsNone(third['next_cursor'])
        lines = self.call('search_files', {'pattern': 'needle', 'path': str(self.root / 'search-0.txt'),
                                           'mode': 'matches', 'page_size': 1})
        self.assertIn(':1:needle', lines['output'])
        self.assertEqual(lines['next_cursor'], 1)
        self.assertEqual(self.call('search_files', {'pattern': 'needle', 'cursor': -1})['exit_code'], 1)

    def test_host_tree_and_python_outline_are_bounded(self):
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'module.py').write_text('class Thing:\n    def run(self):\n        return 1\n')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules' / 'hidden.py').write_text('secret')
        tree = self.call('list_tree', {'path': str(self.root), 'max_depth': 2})
        self.assertEqual(tree['exit_code'], 0)
        self.assertEqual(self.call('list_tree', {})['exit_code'], 0)
        paths = [entry['path'] for entry in tree['entries']]
        self.assertIn('sub/module.py', paths)
        self.assertNotIn('node_modules/hidden.py', paths)
        self.assertNotIn('return 1', tree['output'])
        symbols = self.call('file_outline', {'path': str(self.root / 'sub' / 'module.py')})
        self.assertEqual(symbols['exit_code'], 0)
        self.assertEqual([(item['kind'], item['name'], item['line']) for item in symbols['symbols']],
                         [('class', 'Thing', 1), ('method', 'Thing.run', 2)])
        self.assertNotIn('return 1', symbols['output'])
        self.assertEqual(self.call('file_outline', {'path': str(self.root / 'sub')})['exit_code'], 1)
        (self.root / 'sub' / 'broken.py').write_text('def broken(:\n')
        self.assertEqual(self.call('file_outline', {'path': str(self.root / 'sub' / 'broken.py')})['exit_code'], 1)

    def test_host_tree_honors_gitignore(self):
        import subprocess
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.gitignore').write_text('ignored.txt\n')
        (self.root / 'ignored.txt').write_text('hidden')
        (self.root / 'kept.txt').write_text('shown')
        result = self.call('list_tree', {'path': str(self.root)})
        paths = {entry['path'] for entry in result['entries']}
        self.assertIn('kept.txt', paths)
        self.assertNotIn('ignored.txt', paths)

    def test_host_git_tools_are_bounded_and_respect_model_path_guard(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.test', 'add', '--all'], check=True)
        (self.root / 'a.txt').write_text('old\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'a.txt'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'initial'], check=True)
        (self.root / 'a.txt').write_text('new\n')
        (self.root / 'new.txt').write_text('untracked\n')
        (self.root / '.env').write_text('SECRET=hidden\n')
        guard = {'cwd': str(self.root), 'write_scope': []}

        def call(tool, args):
            return self.rpc.handle(tool, args, str(self.root), path_guard=guard)

        status = call('git_status', {})
        self.assertEqual(status['exit_code'], 0)
        self.assertIn('new.txt', [entry['path'] for entry in status['files']])
        self.assertNotIn('.env', [entry['path'] for entry in status['files']])
        diff = call('git_diff', {'file': 'a.txt'})
        self.assertEqual(diff['exit_code'], 0)
        self.assertIn('+new', diff['patch'])
        self.assertEqual(len(diff['before_hash']), 40)
        self.assertEqual(len(diff['after_hash']), 40)
        log = call('git_log', {'limit': 1})
        self.assertEqual(log['exit_code'], 0)
        self.assertEqual(log['commits'][0]['subject'], 'initial')
        self.assertEqual(call('git_diff', {'file': '.env'})['exit_code'], 1)
        self.assertEqual(call('git_log', {'limit': 21})['exit_code'], 1)

    def test_ambiguous_edit_and_missing_content_do_not_destroy(self):
        path = self.root / 'a'
        path.write_text('aa aa')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.call('edit_file', {'path': 'a', 'old_string': 'aa', 'new_string': 'b',
                                                 'expected_sha256': digest})['exit_code'], 1)
        self.assertEqual(self.call('write_file', {'path': 'a'})['exit_code'], 1)
        self.assertEqual(path.read_text(), 'aa aa')
        self.assertEqual(self.call('edit_file', {'path': 'a', 'old_string': 'aa', 'new_string': 'b',
                                                 'replace_all': True, 'expected_sha256': digest})['exit_code'], 0)
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
        original_hash = hashlib.sha256((self.root / 'a').read_bytes()).hexdigest()
        bad_args = {'patch_text': bad, 'expected_sha256_by_path': {'new': 'missing', 'a': original_hash}}
        self.assertEqual(self.call('apply_patch', bad_args)['exit_code'], 1)
        self.assertFalse((self.root / 'new').exists())
        good = bad.replace('-foo\n', '-foobar\n')
        good_args = {'patch_text': good, 'expected_sha256_by_path': {'new': 'missing', 'a': original_hash}}
        self.assertEqual(self.call('apply_patch', good_args)['exit_code'], 0)
        self.assertEqual((self.root / 'a').read_text(), 'bar\n')
        self.assertEqual((self.root / 'new').read_text(), 'x\n')

    def test_patch_delete_and_existing_add_guard(self):
        (self.root / 'a').write_text('a')
        patch = '*** Begin Patch\n*** Add File: a\n+b\n*** End Patch'
        self.assertEqual(self.call('apply_patch', {'patch_text': patch,
                                                  'expected_sha256_by_path': {'a': 'missing'}})['exit_code'], 1)
        self.assertEqual((self.root / 'a').read_text(), 'a')
        patch = '*** Begin Patch\n*** Delete File: a\n*** End Patch'
        self.assertEqual(self.call('apply_patch', {'patch': patch,
                                                  'expected_sha256_by_path': {'a': hashlib.sha256(b'a').hexdigest()}})['exit_code'], 0)
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
