import bot_test_support  # Load synthetic configuration before application imports.
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_client
import bot_client_v2
import server


def msg(mid, text, reply=None, chat=123, bot=False):
    message = dict(body=dict(mid=mid, text=text), recipient=dict(chat_id=chat), sender=dict(is_bot=bot))
    if reply:
        message['link'] = dict(type='reply', message=dict(body=dict(mid=reply)))
    return message


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = bot_client.MAXBotClient('test-token', 123)

    def test_api_send_contract(self):
        with patch('bot_client.requests.request') as call:
            call.return_value.json.return_value = {'message': msg('opaque-id', 'task')}
            result = self.client.send_task('task', 'demo.txt')
            self.assertEqual(result, 'opaque-id')
            args, kw = call.call_args
            self.assertEqual(args, ('POST', bot_client.API_BASE_URL + '/messages'))
            self.assertEqual(kw['headers']['Authorization'], 'test-token')
            self.assertEqual(kw['params'], {'chat_id': 123})
            self.assertNotIn('token', kw['params'])

    def test_updates_contract_and_zero_cursor(self):
        with patch.object(self.client, '_request', return_value={'updates': [], 'marker': 9}) as call:
            self.client.get_updates(0)
            self.assertEqual(call.call_args.kwargs['params']['marker'], 0)
            self.assertNotIn('offset', call.call_args.kwargs['params'])

    def test_no_result_is_none(self):
        with patch.object(self.client, 'get_last_messages', return_value=None):
            self.assertIsNone(self.client.get_result('opaque-id', None))

    def test_only_linked_answer(self):
        messages = [msg('task', 'question'), msg('unrelated', 'wrong'),
                    msg('echo', 'outgoing', reply='task', bot=True),
                    msg('correct', 'answer', reply='task')]
        with patch.object(self.client, 'get_last_messages', return_value={'messages': messages}):
            self.assertEqual(self.client.get_result('task', None), 'answer')

    def test_invalid_chat(self):
        for chat in (None, True, '@name', 'max:@name', '../x'):
            with self.assertRaises(ValueError):
                self.client.validate_chat_id(chat)

    def test_strip_task(self):
        self.assertEqual(self.client._strip_task('📝 ЗАДАЧА\nтекст\nещё текст'), 'текст\nещё текст')

    def test_v2_save_dict_and_list(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(bot_client_v2, 'BASE_DIR', Path(tmp)):
            client = bot_client_v2.MAXBotClientV2('test', chat_id=123)
            client.save_message_file('demo', {'text': 'one'})
            client.save_message_file('demo', [{'text': 'two'}])
            self.assertEqual(len(list((Path(tmp) / 'tasks').glob('*.txt'))), 2)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(server, 'TASKS_DIR', self.directory.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        server.active_tasks.clear()
        self.client = server.app.test_client()
        self.addCleanup(server.active_tasks.clear)

    def submit(self, **extra):
        data = dict(task_id='demo', message='question', chat_id=123)
        data.update(extra)
        with patch.object(server.bot_client, 'send_task', return_value='opaque-id'):
            return self.client.post('/api/task', json=data)

    def test_submit_scalar_id_and_queued_status(self):
        data = self.submit().get_json()
        self.assertEqual(data['bot_message_id'], 'opaque-id')
        self.assertEqual(self.client.get('/task_status/demo').get_json()['status'], 'queued')

    def test_generated_id(self):
        self.assertTrue(self.submit(task_id='').get_json()['task_id'].startswith('task_'))

    def test_duplicate_does_not_resend(self):
        self.submit()
        with patch.object(server.bot_client, 'send_task') as send:
            self.assertEqual(self.client.post('/api/task', json={
                'task_id': 'demo', 'message': 'question', 'chat_id': 123}).status_code, 409)
            send.assert_not_called()

    def test_validation(self):
        for data in (None, [], {'message': 'x', 'task_id': '../escape', 'chat_id': 123},
                     {'message': [], 'chat_id': 123}):
            response = self.client.post('/api/task', json=data)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/result').status_code, 400)

    def test_own_task_is_not_result(self):
        self.submit()
        with patch.object(server.bot_client, 'get_last_messages', return_value={'messages': [msg('opaque-id', 'question')]}):
            data = self.client.post('/get_response', json={'task_id':'demo', 'bot_message_id':['wrong','id']}).get_json()
            self.assertIsNone(data['result'])
            self.assertEqual(data['status'], 'queued')

    def test_reply_persists_and_survives_reload(self):
        self.submit()
        with patch.object(server.bot_client, 'get_last_messages', return_value={'messages': [msg('reply', 'answer', reply='opaque-id')]}):
            self.assertEqual(self.client.get('/api/result?task_id=demo').get_json()['result'], 'answer')
        server.active_tasks.clear()
        server.load_tasks()
        self.assertEqual(server.active_tasks['demo']['result'], 'answer')
        self.assertEqual((Path(self.directory.name) / 'demo_result.md').read_text(), 'answer')

    def test_publish_result_idempotent_delivery(self):
        self.submit()
        with patch.object(server.bot_client, 'send_message', return_value={'message': msg('result', 'done')}) as send:
            for _ in range(2):
                response = self.client.post('/api/result', json={'task_id':'demo','result':'done'})
                self.assertTrue(response.get_json()['delivered'])
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.client.get('/api/result?task_id=demo').get_json()['result'], 'done')

    def test_failed_delivery_keeps_result_and_retries(self):
        self.submit()
        with patch.object(server.bot_client, 'send_message', return_value=None):
            data = self.client.post('/api/result', json={'task_id':'demo','result':'done'}).get_json()
            self.assertFalse(data['delivered'])
        self.assertEqual(server.active_tasks['demo']['result'], 'done')
        with patch.object(server.bot_client, 'send_message', return_value={'message': msg('result', 'done')}):
            self.assertTrue(self.client.post('/api/result', json={'task_id':'demo','result':'done'}).get_json()['delivered'])

    def test_incoming_event_idempotent_and_chat_isolated(self):
        update = dict(update_type='message_created', message=msg('inbound', 'new task'))
        with patch.object(server, 'CHAT_ID', '123'):
            server.handle_update(update)
            server.handle_update(update)
            server.handle_update(dict(update_type='message_created', message=msg('other', 'ignore', chat=999)))
        self.assertEqual(len(server.active_tasks), 1)

    def test_poll_reply_cannot_complete_other_chat(self):
        self.submit()
        with patch.object(server, 'CHAT_ID', None):
            server.handle_update(dict(update_type='message_created', message=msg('reply', 'wrong', reply='opaque-id', chat=999)))
        self.assertEqual(server.active_tasks['demo']['status'], 'queued')

    def test_replayed_reply_does_not_create_new_task(self):
        self.submit()
        update = dict(update_type='message_created', message=msg('reply', 'answer', reply='opaque-id'))
        with patch.object(server, 'CHAT_ID', None):
            server.handle_update(update)
            server.handle_update(update)
        self.assertEqual(len(server.active_tasks), 1)
        self.assertEqual(server.active_tasks['demo']['result'], 'answer')

    def test_send_failure_is_persisted(self):
        with patch.object(server.bot_client, 'send_task', return_value=None):
            response = self.client.post('/api/task', json={
                'task_id': 'demo', 'message': 'question', 'chat_id': 123})
        self.assertEqual(response.status_code, 502)
        server.active_tasks.clear()
        server.load_tasks()
        self.assertEqual(server.active_tasks['demo']['status'], 'failed')

    def test_failed_persistence_can_be_retried(self):
        update = dict(update_type='message_created', message=msg('incoming', 'task'))
        with patch.object(server, 'CHAT_ID', None):
            with patch.object(server, 'persist', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    server.handle_update(update)
            self.assertEqual(len(server.active_tasks), 0)
            server.handle_update(update)
            self.assertEqual(len(server.active_tasks), 1)

    def test_failed_result_persistence_keeps_task_queued(self):
        self.submit()
        task = server.active_tasks['demo']
        with patch.object(server, 'persist', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                server.complete_task(task, 'answer')
        self.assertEqual(task['status'], 'queued')

    def test_monitor_saves_marker_and_updates(self):
        server.stop_event.clear()
        data = {'updates': [dict(update_type='message_created', message=msg('incoming', 'task'))], 'marker': 42}
        def stop_after_batch(seconds):
            server.stop_event.set()
        with patch.object(server.bot_client, 'get_updates', return_value=data), \
                patch.object(server.stop_event, 'wait', side_effect=stop_after_batch), \
                patch.object(server, 'CHAT_ID', None):
            server.monitor_bot()
        self.assertEqual((Path(self.directory.name) / '.marker').read_text(), '42')
        self.assertEqual(len(server.active_tasks), 1)
        server.stop_event.clear()

    def test_web_ui_and_entrypoints(self):
        import run
        import run_simple
        self.assertIs(run.app, server.app)
        self.assertIs(run_simple.app, server.app)
        with self.client.get('/') as response:
            html = response.get_data(as_text=True)
        self.assertIn('id="sendBtn"', html)
        self.assertIn('encodeURIComponent(taskId)', html)


if __name__ == '__main__':
    unittest.main()
