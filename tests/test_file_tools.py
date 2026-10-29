import bot_test_support  # Load synthetic configuration before application imports.
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import file_tools
import server
import task_processor


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.backups = self.root / '.bot_file_backups'
        for target, name, value in (
            (file_tools, 'BASE_DIR', self.root), (file_tools, 'FS_ROOTS', (self.root,)),
            (file_tools, 'FS_SEARCH_ROOTS', ()),
            (file_tools, 'BACKUP_DIR', self.backups),
            (file_tools, 'PROTECTED_PATHS', {self.root/'config.py', self.root/'guard.py'}),
            (task_processor, 'FS_ROOTS', (self.root,)),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_create_read_and_edit_with_backup(self):
        result = file_tools.write_file('note.txt', 'before\n')
        self.assertTrue(result['created'])
        read = file_tools.read_file('note.txt')
        self.assertEqual(read['content'], 'before\n')
        result = file_tools.edit_file('note.txt', 'before', 'after', read['sha256'])
        self.assertTrue(result['changed'])
        self.assertEqual((self.root/'note.txt').read_text(), 'after\n')
        self.assertEqual((self.backups/(result['backup_id']+'.txt')).read_text(), 'before\n')
        self.assertEqual(list(self.root.glob('.maxbot-*')), [])

    def test_cannot_overwrite_without_current_hash(self):
        (self.root/'note.txt').write_text('original')
        self.assertIn('error', file_tools.write_file('note.txt', 'new'))
        old = file_tools.read_file('note.txt')['sha256']
        (self.root/'note.txt').write_text('changed externally')
        self.assertIn('error', file_tools.write_file('note.txt', 'new', old))
        self.assertEqual((self.root/'note.txt').read_text(), 'changed externally')

    def test_duplicate_write_is_noop(self):
        file_tools.write_file('note.txt', 'same')
        with file_tools.capture_file_actions() as actions:
            self.assertFalse(file_tools.write_file('note.txt', 'same')['changed'])
        self.assertEqual(actions, [])
        self.assertFalse(self.backups.exists())

    def test_edit_requires_unique_exact_fragment(self):
        file_tools.write_file('note.txt', 'old old')
        sha = file_tools.read_file('note.txt')['sha256']
        self.assertIn('error', file_tools.edit_file('note.txt', 'old', 'new', sha))
        self.assertIn('error', file_tools.edit_file('note.txt', 'missing', 'new', sha))
        self.assertNotIn('error', file_tools.edit_file('note.txt', 'old', 'new', sha, replace_all=True))
        self.assertEqual((self.root/'note.txt').read_text(), 'new new')

    def test_directory_listing_search_and_pagination(self):
        file_tools.create_directory('docs/nested')
        file_tools.write_file('docs/note.txt', 'text')
        file_tools.write_file('docs/other.md', 'text')
        result = file_tools.list_directory('docs', limit=1)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['total'], 3)
        self.assertEqual(file_tools.search_files('*.txt')['matches'][0]['path'], str(self.root/'docs/note.txt'))

    def test_project_discovery_does_not_grant_content_access(self):
        with tempfile.TemporaryDirectory() as external:
            project = Path(external).resolve() / 'velosimulator'
            project.mkdir()
            (project/'note.txt').write_text('original')
            with patch.object(file_tools, 'FS_SEARCH_ROOTS', (Path(external),)):
                for path in ('.', '/', str(Path(external))):
                    result = file_tools.search_files('velosimulator', path=path)
                    self.assertIn({'path': str(project), 'type': 'directory'}, result['matches'])
                self.assertIn('error', file_tools.read_file(str(project/'note.txt')))
                self.assertIn('error', file_tools.write_file(str(project/'note.txt'), 'changed'))
            self.assertEqual((project/'note.txt').read_text(), 'original')

    def test_search_matches_configured_root_itself(self):
        with patch.object(file_tools, 'FS_SEARCH_ROOTS', (self.root,)):
            result = file_tools.search_files(self.root.name)
            self.assertEqual(result['matches'], [{'path': str(self.root), 'type': 'directory'}])

    def test_search_does_not_accept_unrelated_external_directory(self):
        with tempfile.TemporaryDirectory() as external:
            self.assertIn('error', file_tools.search_files('anything', path=external))

    def test_paged_read_and_size_limits(self):
        file_tools.write_file('note.txt', 'one\ntwo\nthree\n')
        read = file_tools.read_file('note.txt', start_line=2, max_lines=1)
        self.assertEqual(read['content'], 'two\n')
        self.assertTrue(read['truncated'])
        with patch.object(file_tools, 'FS_MAX_BYTES', 3):
            self.assertIn('error', file_tools.read_file('note.txt'))
            self.assertIn('error', file_tools.write_file('big.txt', '1234'))

    def test_binary_files_are_not_read(self):
        (self.root/'data.bin').write_bytes(b'hello\x00there')
        self.assertIn('error', file_tools.read_file('data.bin'))

    def test_outside_paths_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)/'secret.txt'
            target.write_text('secret')
            (self.root/'alias.txt').symlink_to(target)
            for path in (str(target), '../secret.txt', 'alias.txt'):
                self.assertIn('error', file_tools.read_file(path))
                self.assertIn('error', file_tools.write_file(path, 'overwrite'))
            self.assertEqual(target.read_text(), 'secret')

    def test_credentials_control_files_and_backup_history_protected(self):
        for name in ('config.py', '.env', '.env.local', 'id_rsa'):
            (self.root/name).write_text('secret')
            self.assertIn('error', file_tools.read_file(name))
        (self.root/'guard.py').write_text('code')
        self.assertEqual(file_tools.read_file('guard.py')['content'], 'code')
        self.assertIn('error', file_tools.write_file('guard.py', 'replacement'))
        self.backups.mkdir()
        (self.backups/'old.txt').write_text('old')
        self.assertIn('error', file_tools.read_file('.bot_file_backups/old.txt'))
        names = [entry['name'] for entry in file_tools.list_directory()['entries']]
        self.assertNotIn('config.py', names)
        self.assertNotIn('.bot_file_backups', names)

    def test_hardlinks_are_rejected(self):
        (self.root/'one.txt').write_text('data')
        os.link(self.root/'one.txt', self.root/'two.txt')
        self.assertIn('error', file_tools.read_file('two.txt'))
        self.assertIn('error', file_tools.write_file('two.txt', 'other'))

    def test_changes_recorded_before_model_completes(self):
        observed = []
        with file_tools.capture_file_actions(observed.append) as actions:
            result = file_tools.write_file('note.txt', 'data')
            self.assertEqual(observed, [result])
        self.assertEqual(actions, observed)

    def test_model_edits_real_file_without_web_enabled(self):
        file_tools.write_file('note.txt', 'before')
        sha = file_tools.read_file('note.txt')['sha256']
        responses = []
        for message in (
            {'content':None, 'tool_calls':[{'id':'r','function':{'name':'read_file','arguments':json.dumps({'path':'note.txt'})}}]},
            {'content':None, 'tool_calls':[{'id':'w','function':{'name':'edit_file','arguments':json.dumps({
                'path':'note.txt','old_text':'before','new_text':'after','expected_sha256':sha})}}]},
            {'content':'Файл изменён'},
        ):
            response = MagicMock()
            response.json.return_value = {'choices':[{'message':message}]}
            responses.append(response)
        with patch.object(task_processor, 'WEB_ENABLED', False), \
                patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post', side_effect=responses):
            self.assertIn('изменён', task_processor.process_message('Замени before на after в файле note.txt', allow_files=True))
        self.assertEqual((self.root/'note.txt').read_text(), 'after')

    def test_unrelated_request_does_not_expose_file_tools(self):
        response = MagicMock()
        response.json.return_value = {'choices':[{'message':{'content':'hello'}}]}
        with patch.object(task_processor, 'WEB_ENABLED', False), \
                patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post', return_value=response) as post:
            task_processor.process_message('Привет', allow_files=True)
            self.assertNotIn('tools', post.call_args.kwargs['json'])

    def test_read_request_cannot_trigger_writes_or_web_requests(self):
        response = MagicMock()
        response.json.return_value = {'choices':[{'message':{'content':'fake'}}]}
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor, 'WEB_ENABLED', True), \
                patch.object(task_processor.requests, 'post', return_value=response) as post:
            with self.assertRaises(task_processor.ProcessingError):
                task_processor.process_message('Прочитай файл note.txt', allow_files=True)
            for call in post.call_args_list:
                names = [tool['function']['name'] for tool in call.kwargs['json']['tools']]
                self.assertNotIn('web_search', names)
                self.assertNotIn('write_file', names)
                self.assertNotIn('edit_file', names)


class ChatAccessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(server, 'TASKS_DIR', tmp.name),
                  patch.object(server, 'FS_ALLOWED_CHAT_IDS', frozenset({'123'}))):
            p.start()
            self.addCleanup(p.stop)
        server.active_tasks.clear()
        self.addCleanup(server.active_tasks.clear)
        self.task = dict(task_id='test', message='task', chat_id=123, bot_message_id='mid',
                         status='queued', result=None, timestamp=server.now())
        server.active_tasks['test'] = self.task

    def test_only_allowed_chat_gets_file_tools(self):
        for chat, expected in ((123, True), (999, False)):
            self.task.update(chat_id=chat, status='queued', result=None)
            with patch.object(server, 'process_message', return_value='done') as process, \
                    patch.object(server, 'deliver_result'):
                server.process_next_task()
                if expected:
                    process.assert_called_once_with('task', allow_files=True, allow_commands=True)
                else:
                    process.assert_called_once_with('task')

    def test_restart_does_not_replay_task_after_file_changes(self):
        self.task.update(status='processing', file_actions=[{'path':'note.txt'}])
        server.persist(self.task)
        server.active_tasks.clear()
        server.load_tasks()
        self.assertEqual(server.active_tasks['test']['status'], 'failed')

    def test_model_failure_after_changes_is_not_automatically_retried(self):
        def fail(*args, **kwargs):
            file_tools._actions.get().append({'path':'note.txt','changed':True})
            raise server.ProcessingError('model failed after edit')
        with patch.object(server, 'process_message', side_effect=fail), \
                patch.object(server.bot_client, 'send_message', return_value=None):
            server.process_next_task()
        self.assertEqual(self.task['status'], 'failed')
        self.assertNotIn('retry_at', self.task)
        self.assertTrue(self.task['file_actions'])


if __name__ == '__main__':
    unittest.main()
