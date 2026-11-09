import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from task_store import SQLiteTaskStore, TaskConflict


def task(tid='one', **changes):
    return dict(dict(task_id=tid, message='private text', chat_id=123, source='max',
                     status='queued', result=None), **changes)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = SQLiteTaskStore(self.directory)

    def test_concurrent_claim_has_one_winner(self):
        self.store.save(task())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: SQLiteTaskStore(self.directory).claim('one', 10), range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)
        saved = self.store.get('one')
        self.assertEqual(saved['status'], 'processing')
        self.assertEqual(saved['attempts'], 1)

    def test_stale_result_cannot_overwrite_other_writer(self):
        original = task()
        self.store.save(original)
        stale = self.store.get('one')
        self.store.claim('one', 10)
        stale.update(status='completed', result='stale answer')
        with self.assertRaises(TaskConflict):
            self.store.save(stale)
        self.assertEqual(self.store.get('one')['status'], 'processing')

    def test_duplicate_insert_and_retry_schedule(self):
        self.store.save(task(retry_at=20))
        with self.assertRaises(TaskConflict):
            self.store.save(task(message='replacement'))
        self.assertIsNone(self.store.claim('one', 19))
        self.assertIsNotNone(self.store.claim('one', 20))

    def test_compatibility_exports_are_private_and_atomic(self):
        record = task(status='completed', result='private answer')
        self.store.save(record)
        self.store.export(record)
        for filename in ('one.json', 'one.txt', 'one_result.md'):
            self.assertEqual((self.directory / filename).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.directory.glob('.export-*')), [])

    def test_delivery_claim_excludes_second_sender(self):
        self.store.save(task(status='completed', result='answer', delivered=False))
        winner = self.store.claim_delivery('one')
        self.assertTrue(winner['delivery_inflight'])
        self.assertIsNone(self.store.claim_delivery('one'))
        winner.update(delivery_inflight=False, delivery_uncertain=True)
        self.store.save(winner)
        self.assertIsNone(self.store.claim_delivery('one'))

    def test_migration_snapshots_rejects_invalid_and_is_repeatable(self):
        original = json.dumps(task(), ensure_ascii=False).encode()
        (self.directory / 'one.json').write_bytes(original)
        (self.directory / 'bad.json').write_text('{')
        report = self.store.migrate_json()
        self.assertEqual(report['imported'], 1)
        self.assertEqual(report['rejected'], ['bad.json'])
        self.assertEqual((Path(report['backup']) / 'one.json').read_bytes(), original)
        self.assertEqual((Path(report['backup']) / 'bad.json').read_text(), '{')
        self.assertEqual(self.store.migrate_json(), report)
        self.assertEqual(len(self.store.all()), 1)

    def test_committed_database_wins_over_stale_json(self):
        self.store.save(task(status='completed', result='answer'))
        (self.directory / 'one.json').write_text(json.dumps(task()))
        report = self.store.migrate_json()
        self.assertEqual(report['existing'], 1)
        self.assertEqual(self.store.get('one')['result'], 'answer')

    def test_corrupt_snapshot_aborts_import_without_partial_records(self):
        (self.directory / 'one.json').write_text(json.dumps(task()))
        # Locate the deterministic backup, then force a failed retry verification.
        import hashlib
        data = (self.directory / 'one.json').read_bytes()
        digest = hashlib.sha256(b'one.json\0' + data + b'\0').hexdigest()
        backup = self.directory / 'legacy-backups' / digest
        backup.mkdir(parents=True)
        (backup / 'one.json').write_text('corrupt')
        with self.assertRaises(OSError):
            self.store.migrate_json()
        self.assertEqual(self.store.all(), [])

    def test_snapshot_restores_committed_wal_data(self):
        self.store.save(task())
        destination = self.directory / 'snapshot.sqlite3'
        self.store.backup(destination)
        with sqlite3.connect(destination) as restored:
            payload = restored.execute('SELECT payload FROM tasks').fetchone()[0]
            self.assertEqual(json.loads(payload)['message'], 'private text')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.store.backup(destination)

    def test_unknown_future_schema_is_not_downgraded(self):
        with self.store.connect() as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):
            SQLiteTaskStore(self.directory)
