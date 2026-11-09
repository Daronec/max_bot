import bot_test_support
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from assistant_settings import AssistantSettings
import command_tools
from execution_context import ExecutionContext, ExecutionStopped, check_execution, execution_session
import server
from task_store import SQLiteTaskStore, TaskConflict


class ExecutionControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        for patcher in (patch.object(server, 'TASKS_DIR', temporary.name),
                        patch.object(server, 'active_tasks', {})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_queued_cancel_prevents_inference(self):
        task = server.task_store().submit_chat('never execute', 'local_owner')
        server.refresh_tasks()
        response = server.app.test_client().post('/api/task/'+task['task_id']+'/cancel', json={})
        self.assertEqual(response.status_code, 200)
        with patch.object(server, 'process_message') as model:
            self.assertFalse(server.process_next_task())
            model.assert_not_called()
        self.assertEqual(server.task_store().get(task['task_id'])['status'], 'cancelled')

    def test_running_cancel_is_observed_before_next_action(self):
        task = server.task_store().submit_chat('cancel while running', 'local_owner')
        def model(text, **permissions):
            response = server.app.test_client().post('/api/task/'+task['task_id']+'/cancel', json={})
            self.assertEqual(response.status_code, 200)
            check_execution()
            self.fail('Cancellation should stop this run')
        with patch.object(server, 'process_message', side_effect=model):
            server.process_next_task()
        self.assertEqual(server.task_store().get(task['task_id'])['status'], 'cancelled')

    def test_attempt_limit_stops_retry_loop(self):
        task = server.task_store().submit_chat('task', 'local_owner')
        task['attempts'] = server.WORKER_MAX_ATTEMPTS
        server.persist(task)
        with patch.object(server, 'process_message') as model:
            self.assertTrue(server.process_next_task())
            model.assert_not_called()
        self.assertEqual(server.task_store().get(task['task_id'])['status'], 'failed')

    def test_deadline_and_command_cancellation_cleanup(self):
        with execution_session(ExecutionContext(deadline=time.monotonic() - 1)):
            with self.assertRaises(ExecutionStopped):
                check_execution()
        cancelled = Mock(side_effect=[False, True])
        context = ExecutionContext(cancel_check=cancelled)
        with patch.object(command_tools, 'resolve_path', return_value=__import__('pathlib').Path(self.directory)), \
                execution_session(context), command_tools.capture_command_actions() as actions:
            with self.assertRaises(ExecutionStopped):
                command_tools.run_command('sleep 20')
        self.assertTrue(actions[0]['cancelled'])
        self.assertLess(actions[0]['exit_code'], 0)


class RuntimeSettingsTests(unittest.TestCase):
    def test_validation_version_history_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteTaskStore(directory)
            settings = AssistantSettings(store, dict(local_model='local', remote_model='remote',
                remote_enabled=False, max_attempts=3, run_timeout=600))
            self.assertEqual(settings.get()['version'], 0)
            updated = settings.update({'local_model':'new-local'}, 0)
            self.assertEqual(updated['version'], 1)
            self.assertEqual(updated['values']['local_model'], 'new-local')
            with self.assertRaises(TaskConflict):
                settings.update({'remote_enabled':True}, 0)
            for invalid in ({'api_key':'secret'}, {'max_attempts':100}, {'run_timeout':float('nan')}, {'remote_enabled':'true'}):
                with self.assertRaises(ValueError):
                    settings.update(invalid, 1)
            with store.connect() as db:
                self.assertIsNotNone(db.execute("SELECT value FROM metadata WHERE key='assistant_settings_version:0'").fetchone())
