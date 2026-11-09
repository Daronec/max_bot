"""Lifecycle and persistence contracts for the shared application service."""
import bot_test_support
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_app.core import server
from assistant_app.core.task_service import TaskService, record_lifecycle


class TaskServiceTests(unittest.TestCase):
    def test_duplicate_event_keeps_original_run_and_payload(self):
        tasks, saved = {}, []
        service = TaskService(tasks, threading.RLock(), lambda task: saved.append(dict(task)))
        original, created = service.submit('max_1', 'original', 123, 'max')
        duplicate, repeated = service.submit('max_1', 'replacement', 123, 'max')
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertIs(duplicate, original)
        self.assertEqual(original['message'], 'original')
        self.assertEqual(len(saved), 1)

    def test_failed_save_does_not_register_task(self):
        tasks = {}
        def fail(task):
            raise OSError('disk unavailable')
        with self.assertRaises(OSError):
            TaskService(tasks, threading.RLock(), fail).submit('one', 'text', 1, 'web')
        self.assertEqual(tasks, {})

    def test_events_only_track_transitions_and_exclude_content(self):
        task = dict(status='queued', message='private-message', error='private-error')
        record_lifecycle(task)
        run_id = task['run_id']
        record_lifecycle(task)
        task['status'] = 'processing'
        record_lifecycle(task)
        task['status'] = 'queued'
        record_lifecycle(task)
        self.assertEqual([event['sequence'] for event in task['events']], [1, 2, 3])
        self.assertEqual([event['status'] for event in task['events']],
                         ['queued', 'processing', 'queued'])
        self.assertTrue(all(event['run_id'] == run_id for event in task['events']))
        self.assertNotIn('private', json.dumps(task['events']))


class RunPersistenceTests(unittest.TestCase):
    def test_worker_recovery_does_not_interrupt_live_api_delivery_or_submission(self):
        import os
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'TASKS_DIR', directory), \
                patch.object(server, 'active_tasks', {}):
            task, _ = server.task_service().submit('sending', 'text', 1, 'web', status='sending')
            delivery, _ = server.task_service().submit('delivery', 'text', 1, 'web')
            server.complete_task(delivery, 'answer', pending_delivery=True)
            server.task_store().claim_delivery('delivery')
            server.active_tasks.clear()
            server.load_tasks(recover=True, recover_submissions=False, active_delivery_pid=os.getpid())
            self.assertEqual(server.active_tasks['sending']['status'], 'sending')
            self.assertTrue(server.active_tasks['delivery']['delivery_inflight'])
            self.assertFalse(server.active_tasks['delivery'].get('delivery_uncertain', False))

    def test_interrupted_delivery_waits_for_explicit_resolution(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'TASKS_DIR', directory), \
                patch.object(server, 'active_tasks', {}):
            task, _ = server.task_service().submit('delivery', 'text', 1, 'max')
            server.complete_task(task, 'answer', pending_delivery=True)
            server.task_store().claim_delivery('delivery')
            server.active_tasks.clear()
            server.load_tasks()
            task = server.active_tasks['delivery']
            self.assertTrue(task['delivery_uncertain'])
            with patch.object(server.bot_client, 'send_message') as send:
                self.assertFalse(server.process_next_task())
                send.assert_not_called()
            response = server.app.test_client().post('/api/task/delivery/delivery', json={'action':'retry'})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(task['delivery_uncertain'])

    def test_legacy_task_identity_survives_repeated_loading(self):
        legacy = dict(task_id='old', message='text', chat_id=1, status='queued', result=None)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'TASKS_DIR', directory), \
                patch.object(server, 'active_tasks', {}):
            path = Path(directory) / 'old.json'
            path.write_text(json.dumps(legacy))
            server.load_tasks()
            first = server.active_tasks['old']['run_id']
            server.active_tasks.clear()
            server.load_tasks()
            self.assertEqual(server.active_tasks['old']['run_id'], first)
            self.assertEqual(len(server.active_tasks['old']['events']), 1)

    def test_restart_preserves_history_and_records_recovery(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'TASKS_DIR', directory), \
                patch.object(server, 'active_tasks', {}):
            task, _ = server.task_service().submit('one', 'text', 1, 'max')
            task['status'] = 'processing'
            server.persist(task)
            server.active_tasks.clear()
            server.load_tasks()
            recovered = server.active_tasks['one']
            self.assertEqual(recovered['run_id'], task['run_id'])
            self.assertEqual([event['status'] for event in recovered['events']],
                             ['queued', 'processing', 'queued'])
            response = server.app.test_client().get('/task_status/one').get_json()
            self.assertEqual(response['run_id'], task['run_id'])
            self.assertEqual(response['events'], recovered['events'])
            self.assertNotIn('message', response)
