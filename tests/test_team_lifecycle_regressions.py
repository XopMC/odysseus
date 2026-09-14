"""Lifecycle races use explicit scheduling gates and real SQLite, never a host."""
import asyncio
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from src.team_runtime import TeamRuntime
from src.team_store import TeamStore, Conflict, LeaseLost


class TeamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.
        self.store = TeamStore(Path(self.directory.name) / 'teams.db', clock=lambda: self.now)
        self.selection = {'endpoint_id': 'local', 'model': 'fixture'}
        self.config = {'trusted_host': True, 'reviewer': True, 'web': False, 'external': False}
        self.route = {**self.selection, 'local': True, 'resource_group': 'jetson'}
        self.runtime = TeamRuntime(self.store, host=self.unexpected_host, complete=self.complete)
        resolver = patch('src.team_config.resolve', return_value=self.route)
        resolver.start()
        self.addCleanup(resolver.stop)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def unexpected_host(self, *args, **kwargs):
        raise AssertionError('This regression must never reach a real host')

    async def complete(self, *args, **kwargs):
        return {'message': {'role': 'assistant', 'content': 'Verified fixture'}, 'usage': {}}

    def task(self, config=None):
        return self.store.create_task('owner', 'Lifecycle fixture', metadata={
            'goal': 'Verify', 'project_path': '/project', 'leader': self.selection,
            'participants': [], 'config': {**self.config, **(config or {})}})

    def worker(self, task, kind='worker', **extra):
        return self.store.add_worker('owner', task['id'], 'Fixture', profile={
            **self.selection, 'kind': kind, 'role': 'executor', 'cwd': '/project', **extra})

    def finish(self, task, worker, result):
        claim = self.store.claim_worker('owner', task['id'], worker_id=worker['id'])
        self.store.finish_worker('owner', task['id'], worker['id'], claim['lease_token'], result)
        return self.store.get_worker('owner', task['id'], worker['id'])

    async def test_old_reviewer_after_human_accept_is_consumed_once(self):
        task = self.task()
        target = self.finish(task, self.worker(task), {'completed': True})
        review = self.finish(task, self.worker(task, 'verification', target_worker=target['id'],
                            target_attempt=target['attempt_id']),
                            {'target_worker': target['id'], 'review': {'verdict': 'pass'}})
        await self.runtime.accept_result('owner', task['id'], target['id'])
        await self.runtime.coordinate('owner', task['id'])
        await self.runtime.coordinate('owner', task['id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], review['id'])['status'], 'accepted')
        finalizers = [w for w in self.store.list_workers('owner', task['id']) if w['profile']['kind'] == 'finalizer']
        self.assertEqual(len(finalizers), 1)

    async def test_reviewer_verdict_cannot_accept_a_newer_worker_attempt(self):
        task = self.task()
        target = self.finish(task, self.worker(task), {'completed': True})
        review = self.finish(task, self.worker(task, 'verification', target_worker=target['id'],
                            target_attempt=target['attempt_id']),
                            {'target_worker': target['id'], 'review': {'verdict': 'pass'}})
        self.store.reject_worker('owner', task['id'], target['id'], 'Human requires revision')
        new_claim = self.store.claim_worker('owner', task['id'], worker_id=target['id'])
        await self.runtime.coordinate('owner', task['id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], target['id'])['status'], 'running')
        self.assertNotEqual(new_claim['attempt_id'], target['attempt_id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], review['id'])['status'], 'accepted')

    async def test_human_acceptance_obeys_active_coordinator_lock(self):
        task = self.task()
        target = self.finish(task, self.worker(task), {'completed': True})
        claim = self.store.claim_coordinator('owner', task['id'])
        with self.assertRaises(Conflict):
            await self.runtime.accept_result('owner', task['id'], target['id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], target['id'])['status'], 'done')
        self.store.release_coordinator('owner', task['id'], claim['lease_token'])
        await self.runtime.accept_result('owner', task['id'], target['id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], target['id'])['status'], 'accepted')

    async def test_revoke_during_review_read_prevents_following_integration_rpc(self):
        task = self.task()
        target = self.finish(task, self.worker(task, workspace={'mode': 'git', 'record': {
            'id': 'tree', 'path': '/project/worker', 'source': '/project/integration'}}), {'completed': True})
        calls = []
        async def host(op, args, owner, scope):
            calls.append(op)
            self.store.update_task_metadata(owner, task['id'], {'config': {**self.config, 'trusted_host': False}})
            return {'ok': True, 'result': {'patch': 'fixture patch', 'source_tree': 'A', 'worktree_tree': 'B'}}
        self.runtime.host = host
        with self.assertRaises(PermissionError):
            await self.runtime.accept_result('owner', task['id'], target['id'])
        self.assertEqual(calls, ['git.diff'])
        self.assertEqual(self.store.get_worker('owner', task['id'], target['id'])['status'], 'done')
        self.assertEqual(self.store.list_artifacts('owner', task['id']), [])

    async def test_expired_coordinator_cannot_dispatch_workspace_rpc(self):
        task = self.task()
        claim = self.store.claim_coordinator('owner', task['id'], lease_seconds=1)
        guarded = self.runtime.workspace_host('owner', task['id'], claim['lease_token'])
        self.now += 2
        with self.assertRaises(LeaseLost):
            await guarded('git.integrate', {}, 'owner', 'worker')

    async def test_cancel_includes_integration_scope_used_by_finalizer(self):
        task = self.task()
        worker = self.worker(task, 'finalizer')
        calls = []
        async def host(op, args, owner, scope):
            calls.append((op, scope))
            return {'ok': True, 'result': {'jobs': [{'id': scope + '-job', 'status': 'running'}]}}
        self.runtime.host = host
        await self.runtime.stop_host_jobs('owner', task['id'], worker)
        self.assertEqual({scope for op, scope in calls if op == 'terminal.stop'}, {task['id'], worker['id']})

    async def test_known_failed_final_check_can_be_retried_after_fix(self):
        task = self.task({'project_profile': {'test_command': 'python3 -m unittest'}})
        worker = self.worker(task, 'finalizer')
        runs = []
        async def execute_tool(*args):
            runs.append(1)
            return {'exit_code': 1 if len(runs) == 1 else 0, 'output': 'fixture test result'}
        self.runtime.execute_tool = execute_tool
        states = []
        for attempt in range(2):
            if attempt:
                self.store.update_worker('owner', task['id'], worker['id'], status='pending')
            claim = self.store.claim_worker('owner', task['id'], worker_id=worker['id'])
            await self.runtime.run_worker('owner', task['id'], claim)
            states.append(self.store.get_worker('owner', task['id'], worker['id'])['status'])
        self.assertEqual(len(runs), 2)
        self.assertEqual(states, ['failed', 'done'])

    async def test_auto_continue_disabled_pauses_recovered_worker_before_dispatch(self):
        task = self.task({'auto_continue': False})
        worker = self.worker(task)
        self.store.claim_worker('owner', task['id'], worker_id=worker['id'], lease_seconds=1)
        self.now += 2
        cycle_finished, parked = asyncio.Event(), asyncio.Event()
        dispatched = []
        async def record_run(*args):
            dispatched.append(args)
        async def sleep(seconds):
            cycle_finished.set()
            await parked.wait()
        self.runtime.run_worker = record_run
        with patch('src.team_runtime.asyncio.sleep', side_effect=sleep):
            self.runtime.start()
            await asyncio.wait_for(cycle_finished.wait(), 1)
            self.assertEqual(dispatched, [])
            self.assertEqual(self.store.get_task('owner', task['id'])['status'], 'paused')
            await self.runtime.close()

    async def test_coordinator_lease_loss_does_not_cancel_global_scheduler(self):
        task = self.task()
        entered, renewal_tick, next_cycle, parked = (asyncio.Event() for _ in range(4))
        async def coordinate(owner, task_id, token):
            entered.set()
            await parked.wait()
        async def sleep(seconds):
            if seconds == 20:
                await renewal_tick.wait()
            else:
                next_cycle.set()
                await parked.wait()
        self.runtime._coordinate = coordinate
        with patch('src.team_runtime.asyncio.sleep', side_effect=sleep):
            self.runtime.start()
            await asyncio.wait_for(entered.wait(), 1)
            self.store.set_task_status('owner', task['id'], 'paused')
            renewal_tick.set()
            reached = asyncio.create_task(next_cycle.wait())
            try:
                await asyncio.wait({reached, self.runtime.pump_task}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
                self.assertFalse(self.runtime.pump_task.done(), 'Losing one task lease killed the global pump')
                self.assertTrue(next_cycle.is_set(), 'Scheduler did not continue after coordinator lease loss')
            finally:
                reached.cancel()
                await asyncio.gather(reached, return_exceptions=True)
                await self.runtime.close()


if __name__ == '__main__':
    unittest.main()
