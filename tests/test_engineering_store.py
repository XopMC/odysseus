import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from src.team_store import TeamStore, Conflict, NotFound
from src.engineering_store import EngineeringStore


class EngineeringStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.team = TeamStore(Path(temporary.name) / 'teams.db')
        self.store = EngineeringStore(self.team)

    def project(self):
        return self.store.create_project('alice', name='Demo', root='/work/demo', host_id='legacy-jetson')

    def test_new_project_has_no_execution_authority(self):
        project = self.project()
        self.assertIsNone(project['access_mode'])
        self.store.assert_access('alice', project['id'], effect='read')
        with self.assertRaises(PermissionError):
            self.store.assert_access('alice', project['id'], effect='execute')
        self.assertEqual(self.team.schema_version(), 1)

    def test_owner_scope_and_policy_cas(self):
        project = self.project()
        with self.assertRaises(NotFound):
            self.store.get_project('bob', project['id'])
        with self.assertRaises(PermissionError):
            self.store.set_policy('alice', project['id'], 1, 'trusted_host', confirmation=False)
        allowed = self.store.set_policy('alice', project['id'], 1, 'trusted_host', confirmation=True)
        self.assertEqual(allowed['revision'], 2)
        self.store.assert_access('alice', project['id'], effect='execute', revision=2)
        with self.assertRaises(Conflict):
            self.store.set_policy('alice', project['id'], 1, None, confirmation=True)
        self.store.set_policy('alice', project['id'], 2, None, confirmation=True)
        with self.assertRaises(PermissionError):
            self.store.assert_access('alice', project['id'], effect='execute')

    def test_isolation_never_silently_falls_back_to_host(self):
        project = self.project()
        with self.assertRaises(PermissionError):
            self.store.set_policy('alice', project['id'], 1, 'isolated', confirmation=True)
        self.assertIsNone(self.store.get_project('alice', project['id'])['access_mode'])

    def test_enabled_isolation_is_an_explicit_distinct_policy(self):
        project = self.project()
        from unittest.mock import patch
        with patch.dict('os.environ', {'ODYSSEUS_ISOLATED_RUNNER_ENABLED': '1'}):
            saved = self.store.set_policy('alice', project['id'], 1, 'isolated', confirmation=True)
            self.assertEqual(saved['access_mode'], 'isolated')
            self.store.assert_access('alice', project['id'], effect='execute', revision=2)

    def test_concurrent_policy_updates_have_one_winner(self):
        project = self.project()
        def update(_):
            try:
                return EngineeringStore(TeamStore(self.team.path)).set_policy('alice', project['id'], 1, 'trusted_host', confirmation=True)
            except Conflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(update, range(6)))
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_legacy_store_can_still_read_and_write_after_extension(self):
        task = self.team.create_task('alice', 'Existing task')
        self.project()
        restarted = TeamStore(self.team.path)
        self.assertEqual(restarted.get_task('alice', task['id'])['title'], 'Existing task')
        restarted.create_task('alice', 'New legacy task')
        self.assertEqual(len(restarted.list_tasks('alice')), 2)

    def test_events_page_without_cross_owner_leak(self):
        project = self.project()
        self.store.set_policy('alice', project['id'], 1, 'trusted_host', confirmation=True)
        first = self.store.events('alice', project['id'], limit=1)
        second = self.store.events('alice', project['id'], after_seq=first[0]['seq'])
        self.assertEqual([e['type'] for e in first + second], ['project_created', 'policy_changed'])
        with self.assertRaises(NotFound):
            self.store.events('bob', project['id'])

    def test_invalid_roots_rejected_without_normalization_surprises(self):
        for root in ['relative', '/', '/work/../secret', '/work/./demo', '/work\x00/demo', '/work\\demo']:
            with self.assertRaises(ValueError):
                self.store.create_project('alice', name='Bad', root=root, host_id='legacy-jetson')


if __name__ == '__main__':
    unittest.main()
