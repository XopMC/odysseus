import base64
import tempfile
import unittest
from pathlib import Path

from src import team_artifact_files as artifacts


PNG = b'\x89PNG\r\n\x1a\nfixture'


class Store:
    def __init__(self):
        self.rows = {}

    def add_artifact(self, owner, task_id, name, data, **kwargs):
        identity = f'a{len(self.rows) + 1}'
        row = {'id': identity, 'name': name, 'data': data, 'owner': owner, 'task_id': task_id, **kwargs}
        self.rows[identity] = row
        return row

    def get_artifact(self, owner, task_id, artifact_id):
        row = self.rows.get(artifact_id)
        if not row or row['owner'] != owner or row['task_id'] != task_id:
            raise ValueError('not found')
        return row


class TeamArtifactFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = artifacts.DATA_DIR
        artifacts.DATA_DIR = self.temp.name
        self.store = Store()

    def tearDown(self):
        artifacts.DATA_DIR = self.old_data
        self.temp.cleanup()

    def test_persists_owner_scoped_png_and_reads_exact_row(self):
        rows = artifacts.persist_screenshots(self.store, 'alice', 'task-a', 'worker-a', 'lease', [
            {'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()},
        ])
        self.assertEqual(rows[0]['id'], 'a1')
        path, mime = artifacts.open_screenshot(self.store, 'alice', 'task-a', 'a1')
        self.assertEqual(mime, 'image/png')
        self.assertEqual(path.read_bytes(), PNG)
        with self.assertRaises(ValueError):
            artifacts.open_screenshot(self.store, 'bob', 'task-a', 'a1')
        self.assertNotIn('alice', str(path))

    def test_rejects_non_image_and_leaves_no_artifact(self):
        with self.assertRaises(ValueError):
            artifacts.persist_screenshots(self.store, 'alice', 'task-a', 'worker-a', 'lease', [
                {'mimeType': 'image/png', 'data': base64.b64encode(b'not a png').decode()},
            ])
        self.assertEqual(self.store.rows, {})

    def test_rejects_over_limit_count_before_writing(self):
        image = {'mimeType': 'image/png', 'data': base64.b64encode(PNG).decode()}
        with self.assertRaises(ValueError):
            artifacts.persist_screenshots(self.store, 'alice', 'task-a', 'worker-a', 'lease', [image] * 5)
        self.assertEqual(self.store.rows, {})
