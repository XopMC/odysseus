"""Manual acceptance can acquire its lock after the scheduler waits for a human."""
import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.team_runtime import TeamRuntime
from src.team_store import TeamStore, Conflict, NotFound, LeaseLost


class ManualAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.
        self.store = TeamStore(Path(self.directory.name) / 'team.db', clock=lambda: self.now)
        self.selection = {'endpoint_id': 'local', 'model': 'fixture'}
        self.runtime = TeamRuntime(self.store, host=self.unexpected, complete=self.unexpected)
        resolver = patch('src.team_config.resolve', return_value={
            **self.selection, 'local': True, 'resource_group': 'jetson'})
        resolver.start()
        self.addCleanup(resolver.stop)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def unexpected(self, *args, **kwargs):
        raise AssertionError('Manual-acceptance lock tests must not call any model or host')

    def completed_worker(self):
        task = self.store.create_task('owner', 'Verify fixture', metadata={
            'goal': 'Verify fixture', 'project_path': '/fixture', 'leader': self.selection,
            'participants': [], 'config': {'reviewer': False, 'trusted_host': False}})
        worker = self.store.add_worker('owner', task['id'], 'Ready for review', profile={
            **self.selection, 'kind': 'worker', 'role': 'executor', 'cwd': '/fixture'})
        claim = self.store.claim_worker('owner', task['id'])
        self.store.finish_worker('owner', task['id'], worker['id'], claim['lease_token'],
                                 {'completed': True, 'summary': 'Fixture verified'})
        return task, worker

    async def test_accept_after_scheduler_enters_waiting_approval(self):
        task, worker = self.completed_worker()
        await self.runtime.coordinate('owner', task['id'])
        self.assertEqual(self.store.get_task('owner', task['id'])['status'], 'waiting_approval')
        self.assertIsNone(self.store.claim_coordinator('owner', task['id']))
        await self.runtime.accept_result('owner', task['id'], worker['id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], worker['id'])['status'], 'accepted')
        self.assertEqual(self.store.get_task('owner', task['id'])['status'], 'running')
        await self.runtime.coordinate('owner', task['id'])
        finalizers = [w for w in self.store.list_workers('owner', task['id'])
                      if w['profile'].get('kind') == 'finalizer']
        self.assertEqual(len(finalizers), 1)
        self.assertEqual(finalizers[0]['status'], 'pending')

    async def test_accept_completed_worker_from_blocked_team(self):
        task, worker = self.completed_worker()
        self.store.set_task_status('owner', task['id'], 'blocked')
        self.assertIsNone(self.store.claim_coordinator('owner', task['id']))
        await self.runtime.accept_result('owner', task['id'], worker['id'])
        self.assertEqual(self.store.get_worker('owner', task['id'], worker['id'])['status'], 'accepted')
        self.assertEqual(self.store.get_task('owner', task['id'])['status'], 'running')

    async def test_manual_review_never_reopens_paused_failed_or_terminal_tasks(self):
        for status in ('paused', 'cancelled', 'failed', 'done', 'accepted'):
            with self.subTest(status=status):
                task, worker = self.completed_worker()
                if status in {'done', 'accepted'}:
                    self.store.accept_worker('owner', task['id'], worker['id'])
                self.store.set_task_status('owner', task['id'], status)
                before = self.store.get_worker('owner', task['id'], worker['id'])['status']
                self.assertIsNone(self.store.claim_coordinator('owner', task['id'], manual_review=True))
                with self.assertRaises(Conflict):
                    await self.runtime.accept_result('owner', task['id'], worker['id'])
                self.assertEqual(self.store.get_task('owner', task['id'])['status'], status)
                self.assertEqual(self.store.get_worker('owner', task['id'], worker['id'])['status'], before)

    async def test_manual_review_lock_retains_owner_concurrency_and_pause_fencing(self):
        task, worker = self.completed_worker()
        self.store.set_task_status('owner', task['id'], 'waiting_approval')
        def claim(_):
            return TeamStore(self.store.path, clock=lambda: self.now).claim_coordinator(
                'owner', task['id'], manual_review=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(claim, range(4)))
        winners = [item for item in claims if item]
        self.assertEqual(len(winners), 1)
        token = winners[0]['lease_token']
        with self.assertRaises(Conflict):
            await self.runtime.accept_result('owner', task['id'], worker['id'])
        with self.assertRaises(NotFound):
            self.store.claim_coordinator('stranger', task['id'], manual_review=True)
        self.store.assert_coordinator('owner', task['id'], token)
        self.store.set_task_status('owner', task['id'], 'paused')
        with self.assertRaises(LeaseLost):
            self.store.accept_worker('owner', task['id'], worker['id'], coordinator_token=token)
        self.assertEqual(self.store.get_worker('owner', task['id'], worker['id'])['status'], 'done')


if __name__ == '__main__':
    unittest.main()
