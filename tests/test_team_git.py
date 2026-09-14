import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class TeamGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@localhost')
        (self.source / 'a').write_text('original\n')
        self.git('add', 'a')
        self.git('commit', '-qm', 'initial')
        (self.source / 'a').write_text('staged\n')
        self.git('add', 'a')
        (self.source / 'a').write_text('unstaged\n')
        (self.source / 'untracked').write_text('keep me\n')
        spec = importlib.util.spec_from_file_location('git_runner_test', Path(__file__).resolve().parents[1] / 'scripts/host_runner.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.runner = module.Runner(self.root / 'state')
        self.addCleanup(self.runner.close)
        self.path_patch = patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path])
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args], stderr=subprocess.PIPE)

    def call(self, op, args, scope='worker'):
        return self.runner.handle({'owner': 'alice', 'scope': scope, 'op': op, 'args': args})

    def create(self):
        result = self.call('git.worktree.create', {'source': str(self.source), 'idempotency_key': 'one'})
        self.assertTrue(result['ok'], result)
        return result['result']

    def test_dirty_snapshot_integration_and_rollback_preserve_index(self):
        index = (self.source / '.git/index').read_bytes()
        original_status = self.git('status', '--porcelain')
        # Read status before snapshotting byte baseline: git status itself may
        # refresh stat cache, unrelated to the runner.
        index = (self.source / '.git/index').read_bytes()
        worktree = self.create()
        self.assertEqual((self.source / '.git/index').read_bytes(), index)
        self.assertEqual((Path(worktree['path']) / 'a').read_text(), 'unstaged\n')
        self.assertEqual((Path(worktree['path']) / 'untracked').read_text(), 'keep me\n')
        (Path(worktree['path']) / 'a').write_text('worker result\n')
        (Path(worktree['path']) / 'new').write_text('new file\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        integrated = self.call('git.integrate', {'id': worktree['id'], 'expected_source_tree': diff['source_tree'], 'expected_worktree_tree': diff['worktree_tree']})
        self.assertTrue(integrated['ok'], integrated)
        self.assertEqual((self.source / 'a').read_text(), 'worker result\n')
        self.assertEqual((self.source / '.git/index').read_bytes(), index)
        rolled = self.call('git.rollback', {'checkpoint_id': integrated['result']['checkpoint_id'], 'expected_source_tree': integrated['result']['source_tree']})
        self.assertTrue(rolled['ok'], rolled)
        self.assertEqual((self.source / 'a').read_text(), 'unstaged\n')
        self.assertFalse((self.source / 'new').exists())
        self.assertEqual((self.source / '.git/index').read_bytes(), index)
        self.assertEqual(self.git('status', '--porcelain'), original_status)

    def test_conflict_version_and_scope_guards(self):
        worktree = self.create()
        (Path(worktree['path']) / 'a').write_text('worker result\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        (self.source / 'a').write_text('user new changes\n')
        self.assertFalse(self.call('git.integrate', {'id': worktree['id'], 'expected_source_tree': diff['source_tree'], 'expected_worktree_tree': diff['worktree_tree']})['ok'])
        self.assertEqual((self.source / 'a').read_text(), 'user new changes\n')
        self.assertFalse(self.call('git.diff', {'id': worktree['id']}, scope='other')['ok'])
        self.assertFalse(self.call('file.call', {'tool': 'write_file', 'cwd': worktree['path'], 'content': {'path': 'a', 'content': 'intruder'}}, scope='other')['ok'])

    def child(self, parent, scope):
        response = self.call('git.worktree.create', {'source': parent['path'],
                            'idempotency_key': scope, 'parent_scope': 'team'}, scope=scope)
        self.assertTrue(response['ok'], response)
        return response['result']

    def integrate(self, worktree, scope):
        response = self.call('git.diff', {'id': worktree['id']}, scope=scope)
        self.assertTrue(response['ok'], response)
        diff = response['result']
        return self.call('git.integrate', {'id': worktree['id'], 'expected_source_tree': diff['source_tree'],
                                          'expected_worktree_tree': diff['worktree_tree']}, scope=scope)

    def test_two_children_independent_changes_merge_into_parent(self):
        parent = self.call('git.worktree.create', {'source': str(self.source), 'idempotency_key': 'parent'}, scope='team')['result']
        first, second = self.child(parent, 'first'), self.child(parent, 'second')
        self.assertEqual(first['parent_scope'], 'team')
        (Path(first['path']) / 'a').write_text('worker A\n')
        (Path(second['path']) / 'untracked').write_text('worker B\n')
        index_path = Path(self.git('-C', parent['path'], 'rev-parse', '--git-path', 'index').decode().strip())
        index = index_path.read_bytes()
        result = self.integrate(first, 'first')
        self.assertTrue(result['ok'], result)
        result = self.integrate(second, 'second')
        self.assertTrue(result['ok'], result)
        self.assertEqual((Path(parent['path']) / 'a').read_text(), 'worker A\n')
        self.assertEqual((Path(parent['path']) / 'untracked').read_text(), 'worker B\n')
        self.assertEqual(index_path.read_bytes(), index)
        self.assertEqual((self.source / 'a').read_text(), 'unstaged\n')

    def test_two_children_conflicting_hunk_never_modify_parent(self):
        parent = self.call('git.worktree.create', {'source': str(self.source), 'idempotency_key': 'parent'}, scope='team')['result']
        first, second = self.child(parent, 'first'), self.child(parent, 'second')
        (Path(first['path']) / 'a').write_text('worker A\n')
        (Path(second['path']) / 'a').write_text('worker B\n')
        self.assertTrue(self.integrate(first, 'first')['ok'])
        before = {p.name: p.read_bytes() for p in Path(parent['path']).iterdir() if p.is_file()}
        index_path = Path(self.git('-C', parent['path'], 'rev-parse', '--git-path', 'index').decode().strip())
        index = index_path.read_bytes()
        result = self.integrate(second, 'second')
        self.assertFalse(result['ok'], result)
        after = {p.name: p.read_bytes() for p in Path(parent['path']).iterdir() if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(index_path.read_bytes(), index)

    def test_parent_scope_must_match_same_owner_registered_source(self):
        parent = self.call('git.worktree.create', {'source': str(self.source), 'idempotency_key': 'parent'}, scope='team')['result']
        for parent_scope in (None, 'wrong'):
            result = self.call('git.worktree.create', {'source': parent['path'], 'idempotency_key': 'child',
                                                      'parent_scope': parent_scope}, scope='worker')
            self.assertFalse(result['ok'], result)

    def test_integration_idempotency_recovers_same_checkpoint_after_lost_ack(self):
        worktree = self.create()
        (Path(worktree['path']) / 'a').write_text('merged once\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        args = {'id': worktree['id'], 'expected_source_tree': diff['source_tree'],
                'expected_worktree_tree': diff['worktree_tree'], 'idempotency_key': 'merge-review-one'}
        first = self.call('git.integrate', args)
        self.assertTrue(first['ok'], first)
        # A retry refreshes its source guard after an acknowledgement was lost.
        args['expected_source_tree'] = first['result']['source_tree']
        index = (self.source / '.git/index').read_bytes()
        second = self.call('git.integrate', args)
        self.assertEqual(first, second)
        self.assertEqual(len(self.runner.data['checkpoints']), 1)
        self.assertEqual((self.source / '.git/index').read_bytes(), index)
        self.assertFalse(self.call('git.integrate', dict(args, expected_worktree_tree='other-reviewed-tree'))['ok'])

    def test_prepared_checkpoint_reconciles_completed_apply_on_restart(self):
        worktree = self.create()
        (Path(worktree['path']) / 'a').write_text('completed before crash\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        args = {'id': worktree['id'], 'expected_source_tree': diff['source_tree'],
                'expected_worktree_tree': diff['worktree_tree'], 'idempotency_key': 'crash'}
        first = self.call('git.integrate', args)
        self.assertTrue(first['ok'], first)
        checkpoint = self.runner.data['checkpoints'][first['result']['checkpoint_id']]
        checkpoint['status'] = 'prepared'  # crash after git apply, before metadata acknowledgement
        self.runner.save()
        restarted = type(self.runner)(self.root / 'state')
        self.addCleanup(restarted.close)
        result = restarted.handle({'owner': 'alice', 'scope': 'worker', 'op': 'git.integrate', 'args': args})
        self.assertEqual(result, first)
        self.assertEqual(restarted.data['checkpoints'][checkpoint['id']]['status'], 'applied')

    def test_rolled_back_integration_is_not_replayed_by_old_key(self):
        worktree = self.create()
        (Path(worktree['path']) / 'a').write_text('temporary\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        args = {'id': worktree['id'], 'expected_source_tree': diff['source_tree'],
                'expected_worktree_tree': diff['worktree_tree'], 'idempotency_key': 'rollback'}
        result = self.call('git.integrate', args)['result']
        rolled = self.call('git.rollback', {'checkpoint_id': result['checkpoint_id'], 'expected_source_tree': result['source_tree']})
        self.assertTrue(rolled['ok'], rolled)
        replay = self.call('git.integrate', args)
        self.assertFalse(replay['ok'])
        self.assertIn('rolled back', replay['error'])
        self.assertEqual((self.source / 'a').read_text(), 'unstaged\n')

    def test_selected_file_ignores_unselected_conflict_and_rolls_back_only_selection(self):
        worktree = self.create()
        (Path(worktree['path']) / 'a').write_text('selected A\n')
        (Path(worktree['path']) / 'untracked').write_text('worker B\n')
        (self.source / 'untracked').write_text('user B\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        self.assertEqual(sorted(diff['files']), ['a', 'untracked'])
        args = {'id': worktree['id'], 'expected_source_tree': diff['source_tree'],
                'expected_worktree_tree': diff['worktree_tree'], 'paths': ['a'], 'idempotency_key': 'subset'}
        result = self.call('git.integrate', args)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.source / 'a').read_text(), 'selected A\n')
        self.assertEqual((self.source / 'untracked').read_text(), 'user B\n')
        reused = self.call('git.integrate', dict(args, paths=['untracked']))
        self.assertFalse(reused['ok'])
        self.assertIn('different arguments', reused['error'])
        current = self.call('git.diff', {'id': worktree['id']})['result']
        conflict = self.call('git.integrate', dict(args, idempotency_key='subset-B', paths=['untracked'], expected_source_tree=current['source_tree']))
        self.assertFalse(conflict['ok'])
        self.assertEqual((self.source / 'a').read_text(), 'selected A\n')
        self.assertEqual((self.source / 'untracked').read_text(), 'user B\n')
        rolled = self.call('git.rollback', {'checkpoint_id': result['result']['checkpoint_id'], 'expected_source_tree': result['result']['source_tree']})
        self.assertTrue(rolled['ok'], rolled)
        self.assertEqual((self.source / 'a').read_text(), 'unstaged\n')
        self.assertEqual((self.source / 'untracked').read_text(), 'user B\n')

    def test_selected_paths_are_literal_and_empty_selection_is_noop(self):
        worktree = self.create()
        (Path(worktree['path']) / 'a').write_text('do not apply\n')
        diff = self.call('git.diff', {'id': worktree['id']})['result']
        args = {'id': worktree['id'], 'expected_source_tree': diff['source_tree'], 'expected_worktree_tree': diff['worktree_tree']}
        for path in ('../a', '/a', 'a*', 'a/../b', ':(top)a', 'missing'):
            result = self.call('git.integrate', dict(args, paths=[path]))
            self.assertFalse(result['ok'], result)
        result = self.call('git.integrate', dict(args, paths=[]))
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['result']['changed'])
        self.assertEqual((self.source / 'a').read_text(), 'unstaged\n')

    def test_nongit_error_has_machine_code_under_nonenglish_environment(self):
        plain = self.root / 'plain'
        plain.mkdir()
        with patch.dict(os.environ, {'LANG': 'ru_RU.UTF-8', 'LC_ALL': 'ru_RU.UTF-8', 'LANGUAGE': 'ru'}):
            result = self.call('git.worktree.create', {'source': str(plain), 'idempotency_key': 'plain'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'not_git_repository')
        # A missing path is not classified as a safe non-Git fallback.
        missing = self.call('git.worktree.create', {'source': str(plain / 'missing'), 'idempotency_key': 'missing'})
        self.assertFalse(missing['ok'])
        self.assertNotEqual(missing.get('code'), 'not_git_repository')


if __name__ == '__main__':
    unittest.main()
