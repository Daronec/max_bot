from datetime import datetime, timezone
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from daily_maintenance import STATE, ZONE, tick
from task_service import TaskConflict
from task_store import SQLiteTaskStore


class DailyMaintenanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteTaskStore(directory.name)
        self.manager = Mock()
        self.manager.status.return_value = {'running': True}
        self.recover = Mock(return_value=True)
        self.lock = threading.Lock()
        self.now = datetime.now(ZONE).replace(hour=23, minute=59, second=59).timestamp()

    def tick(self):
        return tick(self.store, self.manager, self.lock, 'http://localhost:5001', self.recover, now=self.now)

    def task(self, status, **values):
        task = self.store.submit_chat('private user request', 'local_owner')
        stamp = datetime.fromtimestamp(self.now - 180, timezone.utc).isoformat()
        task.update(status=status, timestamp=stamp, events=[], **values)
        self.store.save(task)
        return task

    @patch('daily_maintenance.requests.get')
    def test_repairs_and_restarts_once_without_copying_dialogues(self, get):
        task = self.task('failed', error='Нет соединения с сервером модели', routing={'target': 'qwen'})
        get.return_value.json.return_value = {'ready': True}
        report = self.tick()
        self.assertEqual(report['action'], 'restarted_healthy')
        self.assertEqual(report['issues'], [{'task_id': task['task_id'], 'kind': 'local_model_offline'}])
        self.assertNotIn('private user request', str(self.store.runtime(STATE)))
        self.assertIsNone(self.tick())
        self.recover.assert_called_once()
        self.manager.stop.assert_called_once()
        self.manager.start.assert_called_once()

    def test_refusal_is_reported_and_active_task_defers_maintenance(self):
        self.task('completed', result='Не смог выполнить просьбу.',
                  completed_at=datetime.fromtimestamp(self.now - 180, timezone.utc).isoformat())
        task = self.task('processing')
        self.assertIsNone(self.tick())
        self.assertIsNone(self.store.runtime(STATE))
        task.update(status='queued')
        self.store.save(task)
        self.assertEqual(self.tick()['action'], 'review')
        self.recover.assert_not_called()
        self.manager.stop.assert_not_called()

    def test_pause_prevents_start_of_processing_and_legacy_send(self):
        queued = self.task('queued')
        self.store.set_runtime('maintenance_pause', {'until': time.time() + 30})
        self.assertIsNone(self.store.claim(queued['task_id'], time.time()))
        with self.assertRaises(TaskConflict):
            self.store.save(dict(queued, _revision=0, task_id='legacy-send', status='sending'))
