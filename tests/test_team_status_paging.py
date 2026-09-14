import tempfile
import unittest
from pathlib import Path
from src.team_store import TeamStore, NotFound
from src.team_collaboration import validate_tool_arguments


class TeamStatusPagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = TeamStore(Path(temporary.name) / 'team.db')
        self.task = self.store.create_task('owner', 'Large team')['id']

    def test_all_workers_and_dependencies_reachable(self):
        ids = [self.store.add_worker('owner', self.task, f'worker {i}')['id'] for i in range(75)]
        target = self.store.add_worker('owner', self.task, 'dependent', depends_on=ids)['id']
        seen, cursor = [], ''
        while True:
            page = self.store.worker_status_page('owner', self.task, after_id=cursor, limit=13)
            seen.extend(w['id'] for w in page['workers'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(sorted(seen), sorted([*ids, target]))
        self.assertEqual(len(seen), len(set(seen)))
        seen, cursor = [], ''
        while True:
            page = self.store.worker_status_page('owner', self.task, worker_id=target, after_id=cursor, limit=9)
            seen.extend(page['depends_on'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(seen, sorted(ids))
        with self.assertRaises(NotFound):
            self.store.worker_status_page('other', self.task)

    def test_schema_keeps_legacy_empty_arguments_and_checks_page_bounds(self):
        self.assertEqual(validate_tool_arguments('team_status', {}), {})
        for payload in ({'limit': True}, {'limit': 0}, {'limit': 201}, {'after_id': 7}, {'worker_id': ''}, {'owner': 'other'}):
            with self.assertRaises(ValueError):
                validate_tool_arguments('team_status', payload)


if __name__ == '__main__':
    unittest.main()
