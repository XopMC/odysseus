import asyncio
import copy
import unittest

from src import team_workspace as workspace


class WorkspaceTests(unittest.TestCase):
    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def test_team_and_child_deterministic_keys_and_no_mutation(self):
        calls = []

        async def host(op, args, owner, scope):
            calls.append((op, args, owner, scope))
            return {'ok': True, 'result': {'id': scope, 'path': '/managed/' + scope, 'source': args['source']}}

        meta = {'project_path': '/project', 'unrelated': 'preserve'}
        original = copy.deepcopy(meta)
        patch = self.run_async(workspace.ensure_team(host, 'alice', 'team', meta))
        self.run_async(workspace.ensure_team(host, 'alice', 'team', meta))
        self.assertEqual(calls[0][1]['idempotency_key'], calls[1][1]['idempotency_key'])
        profile = self.run_async(workspace.ensure_worker(host, 'alice', 'team', 'worker', {**meta, **patch}, {'model': 'keep'}))
        self.assertEqual(calls[-1][1]['parent_scope'], 'team')
        self.assertEqual(calls[-1][3], 'worker')
        self.assertEqual(profile['cwd'], '/managed/worker')
        self.assertEqual(meta, original)

    def test_only_explicit_nongit_diagnostic_allows_direct_mode(self):
        async def nongit(*args):
            return {'ok': False, 'code': 'not_git_repository',
                    'error': 'Git: fatal: не найден git репозиторий (или один из родительских каталогов): .git'}

        patch = self.run_async(workspace.ensure_team(nongit, 'alice', 'team', {'project_path': '/plain'}))
        self.assertEqual(patch['workspace']['mode'], 'direct')
        self.assertTrue(patch['workspace']['exclusive_required'])
        for error in ('SSH failed', 'Git: fatal: detected dubious ownership', 'source has active runner jobs',
                      'submodule snapshots are not supported', 'Git: fatal: not a git repository',
                      'Git: fatal: не найден git репозиторий'):
            async def failed(*args):
                return {'ok': False, 'error': error}
            with self.assertRaises(workspace.WorkspaceError):
                self.run_async(workspace.ensure_team(failed, 'alice', 'team', {'project_path': '/project'}))

    def test_review_requires_approval_and_pins_worker_but_refreshes_parent(self):
        calls = []
        current_source = ['base']
        current_worker = ['worker-tree']

        async def host(op, args, owner, scope):
            calls.append((op, args))
            if op == 'git.diff':
                return {'ok': True, 'result': {'patch': 'a diff', 'source_tree': current_source[0],
                                               'worktree_tree': current_worker[0], 'truncated': False}}
            return {'ok': True, 'result': {'checkpoint_id': 'checkpoint', 'changed': True}}

        profile = {'workspace': {'mode': 'git', 'record': {'id': 'id', 'path': '/worker', 'source': '/parent'}}}
        review = self.run_async(workspace.review_diff(host, 'alice', 'worker', profile))
        with self.assertRaises(workspace.WorkspaceError):
            self.run_async(workspace.integrate_reviewed(host, 'alice', 'worker', profile, review))
        review['approved'] = True
        current_source[0] = 'after-sibling-merge'
        result = self.run_async(workspace.integrate_reviewed(host, 'alice', 'worker', profile, review))
        self.assertEqual(result['checkpoint_id'], 'checkpoint')
        self.assertEqual(calls[-1][0], 'git.integrate')
        self.assertEqual(calls[-1][1]['expected_source_tree'], 'after-sibling-merge')
        self.assertEqual(calls[-1][1]['expected_worktree_tree'], 'worker-tree')
        integration_key = calls[-1][1]['idempotency_key']
        current_source[0] = 'merged-already'
        self.run_async(workspace.integrate_reviewed(host, 'alice', 'worker', profile, review))
        self.assertEqual(calls[-1][1]['idempotency_key'], integration_key)
        current_worker[0] = 'modified-after-review'
        with self.assertRaises(workspace.WorkspaceError):
            self.run_async(workspace.integrate_reviewed(host, 'alice', 'worker', profile, review))

    def test_direct_mode_is_explicit_no_fake_checkpoint(self):
        async def no_host(*args):
            self.fail('non-Git worker must not create another Git checkout')

        profile = self.run_async(workspace.ensure_worker(no_host, 'alice', 'team', 'worker',
                    {'workspace': {'mode': 'direct', 'source_path': '/plain'}}, {}))
        self.assertTrue(profile['workspace']['exclusive_required'])
        review = self.run_async(workspace.review_diff(no_host, 'alice', 'worker', profile))
        review['approved'] = True
        result = self.run_async(workspace.integrate_reviewed(no_host, 'alice', 'worker', profile, review))
        self.assertFalse(result['checkpoint_supported'])

    def test_selected_files_are_forwarded_and_change_idempotency_identity(self):
        calls = []
        async def host(op, args, owner, scope):
            calls.append((op, args))
            if op == 'git.diff':
                return {'ok': True, 'result': {'patch': 'diff', 'source_tree': 'parent', 'worktree_tree': 'worker',
                                               'files': ['a', 'b'], 'truncated': False}}
            return {'ok': True, 'result': {'changed': True}}
        profile = {'workspace': {'mode': 'git', 'record': {'id': 'id', 'path': '/worker', 'source': '/parent'}}}
        review = self.run_async(workspace.review_diff(host, 'owner', 'worker', profile))
        review.update(approved=True, selected_paths=['a'])
        self.run_async(workspace.integrate_reviewed(host, 'owner', 'worker', profile, review))
        first_key = calls[-1][1]['idempotency_key']
        self.assertEqual(calls[-1][1]['paths'], ['a'])
        review['selected_paths'] = ['b']
        self.run_async(workspace.integrate_reviewed(host, 'owner', 'worker', profile, review))
        self.assertNotEqual(calls[-1][1]['idempotency_key'], first_key)
        review['selected_paths'] = ['not-reviewed']
        with self.assertRaises(workspace.WorkspaceError):
            self.run_async(workspace.integrate_reviewed(host, 'owner', 'worker', profile, review))


if __name__ == '__main__':
    unittest.main()
