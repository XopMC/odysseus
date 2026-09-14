import asyncio
import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from src.team_store import TeamStore, NotFound, Conflict
from src.engineering_operations import Operations, OperationManager


REQUEST = dict(endpoint_id='local', model='same-name', confirmation=True,
               expected_config_digest='a' * 64)


class OperationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.now = 100.
        self.team = TeamStore(Path(temp.name) / 'team.db', clock=lambda: self.now)
        self.store = Operations(self.team)

    def test_check_queue_revision_owner_idempotency_and_cancellation(self):
        from src.engineering_checks import EngineeringChecks
        checks = EngineeringChecks(self.team)
        project = checks.projects.create_project('a', name='Project', root='/work/test', host_id='host')
        profile = checks.approve_profile('a', project['id'], name='Tests', command='true', confirmation=True)
        request = dict(project_id=project['id'], profile_id=profile['id'], kind='check',
                       idempotency_key='same-request', expected_project_revision=2,
                       expected_profile_revision=1, confirmation=True)
        with self.assertRaises(PermissionError):
            self.store.create_check('a', request)
        checks.projects.set_policy('a', project['id'], 1, 'trusted_host', confirmation=True)
        with self.assertRaises(NotFound):
            self.store.create_check('b', request)
        with self.assertRaises(Conflict):
            self.store.create_check('a', dict(request, expected_profile_revision=2))
        queued = self.store.create_check('a', request)
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(queued['id'], self.store.create_check('a', request)['id'])
        self.assertEqual(len(self.store.list('a', kind='check_run')['operations']), 1)
        self.assertEqual(self.store.list('a')['operations'], [])
        self.assertNotIn('command', queued['scope'])
        self.assertIn('run_id', queued['scope'])
        with self.assertRaises(Conflict):
            self.store.create_check('a', dict(request, kind='baseline'))
        self.store.cancel('a', queued['id'])
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.create_check('a', request)['status'], 'cancelled')

    def test_owner_pagination_and_public_scope(self):
        records = [self.store.create('a', REQUEST) for _ in range(4)]
        self.store.create('b', REQUEST)
        page = self.store.list('a', limit=2)
        page2 = self.store.list('a', limit=2, after_id=page['next_cursor'])
        self.assertEqual({r['id'] for r in records}, {r['id'] for r in page['operations'] + page2['operations']})
        self.assertIsNone(page2['next_cursor'])
        self.assertEqual(records[0]['scope']['model'], 'same-name')
        self.assertNotIn('request', records[0])
        with self.assertRaises(NotFound):
            self.store.cancel('b', records[0]['id'])

    def test_check_project_filter_finds_old_work_and_rejects_foreign_cursor(self):
        from src.engineering_checks import EngineeringChecks
        checks = EngineeringChecks(self.team)
        def make_request(name):
            project = checks.projects.create_project('a', name=name, root='/work/' + name, host_id='host')
            checks.projects.set_policy('a', project['id'], 1, 'trusted_host', confirmation=True)
            profile = checks.approve_profile('a', project['id'], name='Test', command='true', confirmation=True)
            return dict(project_id=project['id'], profile_id=profile['id'], kind='check',
                        idempotency_key=name, expected_project_revision=2,
                        expected_profile_revision=1, confirmation=True)
        first, other = make_request('first'), make_request('other')
        old = self.store.create_check('a', first)
        for index in range(55):
            self.now += 1
            new = self.store.create_check('a', dict(other, idempotency_key=str(index)))
        self.assertNotIn(old['id'], {r['id'] for r in self.store.list('a', kind='check_run')['operations']})
        page = self.store.list('a', kind='check_run', project_id=first['project_id'], active_only=True)
        self.assertEqual([r['id'] for r in page['operations']], [old['id']])
        self.assertIsNone(page['next_cursor'])
        with self.assertRaises(NotFound):
            self.store.list('a', kind='check_run', project_id=first['project_id'], after_id=new['id'])
        with self.assertRaises(NotFound):
            self.store.list('b', kind='check_run', project_id=first['project_id'])

    def test_claim_once_and_expired_outcome_not_repeated(self):
        record = self.store.create('a', REQUEST)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda _: Operations(self.team).claim(), range(2)))
        self.assertEqual(sum(x is not None for x in claims), 1)
        row, lease = next(x for x in claims if x)
        self.now += 16
        self.assertIsNone(self.store.claim())
        self.store.finish(row['id'], lease, 'completed', result={'success': True})
        self.assertEqual(self.store.get('a', record['id'])['status'], 'interrupted')

    def test_cancel_queued_and_fence_completed(self):
        record = self.store.create('a', REQUEST)
        self.store.cancel('a', record['id'])
        self.assertIsNone(self.store.claim())
        record = self.store.create('a', REQUEST)
        row, lease = self.store.claim()
        self.store.finish(row['id'], lease, 'completed', result={'ok': True})
        self.store.cancel('a', record['id'])
        self.store.finish(row['id'], lease, 'failed', error='late')
        self.assertEqual(self.store.get('a', record['id'])['status'], 'completed')

    def test_recent_order_paging_and_old_active_beyond_first_page(self):
        oldest = self.store.create('a', REQUEST)
        records = []
        for index in range(70):
            self.now += 1
            record = self.store.create('a', REQUEST)
            self.store.cancel('a', record['id'])
            records.append(record)
        first = self.store.list('a', limit=50)
        self.assertEqual(first['operations'][0]['id'], records[-1]['id'])
        self.assertNotIn(oldest['id'], {row['id'] for row in first['operations']})
        second = self.store.list('a', after_id=first['next_cursor'], limit=50)
        self.assertEqual(second['operations'][-1]['id'], oldest['id'])
        self.assertEqual(len(first['operations'] + second['operations']), 71)
        self.assertIsNone(second['next_cursor'])
        active = self.store.list('a', active_only=True, limit=1)
        self.assertEqual([row['id'] for row in active['operations']], [oldest['id']])
        self.assertIsNone(active['next_cursor'])
        with self.assertRaises(NotFound):
            self.store.list('b', after_id=oldest['id'])

    def test_cursor_survives_new_entries_and_active_completion(self):
        original = [self.store.create('a', REQUEST) for _ in range(6)]
        first = self.store.list('a', limit=2)
        self.now += 1
        newer = self.store.create('a', REQUEST)
        second = self.store.list('a', after_id=first['next_cursor'], limit=10)
        self.assertEqual({row['id'] for row in first['operations'] + second['operations']},
                         {row['id'] for row in original})
        active = self.store.list('a', active_only=True, limit=2)
        self.store.cancel('a', active['next_cursor'])
        next_active = self.store.list('a', active_only=True, after_id=active['next_cursor'], limit=10)
        self.assertEqual(len(next_active['operations']), 5)
        self.assertEqual(next_active['operations'][-1]['id'], newer['id'])


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.team = TeamStore(Path(temp.name) / 'team.db')

    async def wait_status(self, manager, operation_id, status):
        async with asyncio.timeout(4):
            while manager.store.get('a', operation_id)['status'] != status:
                await asyncio.sleep(.01)

    async def test_saved_queue_and_result_after_manager_restart(self):
        called = []
        async def handler(owner, check_active, **request):
            self.assertTrue(check_active())
            called.append(request)
            return {'checks': {'native_tool_call': True}}
        first = OperationManager(self.team, handler)
        record = first.store.create('a', REQUEST)
        second = OperationManager(self.team, handler)
        second.start()
        try:
            await self.wait_status(second, record['id'], 'completed')
        finally:
            await second.close()
        self.assertEqual(len(called), 1)
        self.assertTrue(Operations(self.team).get('a', record['id'])['result']['checks']['native_tool_call'])

    async def test_cancel_inflight_and_shutdown_never_repeats(self):
        entered = asyncio.Event()
        async def handler(owner, check_active, **request):
            entered.set()
            await asyncio.Event().wait()
        manager = OperationManager(self.team, handler)
        first = manager.store.create('a', REQUEST)
        manager.start()
        await asyncio.wait_for(entered.wait(), 2)
        manager.store.cancel('a', first['id'])
        await self.wait_status(manager, first['id'], 'cancelled')
        entered.clear()
        second = manager.store.create('a', REQUEST)
        await asyncio.wait_for(entered.wait(), 2)
        await manager.close()
        self.assertEqual(manager.store.get('a', second['id'])['status'], 'interrupted')
        self.assertIsNone(manager.store.claim())

    async def test_provider_error_is_redacted(self):
        async def handler(*args, **kwargs):
            raise RuntimeError('secret-provider-credential')
        manager = OperationManager(self.team, handler)
        record = manager.store.create('a', REQUEST)
        manager.start()
        try:
            await self.wait_status(manager, record['id'], 'failed')
            self.assertNotIn('secret-provider', str(manager.store.get('a', record['id'])))
        finally:
            await manager.close()
