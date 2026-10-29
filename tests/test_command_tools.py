import bot_test_support  # Load synthetic configuration before application imports.
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import command_tools
import file_tools
import server
import task_processor


class CommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patcher = patch.object(file_tools, 'FS_ROOTS', (self.root,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cwd_stdout_stderr_and_exit_code(self):
        result = command_tools.run_command('pwd; printf output; printf error >&2; exit 7', cwd=str(self.root))
        self.assertEqual(result['exit_code'], 7)
        self.assertIn(str(self.root), result['stdout'])
        self.assertIn('output', result['stdout'])
        self.assertEqual(result['stderr'], 'error')
        self.assertFalse(result['timed_out'])

    def test_timeout_kills_process_group(self):
        started = time.monotonic()
        result = command_tools.run_command('sleep 20 & wait', timeout=1)
        self.assertTrue(result['timed_out'])
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotEqual(result['exit_code'], 0)

    def test_output_is_bounded(self):
        with patch.object(command_tools, 'COMMAND_MAX_OUTPUT_BYTES', 100):
            result = command_tools.run_command("printf '%0200d' 0; printf '%0200d' 0 >&2")
        self.assertTrue(result['output_truncated'])
        self.assertLessEqual(len(result['stdout'].encode()) + len(result['stderr'].encode()), 100)

    def test_bad_cwd_and_empty_commands_do_not_start(self):
        with patch.object(command_tools.subprocess, 'Popen') as popen:
            self.assertIn('error', command_tools.run_command(''))
            self.assertIn('error', command_tools.run_command('pwd', cwd='/'))
            popen.assert_not_called()

    def test_attempt_is_recorded_before_start(self):
        observed = []
        with command_tools.capture_command_actions(observed.append) as actions:
            result = command_tools.run_command('printf done')
        self.assertFalse(observed[0]['started'])
        self.assertTrue(observed[-1]['started'])
        self.assertEqual(observed[-1]['exit_code'], 0)
        self.assertEqual(actions[0]['id'], result['id'])

    def test_bot_credentials_are_not_inherited_as_environment(self):
        with patch.dict('os.environ', {'MAX_BOT_TOKEN':'secret1','LM_API_KEY':'secret2'}):
            result = command_tools.run_command('printf "%s:%s" "$MAX_BOT_TOKEN" "$LM_API_KEY"')
        self.assertEqual(result['stdout'], ':')


class CommandModelTests(unittest.TestCase):
    def test_authorized_explicit_command_reaches_tool(self):
        response1 = MagicMock()
        response1.json.return_value = {'choices':[{'message':{'content':None,'tool_calls':[
            {'id':'cmd1','function':{'name':'run_command','arguments':json.dumps({'command':'pwd','cwd':'.'})}}]}}]}
        response2 = MagicMock()
        response2.json.return_value = {'choices':[{'message':{'content':'/project'}}]}
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post', side_effect=[response1,response2]), \
                patch.object(command_tools, 'run_command', return_value={'exit_code':0,'stdout':'/project','stderr':'','timed_out':False}) as run:
            self.assertEqual(task_processor.process_message('Выполни команду pwd', allow_commands=True), '/project')
            run.assert_called_once_with(command='pwd', cwd='.')

    def test_untrusted_chat_cannot_dispatch_commands(self):
        response1 = MagicMock()
        response1.json.return_value = {'choices':[{'message':{'content':None,'tool_calls':[
            {'id':'cmd1','function':{'name':'run_command','arguments':json.dumps({'command':'pwd'})}}]}}]}
        response2 = MagicMock()
        response2.json.return_value = {'choices':[{'message':{'content':'denied'}}]}
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post', side_effect=[response1,response2]), \
                patch.object(command_tools, 'run_command') as run:
            task_processor.process_message('Привет')
            run.assert_not_called()

    def test_explanation_request_does_not_expose_console(self):
        response = MagicMock()
        response.json.return_value = {'choices':[{'message':{'content':'instructions'}}]}
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post', return_value=response) as post:
            task_processor.process_message('Объясни, как запустить тесты', allow_commands=True)
            self.assertNotIn('run_command', [t['function']['name'] for t in post.call_args.kwargs['json'].get('tools',[])])

    def test_failed_command_is_not_reported_as_success(self):
        response1 = MagicMock()
        response1.json.return_value = {'choices':[{'message':{'content':None,'tool_calls':[
            {'id':'cmd1','function':{'name':'run_command','arguments':json.dumps({'command':'false'})}}]}}]}
        response2 = MagicMock()
        response2.json.return_value = {'choices':[{'message':{'content':'успешно'}}]}
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post', side_effect=[response1,response2]), \
                patch.object(command_tools, 'run_command', return_value={'exit_code':1,'stderr':'failed','stdout':'','timed_out':False}):
            answer = task_processor.process_message('Выполни команду false', allow_commands=True)
            self.assertIn('Код завершения: 1', answer)
            self.assertNotEqual(answer, 'успешно')


class CommandRecoveryTests(unittest.TestCase):
    def test_interrupted_command_task_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, 'TASKS_DIR', temp):
            task = {'task_id':'cmd','message':'run','chat_id':123,'status':'processing',
                    'command_actions':[{'id':'attempt','command':'pwd','cwd':temp,'started':True}]}
            server.persist(task)
            previous = dict(server.active_tasks)
            try:
                server.active_tasks.clear()
                server.load_tasks()
                self.assertEqual(server.active_tasks['cmd']['status'], 'failed')
            finally:
                server.active_tasks.clear()
                server.active_tasks.update(previous)


if __name__ == '__main__':
    unittest.main()
