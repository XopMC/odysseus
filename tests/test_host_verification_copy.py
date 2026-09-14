import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import shutil
import subprocess
import unittest
from unittest.mock import patch


class VerificationCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        spec = importlib.util.spec_from_file_location('verification_runner', Path(__file__).resolve().parents[1] / 'scripts/host_runner.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.runner = module.Runner(self.root / 'state')
        self.addCleanup(self.runner.close)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / '.git').mkdir()
        (self.source / '.git' / 'index').write_bytes(b'original dirty index marker')
        (self.source / '.git' / 'config').write_text('[filter "unsafe"]\n clean = touch FILTER_EXECUTED\n smudge = touch FILTER_EXECUTED\n')
        (self.source / '.gitattributes').write_text('*.txt filter=unsafe\n')
        (self.source / '.gitignore').write_text('ignored.txt\n')
        (self.source / 'dirty.txt').write_text('uncommitted working bytes')
        (self.source / 'untracked.txt').write_text('new untracked bytes')
        (self.source / 'ignored.txt').write_text('ignored but included source bytes')

    def call(self, op, args, owner='owner', scope='project'):
        return self.runner.handle({'op': op, 'args': args, 'owner': owner, 'scope': scope})

    def digest(self, path):
        result = self.call('workspace.digest', {'cwd': str(path)})
        self.assertTrue(result['ok'], result)
        return result['result']['sha256']

    def args(self, key='copy'):
        return {'source': str(self.source), 'expected_source_sha256': self.digest(self.source), 'idempotency_key': key}

    def create(self, args=None):
        response = self.call('workspace.verification-copy', args or self.args())
        self.assertTrue(response['ok'], response)
        return response['result']

    def test_raw_copy_preserves_source_includes_ignored_and_never_invokes_git(self):
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        args = self.args()
        with patch.object(self.runner, '_git', side_effect=AssertionError('Git must not run')):
            copy = self.create(args)
        target = Path(copy['path'])
        self.assertFalse(copy['git_metadata_present'])
        self.assertFalse((target / '.git').exists())
        self.assertEqual(copy['source_sha256'], self.digest(target))
        self.assertEqual(copy['copy_sha256'], args['expected_source_sha256'])
        for name in ('dirty.txt', 'untracked.txt', 'ignored.txt', '.gitignore', '.gitattributes'):
            self.assertEqual((target / name).read_bytes(), (self.source / name).read_bytes())
            self.assertNotEqual((target / name).stat().st_ino, (self.source / name).stat().st_ino)
        self.assertFalse((target / 'FILTER_EXECUTED').exists())
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertEqual(copy['id'], self.create(args)['id'])
        (target / 'dirty.txt').write_text('verification modified its copy')
        self.assertEqual((self.source / 'dirty.txt').read_text(), 'uncommitted working bytes')
        self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        self.assertTrue(target.exists())  # never silently replaced or deleted

    def test_hash_binding_ownership_scope_and_git_rejection(self):
        args = self.args()
        copy = self.create(args)
        self.assertFalse(self.call('workspace.verification-copy', dict(args, expected_source_sha256='0' * 64))['ok'])
        for owner, scope in [('intruder', 'project'), ('owner', 'other')]:
            self.assertFalse(self.call('command.start', {'cwd': copy['path'], 'command': 'true', 'idempotency_key': 'x'}, owner, scope)['ok'])
            self.assertFalse(self.call('workspace.digest', {'cwd': copy['path']}, owner, scope)['ok'])
        self.assertFalse(self.call('git.diff', {'id': copy['id']})['ok'])

    def test_failed_copy_is_retained_never_replayed(self):
        args = self.args()
        original = self.runner.workspace_digest
        def fail_after_copy(cwd, owner, scope, **kwargs):
            result = original(cwd, owner, scope, **kwargs)
            if kwargs.get('_copy_to'):
                raise OSError('injected failure after files copied')
            return result
        with patch.object(self.runner, 'workspace_digest', side_effect=fail_after_copy):
            self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        record = next(r for r in self.runner.data['worktrees'].values() if r.get('kind') == 'verification-copy')
        self.assertEqual(record['status'], 'failed')
        self.assertTrue(Path(record['path']).exists())
        self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        self.assertFalse(self.call('command.start', {'cwd': record['path'], 'command': 'true', 'idempotency_key': 'no'})['ok'])

    def test_interrupted_preparation_restart_retains_claim(self):
        args = self.args()
        copy = self.create(args)
        self.runner.data['worktrees'][copy['id']]['status'] = 'preparing'
        self.runner.save()
        self.runner.close()
        self.runner = self.module.Runner(self.root / 'state')
        self.addCleanup(self.runner.close)
        self.assertEqual(self.runner.data['worktrees'][copy['id']]['status'], 'failed')
        self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        self.assertTrue(Path(copy['path']).exists())

    def test_source_race_and_source_change_before_command_block(self):
        args = self.args()
        original = self.runner.workspace_digest
        def race(cwd, owner, scope, **kwargs):
            result = original(cwd, owner, scope, **kwargs)
            if kwargs.get('_copy_to'):
                (self.source / 'dirty.txt').write_text('concurrent change')
            return result
        with patch.object(self.runner, 'workspace_digest', side_effect=race):
            self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        copy = self.create(self.args('fresh'))
        (self.source / 'dirty.txt').write_text('changed again before command')
        result = self.call('command.start', {'cwd': copy['path'], 'command': 'touch MUST_NOT_EXIST',
            'idempotency_key': 'no-start', 'expected_workspace_hash': copy['copy_sha256']})
        self.assertFalse(result['ok'])
        self.assertFalse((Path(copy['path']) / 'MUST_NOT_EXIST').exists())

    def test_symlink_fifo_cancel_and_state_inside_source_refused(self):
        args = self.args()
        (self.source / 'link').symlink_to(self.root / 'outside')
        self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        (self.source / 'link').unlink()
        os.mkfifo(self.source / 'fifo')
        self.assertFalse(self.call('workspace.verification-copy', args)['ok'])
        (self.source / 'fifo').unlink()
        self.assertFalse(self.call('workspace.verification-copy', dict(args, source=str(self.root)))['ok'])
        self.call('scope.cancel', {})
        self.assertFalse(self.call('workspace.verification-copy', args)['ok'])

    def test_real_command_writes_copy_only(self):
        copy = self.create()
        result = self.call('command.start', {'cwd': copy['path'], 'command': 'printf checked > dirty.txt',
            'idempotency_key': 'real-command', 'expected_workspace_hash': copy['copy_sha256']})
        self.assertTrue(result['ok'], result)
        for _ in range(200):
            job = self.call('terminal.poll', {'id': result['result']['id']})['result']
            if job['status'] != 'running':
                break
            time.sleep(.01)
        self.assertEqual(job['exit_code'], 0)
        self.assertEqual((Path(copy['path']) / 'dirty.txt').read_text(), 'checked')
        self.assertEqual((self.source / 'dirty.txt').read_text(), 'uncommitted working bytes')

    @unittest.skipUnless(shutil.which('git'), 'Git unavailable for parent-discovery negative test')
    def test_copy_command_cannot_discover_parent_git_or_inherit_git_overrides(self):
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        copy = self.create()
        with patch.dict(os.environ, {'GIT_DIR': str(self.root / '.git'),
                                     'GIT_WORK_TREE': str(self.root),
                                     'GIT_INDEX_FILE': str(self.root / '.git' / 'index')}):
            response = self.call('command.start', {'cwd': copy['path'], 'command': 'git rev-parse --show-toplevel',
                                                   'idempotency_key': 'git-must-not-discover'})
        self.assertTrue(response['ok'], response)
        for _ in range(200):
            job = self.call('terminal.poll', {'id': response['result']['id']})['result']
            if job['status'] != 'running':
                break
            time.sleep(.01)
        self.assertNotEqual(job['exit_code'], 0)
        self.assertNotEqual(job['output'].strip(), str(self.root))

    def test_copy_retention_limit_never_deletes_existing(self):
        copy = self.create()
        with patch.object(self.module, 'MAX_VERIFICATION_COPIES', 1):
            self.assertFalse(self.call('workspace.verification-copy', self.args('second'))['ok'])
            self.assertEqual(self.create()['id'], copy['id'])
        self.assertTrue(Path(copy['path']).exists())


if __name__ == '__main__':
    unittest.main()
