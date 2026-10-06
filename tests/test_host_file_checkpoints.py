import base64
import hashlib
import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


class FileCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        spec = importlib.util.spec_from_file_location('file_checkpoint_runner', Path(__file__).resolve().parents[1] / 'scripts/host_runner.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.runner = self.module.Runner(self.root / 'state')
        self.addCleanup(self.runner.close)
        self.imports = patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path])
        self.imports.start()
        self.addCleanup(self.imports.stop)

    def call(self, op, args=None, owner='alice', scope='scope'):
        return self.runner.handle({'owner': owner, 'scope': scope, 'op': op, 'args': args or {}})

    def write(self, name, body):
        return self.call('file.call', {'cwd': str(self.root), 'tool': 'write_file', 'content': {'path': name, 'content': body}})

    def rollback_args(self, identity):
        records = self.call('file.checkpoint.list')['result']['checkpoints']
        record = next(record for record in records if record['id'] == identity)
        return {'checkpoint_id': identity, 'expected_sha256': {item['path']: item['after_sha256'] for item in record['files']}}

    def test_full_live_metadata_archives_without_losing_rollback_or_owner_scope(self):
        (self.root / 'first').write_text('original')
        with patch.object(self.module, 'MAX_FILE_CHECKPOINTS', 3):
            first = self.write('first', 'changed')['result']['checkpoint_id']
            for name in ('second', 'third', 'fourth', 'fifth'):
                response = self.write(name, 'new')
                self.assertTrue(response['ok'], response)
            self.assertLessEqual(len(self.runner.data['file_checkpoints']), 3)
            records = self.call('file.checkpoint.list')['result']['checkpoints']
            self.assertEqual(len(records), 5)
            self.assertEqual(len({r['id'] for r in records}), 5)
            self.assertEqual(self.call('file.checkpoint.list', owner='bob')['result']['checkpoints'], [])
            self.assertFalse(self.call('file.rollback', self.rollback_args(first), scope='other')['ok'])
            restarted = self.module.Runner(self.root / 'state')
            self.addCleanup(restarted.close)
            response = restarted.handle({'owner':'alice', 'scope':'scope', 'op':'file.rollback',
                'args':self.rollback_args(first)})
            self.assertTrue(response['ok'], response)
            self.assertEqual((self.root / 'first').read_text(), 'original')
            records = restarted.handle({'owner':'alice', 'scope':'scope', 'op':'file.checkpoint.list'})['result']['checkpoints']
            self.assertEqual(next(r for r in records if r['id']==first)['status'], 'rolled_back')

    def test_prepared_and_uncertain_receipts_are_not_archived_or_reported_unknown(self):
        with patch.object(self.module, 'MAX_FILE_CHECKPOINTS', 2):
            for index, status in enumerate(('prepared', 'uncertain')):
                self.runner.data['file_checkpoints'][str(index)] = {
                    'id':str(index), 'owner':'alice', 'scope':'scope', 'status':status,
                    'created_at':index, 'bytes':0, 'files':[],
                }
            result = self.write('not-created', 'new')
            self.assertFalse(result['ok'])
            self.assertTrue(result.get('not_executed'), result)
            self.assertEqual(result.get('code'), 'file_checkpoint_capacity')
            self.assertFalse((self.root / 'not-created').exists())
            self.assertEqual(len(self.runner.data['file_checkpoints']), 2)

    def test_archived_before_images_still_count_toward_storage_limit(self):
        (self.root / 'a').write_text('1234')
        (self.root / 'b').write_text('5678')
        with patch.object(self.module, 'MAX_FILE_CHECKPOINTS', 1):
            self.assertTrue(self.write('a', 'after')['ok'])
            with patch.object(self.module, 'MAX_FILE_CHECKPOINT_BYTES', 7):
                result=self.write('b', 'must not happen')
            self.assertFalse(result['ok'])
            self.assertTrue(result.get('not_executed'), result)
            self.assertEqual((self.root / 'b').read_text(), '5678')
            records=self.call('file.checkpoint.list')['result']['checkpoints']
            self.assertEqual(len(records),1)

    def test_checkpoint_archive_pages_are_bounded_and_do_not_drop_old_records(self):
        with patch.object(self.module, 'MAX_FILE_CHECKPOINTS', 2):
            for index in range(6):
                self.assertTrue(self.write(str(index), 'body')['ok'])
            records=[]
            cursor=None
            while True:
                page=self.call('file.checkpoint.list', {'before':cursor, 'limit':2})['result']
                self.assertLessEqual(len(page['checkpoints']),2)
                records.extend(page['checkpoints'])
                if page['next_cursor'] is None:
                    break
                cursor=page['next_cursor']
            self.assertEqual(len(records),6)
            self.assertEqual(len({r['id'] for r in records}),6)

    def test_checkpoint_cursor_survives_new_writes_between_pages(self):
        with patch.object(self.module,'MAX_FILE_CHECKPOINTS',2):
            for name in ('a','b','c','d'):
                self.assertTrue(self.write(name,'body')['ok'])
            first=self.call('file.checkpoint.list',{'limit':2})['result']
            self.assertTrue(self.write('new-after-first-page','body')['ok'])
            older=self.call('file.checkpoint.list',{'limit':2,'before':first['next_cursor']})['result']
            ids=[r['id'] for r in first['checkpoints']+older['checkpoints']]
            self.assertEqual(len(ids),4)
            self.assertEqual(len(set(ids)),4)

    def test_failed_archive_metadata_commit_preserves_hot_and_cold_receipts(self):
        with patch.object(self.module, 'MAX_FILE_CHECKPOINTS', 2):
            for name in ('a','b'):
                self.assertTrue(self.write(name,'value')['ok'])
            before=set(self.runner.data['file_checkpoints'])
            with patch.object(self.module,'atomic_json',side_effect=OSError('simulated fsync failure')):
                result=self.write('not-created','body')
            self.assertFalse(result['ok'])
            self.assertTrue(result.get('not_executed'), result)
            self.assertFalse((self.root / 'not-created').exists())
            self.assertEqual(set(self.runner.data['file_checkpoints']),before)
            restarted=self.module.Runner(self.root / 'state')
            self.addCleanup(restarted.close)
            records=restarted.handle({'op':'file.checkpoint.list','owner':'alice','scope':'scope'})['result']['checkpoints']
            self.assertEqual({r['id'] for r in records},before)
            self.assertEqual(len(records),2)

    def test_restore_original_text_mode_and_keep_contents_out_of_metadata(self):
        target = self.root / 'a'
        target.write_text('original private content')
        target.chmod(0o751)
        result = self.write('a', 'new content')
        self.assertTrue(result['ok'], result)
        identity = result['result']['checkpoint_id']
        checkpoint = result['result']['file_checkpoint']
        self.assertEqual(checkpoint['id'], identity)
        self.assertEqual(checkpoint['status'], 'applied')
        self.assertEqual(checkpoint['files'][0]['after_sha256'], hashlib.sha256(b'new content').hexdigest())
        self.assertEqual(checkpoint['files'][0]['before_sha256'], hashlib.sha256(b'original private content').hexdigest())
        self.assertNotIn('original private content', (self.root / 'state/metadata.json').read_text())
        restored = self.call('file.rollback', self.rollback_args(identity))
        self.assertTrue(restored['ok'], restored)
        self.assertEqual(target.read_text(), 'original private content')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o751)

    def test_agent_goal_run_lineage_is_in_checkpoint_response_and_durable_record(self):
        result = self.call('file.call', {
            'cwd': str(self.root), 'tool': 'write_file', 'run_id': 'agent-run-7',
            'content': {'path': 'agent.txt', 'content': 'agent output'},
        })
        self.assertTrue(result['ok'], result)
        checkpoint = result['result']['file_checkpoint']
        self.assertEqual(checkpoint['run_id'], 'agent-run-7')
        record = self.runner.data['file_checkpoints'][checkpoint['id']]
        self.assertEqual(record['run_id'], 'agent-run-7')

    def test_created_file_deleted_only_when_exact_after_sha_matches(self):
        result = self.write('created', 'created once')
        identity = result['result']['checkpoint_id']
        wrong = self.rollback_args(identity)
        wrong['expected_sha256'][str(self.root / 'created')] = 'wrong'
        self.assertFalse(self.call('file.rollback', wrong)['ok'])
        self.assertTrue((self.root / 'created').exists())
        self.assertTrue(self.call('file.rollback', self.rollback_args(identity))['ok'])
        self.assertFalse((self.root / 'created').exists())

    def test_later_user_edit_refuses_even_when_caller_provides_new_hash(self):
        identity = self.write('a', 'checkpoint after')['result']['checkpoint_id']
        (self.root / 'a').write_text('newer user changes')
        import hashlib
        args = self.rollback_args(identity)
        args['expected_sha256'][str(self.root / 'a')] = hashlib.sha256(b'newer user changes').hexdigest()
        self.assertFalse(self.call('file.rollback', args)['ok'])
        self.assertEqual((self.root / 'a').read_text(), 'newer user changes')

    def test_multifile_add_update_delete_restores_all_and_scope_isolated(self):
        (self.root / 'update').write_text('old\n')
        (self.root / 'delete').write_text('restore deletion\n')
        patch_text = '*** Begin Patch\n*** Update File: update\n@@\n-old\n+new\n*** Delete File: delete\n*** Add File: created\n+created\n*** End Patch'
        hashes = {
            str(self.root / 'update'): hashlib.sha256(b'old\n').hexdigest(),
            str(self.root / 'delete'): hashlib.sha256(b'restore deletion\n').hexdigest(),
            str(self.root / 'created'): 'missing',
        }
        result = self.call('file.call', {'cwd': str(self.root), 'tool': 'apply_patch',
                                        'content': {'patch_text': patch_text, 'expected_sha256_by_path': hashes}})
        self.assertTrue(result['ok'], result)
        identity = result['result']['checkpoint_id']
        args = self.rollback_args(identity)
        self.assertFalse(self.call('file.rollback', args, scope='other')['ok'])
        self.assertEqual(self.call('file.checkpoint.list', owner='bob')['result']['checkpoints'], [])
        restored = self.call('file.rollback', args)
        self.assertTrue(restored['ok'], restored)
        self.assertEqual((self.root / 'update').read_text(), 'old\n')
        self.assertEqual((self.root / 'delete').read_text(), 'restore deletion\n')
        self.assertFalse((self.root / 'created').exists())

    def test_binary_upload_checkpoint_and_restart(self):
        (self.root / 'blob').write_bytes(b'\x00\xffbefore')
        import hashlib
        args = {'cwd': str(self.root), 'path': 'blob', 'data_base64': base64.b64encode(b'\xfeafter').decode(),
                'expected_sha256': hashlib.sha256(b'\x00\xffbefore').hexdigest()}
        result = self.call('file.upload', args)
        self.assertTrue(result['ok'], result)
        rollback = self.rollback_args(result['result']['checkpoint_id'])
        restarted = self.module.Runner(self.root / 'state')
        self.addCleanup(restarted.close)
        response = restarted.handle({'op': 'file.rollback', 'owner': 'alice', 'scope': 'scope', 'args': rollback})
        self.assertTrue(response['ok'], response)
        self.assertEqual((self.root / 'blob').read_bytes(), b'\x00\xffbefore')

    def test_multifile_conflict_preflight_preserves_every_path(self):
        for name in ('a', 'b'):
            (self.root / name).write_text('old\n')
        patch_text = '*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** Update File: b\n@@\n-old\n+new\n*** End Patch'
        hashes = {str(self.root / name): hashlib.sha256(b'old\n').hexdigest() for name in ('a', 'b')}
        result = self.call('file.call', {'cwd': str(self.root), 'tool': 'apply_patch',
                                        'content': {'patch_text': patch_text, 'expected_sha256_by_path': hashes}})
        args = self.rollback_args(result['result']['checkpoint_id'])
        (self.root / 'b').write_text('later user edit\n')
        response = self.call('file.rollback', args)
        self.assertFalse(response['ok'], response)
        self.assertEqual((self.root / 'a').read_text(), 'new\n')
        self.assertEqual((self.root / 'b').read_text(), 'later user edit\n')

    def test_change_after_hash_check_is_not_adopted_as_new_baseline(self):
        for initially_exists in (True, False):
            with self.subTest(initially_exists=initially_exists):
                name = 'existing' if initially_exists else 'created'
                target = self.root / name
                if initially_exists:
                    target.write_text('original')
                identity = self.write(name, 'changed')['result']['checkpoint_id']
                args = self.rollback_args(identity)
                original = self.runner._file_state
                reads = 0
                def racing_state(path, with_data=False):
                    nonlocal reads
                    value = original(path, with_data=with_data)
                    if str(path) == str(target):
                        reads += 1
                        if reads == 2:
                            target.write_text('concurrent user edit')
                    return value
                with patch.object(self.runner, '_file_state', side_effect=racing_state):
                    response = self.call('file.rollback', args)
                self.assertFalse(response['ok'], response)
                self.assertEqual(target.read_text(), 'concurrent user edit')

    def test_active_direct_workspace_job_blocks_rollback(self):
        target = self.root / 'a'
        target.write_text('original')
        identity = self.write('a', 'changed')['result']['checkpoint_id']
        self.runner.data['jobs']['active-test'] = {'status': 'running', 'cwd': str(self.root),
                                                 'owner': 'alice', 'scope': 'scope'}
        response = self.call('file.rollback', self.rollback_args(identity))
        self.assertFalse(response['ok'], response)
        self.assertIn('active', response['error'])
        self.assertEqual(target.read_text(), 'changed')

    def test_mid_rollback_conflict_is_explicit_partial_and_preserves_conflict(self):
        for name in ('a', 'b'):
            (self.root / name).write_text('old\n')
        patch_text = '*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** Update File: b\n@@\n-old\n+new\n*** End Patch'
        hashes = {str(self.root / name): hashlib.sha256(b'old\n').hexdigest() for name in ('a', 'b')}
        result = self.call('file.call', {'cwd': str(self.root), 'tool': 'apply_patch',
                                        'content': {'patch_text': patch_text, 'expected_sha256_by_path': hashes}})
        identity = result['result']['checkpoint_id']
        args = self.rollback_args(identity)
        original = self.runner._file_state
        counts = {}
        def concurrent_state(path, with_data=False):
            counts[str(path)] = counts.get(str(path), 0) + 1
            if str(path) == str(self.root / 'b') and counts[str(path)] == 2:
                (self.root / 'b').write_text('later user edit\n')
            return original(path, with_data=with_data)
        with patch.object(self.runner, '_file_state', side_effect=concurrent_state):
            response = self.call('file.rollback', args)
        self.assertFalse(response['ok'], response)
        self.assertIn('partial rollback', response['error'])
        self.assertEqual((self.root / 'a').read_text(), 'old\n')
        self.assertEqual((self.root / 'b').read_text(), 'later user edit\n')
        self.assertEqual(self.runner.data['file_checkpoints'][identity]['status'], 'rollback_partial')

    def test_symlink_and_quota_fail_before_file_changes(self):
        (self.root / 'outside').mkdir()
        (self.root / 'link').symlink_to(self.root / 'outside', target_is_directory=True)
        self.assertFalse(self.write('link/a', 'bad')['ok'])
        self.assertFalse((self.root / 'outside/a').exists())
        (self.root / 'a').write_text('original')
        with patch.object(self.module, 'MAX_FILE_CHECKPOINT_BYTES', 1):
            self.assertFalse(self.write('a', 'new')['ok'])
        self.assertEqual((self.root / 'a').read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
