import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from src.context_policy_store import ContextPolicyStore
from src.engineering_store import EngineeringStore
from src.team_store import TeamStore, Conflict, NotFound


class ContextPolicyStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.team = TeamStore(Path(temp.name) / 'teams.db')
        self.store = ContextPolicyStore(self.team)
        self.project = EngineeringStore(self.team).create_project('a', name='Test', root='/work/test', host_id='jetson')['id']

    def save(self, overrides, **scope):
        current = self.store.get('a', **scope)
        return self.store.save('a', overrides=overrides, expected_revisions=current['revisions'], **scope)

    def test_old_preset_schema_migrates_once_preserving_rows_and_tasks(self):
        legacy = TeamStore(self.team.path.with_name('legacy.db'))
        task = legacy.create_task('a', 'Existing task')
        values = self.store.get('a')['effective']
        encoded = json.dumps(values, ensure_ascii=False, indent=2)
        with legacy._tx() as db:
            db.execute('''CREATE TABLE engineering_context_presets (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL, name TEXT NOT NULL, revision INTEGER NOT NULL,
                policy TEXT NOT NULL, updated_at REAL NOT NULL)''')
            db.execute('INSERT INTO engineering_context_presets VALUES (?,?,?,?,?,?,?)',
                       (7, 'legacy-preset', 'a', 'Старый профиль', 4, encoded, 123.5))
        def reopen(_):
            return ContextPolicyStore(TeamStore(legacy.path)).list_presets('a')['items'][0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reopen, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['kind'], 'full')
        self.assertEqual(results[0]['values'], values)
        self.assertEqual(results[0]['revision'], 4)
        with legacy._tx(write=False) as db:
            row = db.execute('SELECT seq,id,owner,name,revision,policy,updated_at FROM engineering_context_presets').fetchone()
            self.assertEqual(tuple(row), (7, 'legacy-preset', 'a', 'Старый профиль', 4, encoded, 123.5))
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(legacy.get_task('a', task['id'])['title'], 'Existing task')
        self.assertEqual(ContextPolicyStore(legacy).list_presets('b')['items'], [])

    def test_named_presets_are_versioned_owner_scoped_and_do_not_apply(self):
        policy = self.store.get('a')['effective']
        preset = self.store.save_preset('a', name='Долгая работа', values=policy)
        self.assertEqual(preset['revision'], 1)
        self.assertFalse(self.store.get('a')['configured'])
        self.assertEqual(self.store.list_presets('b')['items'], [])
        with self.assertRaises(NotFound):
            self.store.save_preset('b', preset_id=preset['id'], expected_revision=1,
                                   name='Other', values=policy)
        changed = self.store.save_preset('a', preset_id=preset['id'], expected_revision=1,
                                         name='Новое имя', values=policy)
        self.assertEqual(changed['revision'], 2)
        with self.assertRaises(Conflict):
            self.store.delete_preset('a', preset['id'], expected_revision=1)
        restarted = ContextPolicyStore(TeamStore(self.team.path))
        self.assertEqual(restarted.list_presets('a')['items'], [changed])
        restarted.delete_preset('a', preset['id'], expected_revision=2)
        self.assertEqual(restarted.list_presets('a')['items'], [])

    def test_rename_preserves_policy_and_rejects_stale_or_foreign_edits(self):
        values = {**self.store.get('a')['effective'], 'output_reserve': 7777}
        row = self.store.save_preset('a', name='Old', values=values)
        with self.assertRaises(NotFound):
            self.store.rename_preset('b', row['id'], name='Other', expected_revision=1)
        renamed = self.store.rename_preset('a', row['id'], name='Новое', expected_revision=1)
        self.assertEqual(renamed['values'], values)
        self.assertEqual(renamed['name'], 'Новое')
        self.assertEqual(renamed['revision'], 2)
        with self.assertRaises(Conflict):
            self.store.rename_preset('a', row['id'], name='Stale', expected_revision=1)

    def test_partial_preset_preserves_only_overrides_and_validates_coupled_fields(self):
        row = self.store.save_preset('a', name='Target only', values={'target_percent': 80}, kind='overrides')
        self.assertEqual(row['values'], {'target_percent': 80})
        self.assertEqual(row['kind'], 'overrides')
        self.assertEqual(ContextPolicyStore(TeamStore(self.team.path)).list_presets('a')['items'], [row])
        for values in [{'target_percent': 80, 'trigger_percent': 70}, {'output_reserve': True}, {'unknown': 1}]:
            with self.assertRaises(ValueError):
                self.store.save_preset('a', name='Bad', values=values, kind='overrides')
        with self.assertRaises(ValueError):
            self.save(row['values'])  # Invalid against the destination's actual 75% trigger.
        self.save({'trigger_percent': 90})
        self.save(row['values'], project_id=self.project)
        self.assertEqual(self.store.get('a', project_id=self.project)['effective']['target_percent'], 80)

    def test_preset_search_unicode_literal_wildcards_and_owner_pages(self):
        policy = self.store.get('a')['effective']
        for name in ['ДОЛГАЯ 1', 'other', 'Долгая 2', '100%_literal']:
            self.store.save_preset('a', name=name, values=policy)
        self.store.save_preset('b', name='Долгая private', values=policy)
        first = self.store.list_presets('a', query='долгая', limit=1)
        second = self.store.list_presets('a', query='долгая', limit=1, after_seq=first['next_cursor'])
        self.assertEqual([first['items'][0]['name'], second['items'][0]['name']], ['ДОЛГАЯ 1', 'Долгая 2'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len(self.store.list_presets('a', query='%_')['items']), 1)
        self.assertEqual(self.store.list_presets('a', query='missing')['items'], [])
        with self.assertRaises(ValueError):
            self.store.list_presets('a', query='x' * 121)

    def test_preset_pagination_and_full_policy_validation(self):
        policy = self.store.get('a')['effective']
        for name in ['One', 'Two', 'Three']:
            self.store.save_preset('a', name=name, values=policy)
        first = self.store.list_presets('a', limit=2)
        second = self.store.list_presets('a', after_seq=first['next_cursor'], limit=2)
        self.assertEqual([x['name'] for x in first['items'] + second['items']], ['One', 'Two', 'Three'])
        self.assertIsNone(second['next_cursor'])
        for values in [{}, {**policy, 'trigger_percent': 40}, {**policy, 'secret': 'no'}]:
            with self.assertRaises(ValueError):
                self.store.save_preset('a', name='Invalid', values=values)

    def test_absent_policy_preserves_legacy_and_owner_isolation(self):
        self.assertFalse(self.store.get('a')['configured'])
        self.save({'trigger_percent': 80})
        self.assertFalse(self.store.get('b')['configured'])
        with self.assertRaises(NotFound):
            self.store.get('b', project_id=self.project)
        self.assertEqual(self.store.events('b'), [])

    def test_inheritance_reset_and_restart(self):
        self.save({'trigger_percent': 80, 'target_percent': 45})
        result = self.save({'target_percent': 40}, project_id=self.project)
        self.assertEqual(result['effective']['trigger_percent'], 80)
        self.assertEqual(result['effective']['target_percent'], 40)
        self.assertEqual(result['sources']['trigger_percent'], 'owner')
        self.assertEqual(result['sources']['target_percent'], 'project:' + self.project)
        restarted = ContextPolicyStore(TeamStore(self.team.path))
        self.assertEqual(result, restarted.get('a', project_id=self.project))
        reset = self.save({}, project_id=self.project)
        self.assertEqual(reset['effective']['target_percent'], 45)
        events = self.store.events('a', limit=2)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(self.store.events('a', after_seq=events[-1]['seq'])), 1)

    def test_parent_change_requires_refresh_and_invalid_child_can_be_repaired(self):
        self.save({'target_percent': 65}, project_id=self.project)
        stale = self.store.get('a', project_id=self.project)
        self.save({'trigger_percent': 60, 'target_percent': 40})
        current = self.store.get('a', project_id=self.project)
        self.assertFalse(current['valid'])
        self.assertEqual(current['effective']['target_percent'], 65)
        with self.assertRaises(Conflict):
            self.store.save('a', project_id=self.project, overrides={}, expected_revisions=stale['revisions'])
        self.assertTrue(self.save({}, project_id=self.project)['valid'])

    def test_policy_history_replay_pages_are_owner_scoped_and_indexed(self):
        for _ in range(65):
            self.save({'trigger_percent': 80})
            current = self.store.get('b')
            self.store.save('b', overrides={'trigger_percent': 70}, expected_revisions=current['revisions'])
        rows, cursor = [], 0
        # Model an existing database from before the additive index migration.
        with self.team._tx() as db:
            db.execute('DROP INDEX engineering_context_events_owner_seq')
        restarted = ContextPolicyStore(TeamStore(self.team.path))
        while page := restarted.events('a', after_seq=cursor, limit=17):
            self.assertLessEqual(len(page), 17)
            rows.extend(page)
            cursor = page[-1]['seq']
        self.assertEqual(len(rows), 65)
        self.assertEqual(len({row['seq'] for row in rows}), 65)
        self.assertEqual([row['revision'] for row in rows], list(range(1, 66)))
        self.assertTrue(all(row['overrides'] == {'trigger_percent': 80} for row in rows))
        self.assertEqual(restarted.events('a', after_seq=cursor), [])
        with self.team._tx(write=False) as db:
            plan = db.execute('''EXPLAIN QUERY PLAN SELECT seq,scope,revision,overrides,created_at
                FROM engineering_context_policy_events WHERE owner=? AND seq>? ORDER BY seq LIMIT ?''',
                ('a', 0, 17)).fetchall()
        self.assertIn('engineering_context_events_owner_seq', ' '.join(row['detail'] for row in plan))

    def test_concurrent_devices_only_one_save_wins(self):
        revisions = self.store.get('a')['revisions']
        def save(value):
            try:
                return ContextPolicyStore(TeamStore(self.team.path)).save('a',
                    overrides={'trigger_percent': value}, expected_revisions=revisions)
            except Conflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(save, [70, 80]))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(len(self.store.events('a')), 1)

    def test_invalid_save_is_atomic(self):
        with self.assertRaises(ValueError):
            self.save({'target_percent': 80})
        self.assertFalse(self.store.get('a')['configured'])
        self.assertEqual(self.store.events('a'), [])
        with self.assertRaises(ValueError):
            self.store.get('a', worker_id='unbound')

    def test_task_worker_inheritance_and_owner_binding(self):
        task = self.team.create_task('a', 'Task', metadata={'engineering_project_id': self.project})['id']
        worker = self.team.add_worker('a', task, 'Worker')['id']
        self.save({'trigger_percent': 85})
        self.save({'target_percent': 45}, project_id=self.project)
        self.save({'summary_tokens': 800}, task_id=task)
        result = self.save({'recent_groups': 2}, task_id=task, worker_id=worker)
        self.assertEqual(len(result['layers']), 4)
        self.assertEqual(result['effective']['trigger_percent'], 85)
        self.assertEqual(result['effective']['target_percent'], 45)
        self.assertEqual(result['effective']['summary_tokens'], 800)
        self.assertEqual(result['effective']['recent_groups'], 2)
        with self.assertRaises(NotFound):
            self.store.get('b', task_id=task, worker_id=worker)
        other = self.team.create_task('a', 'Other')['id']
        with self.assertRaises(NotFound):
            self.store.get('a', task_id=other, worker_id=worker)
        with self.assertRaises(ValueError):
            self.store.get('a', project_id='foreign', task_id=task)

    def test_last_completed_request_is_owner_worker_scoped_and_durable(self):
        task = self.team.create_task('a', 'Task')['id']
        worker = self.team.add_worker('a', task, 'One')['id']
        sibling = self.team.add_worker('a', task, 'Two')['id']
        self.team.add_event('a', task, 'worker_metrics', {'worker_id': worker, 'context_policy': {'max_output_tokens': 512}})
        self.team.add_event('a', task, 'worker_metrics', {'worker_id': sibling, 'context_policy': {'max_output_tokens': 1024}})
        restarted = ContextPolicyStore(TeamStore(self.team.path))
        self.assertEqual(restarted.last_completed_request('a', task_id=task, worker_id=worker)['context_policy']['max_output_tokens'], 512)
        with self.assertRaises(NotFound):
            restarted.last_completed_request('b', task_id=task, worker_id=worker)
        self.assertIsNone(restarted.last_completed_request('a', task_id=task))
        self.team.add_event('a', task, 'worker_metrics', {'worker_id': worker})
        self.assertIsNone(restarted.last_completed_request('a', task_id=task, worker_id=worker)['context_policy'])
