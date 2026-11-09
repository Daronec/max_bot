import bot_test_support
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests
import server
from task_service import TaskService
from task_store import SQLiteTaskStore
from telegram_adapter import TelegramAdapter, TelegramClient, TelegramError


def update(ident=1, sender=42, chat=123456789, text='Привет'):
    return dict(update_id=ident, message=dict(message_id=ident, text=text,
                chat=dict(id=chat, type='private'), **{'from':dict(id=sender, is_bot=False)}))


class TelegramTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.store = SQLiteTaskStore(temporary.name)
        self.tasks = {}
        service = TaskService(self.tasks, threading.RLock(), self.store.save, self.store.get)
        self.client = Mock()
        self.adapter = TelegramAdapter(self.client, lambda:service, lambda:self.store, {'42'})

    def test_allowlist_dedup_and_namespace(self):
        self.adapter.handle(update())
        self.adapter.handle(update())
        self.adapter.handle(update(ident=2, sender=99))
        group = update(ident=3)
        group['message']['chat']['type'] = 'group'
        self.adapter.handle(group)
        self.assertEqual(len(self.tasks), 1)
        task = next(iter(self.tasks.values()))
        self.assertEqual(task['source'], 'telegram')
        self.assertEqual(task['user_id'], 'telegram:42')
        self.assertTrue(task['task_id'].startswith('tg_'))

    def test_offset_advances_only_after_durable_registration(self):
        stopped = threading.Event()
        calls = 0
        def fetch(offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [update()]
            self.assertEqual(offset, 2)
            stopped.set()
            return []
        self.client.updates.side_effect = fetch
        self.adapter.poll(stopped)
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.runtime('telegram_offset'), {'offset':2})

    def test_request_contract_and_secret_redaction(self):
        client = TelegramClient('private-token')
        with patch('telegram_adapter.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'ok':True,'result':{'message_id':3}}
            client.send(5, 'Ответ', 8)
            self.assertEqual(post.call_args.kwargs['json']['reply_parameters']['message_id'], 8)
            self.assertNotIn('parse_mode', post.call_args.kwargs['json'])
        with patch('telegram_adapter.requests.post', side_effect=requests.ConnectionError('https://botprivate-token')):
            with self.assertRaises(TelegramError) as raised:
                client.updates(0)
            self.assertNotIn('private-token', str(raised.exception))

    def test_telegram_chat_id_cannot_inherit_max_tool_permissions(self):
        with patch.object(server, 'TASKS_DIR', self.directory), patch.object(server, 'active_tasks', self.tasks):
            self.adapter.handle(update(text='Выполни команду pwd'))
            task = next(iter(self.tasks.values()))
            with patch.object(server, 'process_message', return_value='Ответ') as model, \
                    patch('telegram_adapter.TelegramClient.send', return_value={'message_id':99}) as send, \
                    patch.object(server.bot_client, 'send_message') as max_send:
                server.process_next_task()
            model.assert_called_once_with('Выполни команду pwd')
            send.assert_called_once()
            max_send.assert_not_called()
            self.assertTrue(task['delivered'])
