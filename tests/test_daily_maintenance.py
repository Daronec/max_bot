from datetime import datetime, timedelta, timezone
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from assistant_app.operations.maintenance import STATE, ZONE, review_reason, tick
from assistant_app.core.task_service import TaskConflict
from assistant_app.core.task_store import SQLiteTaskStore


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

    @patch('assistant_app.operations.maintenance.requests.get')
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

    def test_completed_messages_can_still_require_review(self):
        task = {'status':'completed', 'message':'Добавь в календарь событие'}
        for answer, reason in (
            ('Я не могу добавить события в календарь.', 'request_unfulfilled'),
            ('<tool_call><function=read_file>', 'tool_markup'),
            ('Событие создано.', 'unverified_action'),
            ('Создано событие. ID: x-apple-event://fixture', None),
        ):
            self.assertEqual(review_reason(dict(task, result=answer)), reason)
        tool = {'name':'create_reminder', 'effect':'write', 'status':'failed'}
        self.assertEqual(review_reason(dict(task, result='Готово', tool_executions=[tool])), 'tool_error')
        self.assertIsNone(review_reason(dict(task, result='Напоминание создано.',
                                            tool_executions=[tool, {**tool, 'status':'completed'}])))

    def test_schedule_runs_at_four_moscow_once_per_day_across_restart(self):
        boundary = datetime(2026, 11, 9, 4, tzinfo=ZONE)
        def run(at):
            return tick(self.store, self.manager, self.lock, 'http://localhost:5001',
                        self.recover, now=at.timestamp())

        self.assertIsNone(run(boundary - timedelta(seconds=1)))
        self.assertIsNone(self.store.runtime(STATE))
        self.assertEqual(run(boundary)['day'], '2026-11-09')
        self.store = SQLiteTaskStore(self.store.directory)
        self.assertIsNone(run(boundary + timedelta(hours=10)))
        self.assertIsNone(run(boundary + timedelta(days=1, seconds=-1)))
        self.assertEqual(run(boundary + timedelta(days=1))['day'], '2026-11-10')
        self.manager.stop.assert_not_called()
