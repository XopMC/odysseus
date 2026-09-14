import os
from pathlib import Path
import tempfile
import unittest

from src.team_tool_paths import normalize_file_args


class TeamToolPathTests(unittest.TestCase):
    def test_normalizes_without_mutation_and_allows_ordinary_host_read(self):
        args = {'path': 'src/../file.py', 'content': 'x'}
        result = normalize_file_args('write_file', args, '/worker')
        self.assertEqual(result['path'], '/worker/file.py')
        self.assertEqual(args['path'], 'src/../file.py')
        self.assertEqual(normalize_file_args('read_file', {'path': '/etc/os-release'}, '/worker')['path'], '/etc/os-release')

    def test_write_checkout_and_scope_boundaries(self):
        for path in ('../outside', '/worker-other/file', '/etc/config'):
            with self.assertRaises(PermissionError):
                normalize_file_args('write_file', {'path': path}, '/worker')
        self.assertEqual(normalize_file_args('write_file', {'path': 'src/a.py'}, '/worker', ['src'])['path'], '/worker/src/a.py')
        for path in ('tests/test.py', 'src-other/a.py'):
            with self.assertRaises(PermissionError):
                normalize_file_args('write_file', {'path': path}, '/worker', ['src'])
        with self.assertRaises(PermissionError):
            normalize_file_args('write_file', {'path': 'a'}, '/worker', [])

    def test_glob_scope_single_star_does_not_cross_directories(self):
        normalize_file_args('edit_file', {'path': 'src/a.py'}, '/worker', ['src/*.py'])
        with self.assertRaises(PermissionError):
            normalize_file_args('edit_file', {'path': 'src/nested/a.py'}, '/worker', ['src/*.py'])
        normalize_file_args('edit_file', {'path': 'src/nested/a.py'}, '/worker', ['src/**/*.py'])

    def test_known_credentials_denied_for_reads_and_writes(self):
        paths = ['/home/x/.ssh/id_ed25519', '/home/x/.aws/credentials', '/home/x/.config/gcloud/token',
                 '/home/x/.kube/config', '/project/.env', '/project/credentials.json',
                 '/home/x/services/odysseus/data/settings.json', '/app/data/app.db', '/project/.app_key']
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(PermissionError):
                    normalize_file_args('read_file', {'path': path}, '/project')
        normalize_file_args('read_file', {'path': '/project/settings.py'}, '/project')

    def test_patch_validates_every_operation_before_return(self):
        text = '*** Begin Patch\n*** Add File: src/a.py\n+x\n*** Delete File: ../outside\n*** End Patch'
        with self.assertRaises(PermissionError):
            normalize_file_args('apply_patch', {'patch_text': text}, '/worker')
        good = text.replace('../outside', 'src/old.py')
        result = normalize_file_args('apply_patch', {'patch_text': good}, '/worker', ['src'])
        self.assertIn('*** Add File: /worker/src/a.py', result['patch_text'])
        self.assertIn('*** Delete File: /worker/src/old.py', result['patch_text'])

    def test_host_realpath_guard_rejects_outside_and_secret_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worker = root / 'worker'
            worker.mkdir()
            outside = root / 'outside'
            outside.mkdir()
            (worker / 'link').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(PermissionError):
                normalize_file_args('write_file', {'path': 'link/new'}, str(worker), realpath=os.path.realpath)
            secret = root / '.ssh'
            secret.mkdir()
            (secret / 'key').write_text('do not read')
            (worker / 'ordinary').symlink_to(secret / 'key')
            with self.assertRaises(PermissionError):
                normalize_file_args('read_file', {'path': 'ordinary'}, str(worker), realpath=os.path.realpath)
            (worker / 'src').mkdir()
            (worker / 'private').mkdir()
            (worker / 'private/a.py').write_text('private')
            (worker / 'src/a.py').symlink_to(worker / 'private/a.py')
            with self.assertRaises(PermissionError):
                normalize_file_args('write_file', {'path': 'src/a.py'}, str(worker), ['src/*.py'], realpath=os.path.realpath)


if __name__ == '__main__':
    unittest.main()
