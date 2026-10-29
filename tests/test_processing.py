import bot_test_support  # Load synthetic configuration before application imports.
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import server
import task_processor
from intent_router import RouteDecision
from test_bot import msg


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        routing = patch.object(task_processor.intent_router, 'route', return_value=RouteDecision('qwen', 'general_chat', .9, 'test'))
        routing.start()
        self.addCleanup(routing.stop)
        provider = patch.object(task_processor, 'LM_PROVIDER', 'lmstudio')
        provider.start()
        self.addCleanup(provider.stop)
        mode = patch.object(task_processor, 'LM_API_MODE', 'openai')
        mode.start()
        self.addCleanup(mode.stop)

    def test_native_lmstudio_disables_thinking_and_returns_final_text(self):
        with patch.object(task_processor, 'LM_API_MODE', 'lmstudio'), \
                patch.object(task_processor, 'WEB_ENABLED', False), \
                patch('config.LOCAL_LM_MODEL', 'qwen/qwen3.5-9b'), \
                patch.object(task_processor.requests, 'post') as post:
            post.return_value.json.return_value = {'output': [
                {'type': 'reasoning', 'content': 'internal'},
                {'type': 'message', 'content': 'answer'}]}
            self.assertEqual(task_processor.process_message('task'), 'answer')
            self.assertTrue(post.call_args.args[0].endswith('/api/v1/chat'))
            self.assertEqual(post.call_args.kwargs['json']['reasoning'], 'off')
            self.assertFalse(post.call_args.kwargs['json']['store'])

    def test_auto_select_model_and_generate(self):
        with patch('config.LOCAL_LM_MODEL', ''), \
                patch.object(task_processor.requests, 'get') as get, \
                patch.object(task_processor.requests, 'post') as post:
            get.return_value.json.return_value = {'data': [{'id': 'embed-model'}, {'id': 'local-chat'}]}
            post.return_value.json.return_value = {'choices': [{'message': {'content': ' Ответ '}}]}
            self.assertEqual(task_processor.process_message('Задание'), 'Ответ')
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['model'], 'local-chat')
            self.assertEqual(payload['messages'][-1], {'role':'user','content':'Задание'})

    def test_configured_model_skips_discovery(self):
        with patch('config.LOCAL_LM_MODEL', 'chosen'), \
                patch.object(task_processor.requests, 'get') as get, \
                patch.object(task_processor.requests, 'post') as post:
            post.return_value.json.return_value = {'choices': [{'message': {'content': 'answer'}}]}
            self.assertEqual(task_processor.process_message('task'), 'answer')
            get.assert_not_called()

    def test_unavailable_or_empty_model(self):
        with patch('config.LOCAL_LM_MODEL', ''), \
                patch.object(task_processor.requests, 'get', side_effect=requests.ConnectionError):
            with self.assertRaises(task_processor.ProcessingError):
                task_processor.process_message('task')
        with patch('config.LOCAL_LM_MODEL', 'local'), \
                patch.object(task_processor.requests, 'post') as post:
            post.return_value.json.return_value = {'choices': [{'message': {'content': ''}}]}
            with self.assertRaises(task_processor.ProcessingError):
                task_processor.process_message('task')


class AutomaticFlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(server, 'TASKS_DIR', tmp.name), patch.object(server, 'CHAT_ID', None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        server.active_tasks.clear()
        self.addCleanup(server.active_tasks.clear)

    def incoming(self, mid='inbound', chat=123, text='Задание'):
        server.handle_update({'update_type':'message_created','message':msg(mid,text,chat=chat)})
        return next(t for t in server.active_tasks.values() if t['bot_message_id'] == mid)

    def test_max_message_model_answer_same_chat_reply(self):
        task = self.incoming(chat=987)
        with patch.object(server, 'process_message', return_value='Готовый ответ') as process, \
                patch.object(server.bot_client, 'send_message', return_value={'message': msg('out', 'Готовый ответ')}) as send:
            self.assertTrue(server.process_next_task())
            process.assert_called_once_with('Задание')
            send.assert_called_once_with(987, 'Готовый ответ', reply_to_message_id='inbound')
        self.assertEqual(task['status'], 'completed')
        self.assertTrue(task['delivered'])
        self.assertEqual(task['result'], 'Готовый ответ')

    def test_incoming_reply_is_new_task_not_fake_result(self):
        task = self.incoming()
        server.handle_update({'update_type':'message_created','message':msg('followup', 'Уточнение', reply='inbound')})
        self.assertEqual(len(server.active_tasks), 2)
        self.assertEqual(task['status'], 'queued')

    def test_model_failure_keeps_task_and_notifies_once(self):
        task = self.incoming()
        with patch.object(server, 'process_message', side_effect=server.ProcessingError('Модель недоступна')), \
                patch.object(server.bot_client, 'send_message', return_value={'message':msg('notice','error')}) as send:
            server.process_next_task()
            self.assertEqual(task['status'], 'queued')
            self.assertIn('retry_at', task)
            self.assertFalse(server.process_next_task())
            task['retry_at'] = 0
            server.process_next_task()
            self.assertEqual(send.call_count, 1)

    def test_delivery_retry_does_not_regenerate(self):
        task = self.incoming()
        with patch.object(server, 'process_message', return_value='answer') as process, \
                patch.object(server.bot_client, 'send_message', side_effect=[None, {'message':msg('out','answer')}]):
            server.process_next_task()
            self.assertFalse(task['delivered'])
            task['delivery_retry_at'] = 0
            server.process_next_task()
            self.assertTrue(task['delivered'])
            self.assertEqual(process.call_count, 1)

    def test_long_answer_retry_resumes_unsent_chunk(self):
        task = self.incoming()
        answer = 'a' * 4500
        with patch.object(server, 'process_message', return_value=answer), \
                patch.object(server.bot_client, 'send_message', side_effect=[{'message':msg('one','a')}, None]) as send:
            server.process_next_task()
            self.assertEqual([len(call.args[1]) for call in send.call_args_list], [4000, 500])
            self.assertEqual(task['sent_chunks'], 1)
        task['delivery_retry_at'] = 0
        with patch.object(server, 'process_message') as process, \
                patch.object(server.bot_client, 'send_message', return_value={'message':msg('two','a')}) as send:
            server.process_next_task()
            process.assert_not_called()
            self.assertEqual(len(send.call_args.args[1]), 500)
        self.assertTrue(task['delivered'])

    def test_restore_interrupted_processing(self):
        task = self.incoming()
        task['status'] = 'processing'
        server.persist(task)
        server.active_tasks.clear()
        server.load_tasks()
        self.assertEqual(next(iter(server.active_tasks.values()))['status'], 'queued')

    def test_incoming_result_endpoint_does_not_require_history(self):
        task = self.incoming()
        with patch.object(server.bot_client, 'get_last_messages') as history:
            response = server.app.test_client().get('/api/result?task_id=' + task['task_id'])
            self.assertEqual(response.status_code, 200)
            history.assert_not_called()

    def test_bot_output_is_not_reprocessed(self):
        server.handle_update({'update_type':'message_created','message':msg('out','answer',bot=True)})
        self.assertEqual(server.active_tasks, {})


if __name__ == '__main__':
    unittest.main()
