import fcntl
from pathlib import Path
import tempfile
import unittest

from assistant_app.knowledge.index import KnowledgeStore
from assistant_app.operations.storage_admin import restore
from assistant_app.core.task_store import SQLiteTaskStore


class StorageRecoveryTests(unittest.TestCase):
    def test_restore_preserves_deletions_and_stops_historical_pending_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteTaskStore(root)
            knowledge = KnowledgeStore(store)
            document = knowledge.ingest('owner', 'notes.txt', 'Удаляемая личная запись')
            store.save(dict(task_id='queued', message='external action', status='queued', source='max', result=None))
            store.save(dict(task_id='delivery', message='message', status='completed', result='answer', delivered=False))
            snapshot = root / 'snapshot.sqlite3'
            store.backup(snapshot)
            knowledge.delete('owner', document['id'])
            recovery = restore(root, snapshot)
            self.assertTrue(recovery.is_file())
            restored = SQLiteTaskStore(root)
            self.assertEqual(restored.get('queued')['status'], 'failed')
            self.assertTrue(restored.get('delivery')['delivery_uncertain'])
            self.assertEqual(KnowledgeStore(restored).list('owner'), [])
            self.assertTrue(restored.migrate_json()['restored'])

    def test_restore_rejects_active_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteTaskStore(root)
            snapshot = root / 'snapshot.sqlite3'
            store.backup(snapshot)
            with (root / '.api.lock').open('a') as guard:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(RuntimeError):
                    restore(root, snapshot)
