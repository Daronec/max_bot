import bot_test_support
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from execution_context import current_execution
from identity_service import IdentityService
from personal_memory import Fact, MemoryStore, PersonalMemory
from task_store import SQLiteTaskStore


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteTaskStore(self.tmp.name)
        self.ids = IdentityService(self.store)

    def task(self, text, sender='42', chat=123):
        return dict(source='telegram', sender_id=sender, chat_id=chat, message=text)

    def link(self):
        code = self.ids.issue('telegram')
        self.ids.command(self.task(code['command']))
        return self.ids.links()[0]['id']

    def test_code_is_single_use_channel_bound_and_needs_panel_confirmation(self):
        code = self.ids.issue('telegram')
        wrong = self.task(code['command']); wrong['source'] = 'max'
        self.assertIn('недействителен', self.ids.command(wrong))
        self.ids.command(self.task(code['command']))
        self.assertEqual(self.ids.links()[0]['state'], 'pending')
        self.assertIn('недействителен', self.ids.command(self.task(code['command'], sender='99')))
        with self.assertRaises(ValueError):
            self.ids.update(self.ids.links()[0]['id'], 'conversation', 'missing')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pairing_codes').fetchone()[0], 0)

    def test_expiry_and_wrong_chat_cannot_use_binding(self):
        code = self.ids.issue('telegram')
        with self.store.connect() as db:
            db.execute('UPDATE pairing_codes SET expires=0')
        self.assertIn('недействителен', self.ids.command(self.task(code['command'])))
        ident = self.link()
        self.ids.update(ident, 'confirm')
        self.assertFalse(self.ids.attach(self.task('hello', chat=999)))
        self.ids.command(self.task('/unlink'))
        self.assertEqual(self.ids.links()[0]['state'], 'revoked')

    def test_shared_dialogue_local_history_and_revocation_before_delivery(self):
        with patch.object(server, 'TASKS_DIR', self.tmp.name), patch.object(server, 'active_tasks', {}):
            first = server.task_store().submit_chat('Начало диалога', 'local_owner', use_knowledge=False)
            seen = []
            def respond(text, **kw):
                seen.append(current_execution())
                return 'Ответ'
            with patch.object(server, 'process_message', side_effect=respond), patch.object(server, 'send_task_reply', return_value={'message':{'id':'ok'}}):
                server.process_next_task()
                ident = self.link()
                self.ids.update(ident, 'confirm')
                self.ids.update(ident, 'conversation', first['conversation_id'])
                task, _ = server.task_service().submit('tg_turn', 'Продолжение', 123, 'telegram', sender_id='42', bot_message_id=1)
                server.process_next_task()
                self.assertTrue(seen[-1].local_only)
                self.assertEqual([m['role'] for m in seen[-1].history], ['user', 'assistant'])
                stored = self.store.get('tg_turn')
                self.assertEqual(stored['conversation_id'], first['conversation_id'])
                self.assertTrue(self.ids.delivery_allowed(stored))
                self.ids.update(ident, 'revoke')
                self.assertFalse(self.ids.delivery_allowed(stored))
                self.assertTrue(self.store.conversation_privacy(first['conversation_id'], 'local_owner'))

    def test_cannot_select_another_owners_conversation(self):
        ident = self.link(); self.ids.update(ident, 'confirm')
        other = self.store.submit_chat('private', 'other')
        with self.assertRaises(KeyError):
            self.ids.update(ident, 'conversation', other['conversation_id'])

    def test_max_pairing_and_history_require_a_verified_private_dialog(self):
        code = self.ids.issue('max')
        task = dict(self.task(code['command']), source='max', chat_type='chat')
        self.assertIn('только в личном', self.ids.command(task))
        self.assertEqual(self.ids.links(), [])
        self.ids.command(dict(task, chat_type='dialog'))
        ident = self.ids.links()[0]['id']
        self.ids.update(ident, 'confirm')
        chat = self.store.submit_chat('private', 'local_owner')
        self.ids.update(ident, 'conversation', chat['conversation_id'])
        self.assertFalse(self.ids.attach(dict(task, message='group turn')))

    def test_repairing_does_not_reauthorize_old_deliveries(self):
        chat = self.store.submit_chat('private', 'local_owner')
        ident = self.link()
        self.ids.update(ident, 'confirm')
        self.ids.update(ident, 'conversation', chat['conversation_id'])
        old = dict(self.task('old answer'), channel_link=ident, conversation_id=chat['conversation_id'])
        self.assertTrue(self.ids.delivery_allowed(old))
        replacement = self.link()
        self.ids.update(replacement, 'confirm')
        self.ids.update(replacement, 'conversation', chat['conversation_id'])
        self.assertFalse(self.ids.delivery_allowed(old))
        with self.assertRaises(PermissionError):
            self.ids.attach(old)

    def test_shared_memory_with_private_links_and_without_shared_history(self):
        legacy = MemoryStore(Path(self.tmp.name) / 'old-max.sqlite3')
        legacy_scope = hashlib.sha256(b'55:10').hexdigest()
        legacy.save(legacy_scope, 'old', [Fact('personal', 'self.birth_date', 'Я родился 12.11.1990'),
                                         Fact('personal', 'self.address', 'Мой адрес: старый адрес')])
        with patch.object(server, 'TASKS_DIR', self.tmp.name), patch.object(server, 'active_tasks', {}), \
             patch.object(server, 'personal_memory', PersonalMemory(legacy, enabled=True, allowed_chats={'55'})), \
             patch.object(server, 'send_task_reply', return_value={'message':{'id':'ok'}}), \
             patch.object(server, 'process_message', return_value='Обычный ответ') as model:
            memory = server.desktop_memory()
            memory.save('personal', 'address', 'Мой адрес: адрес из панели')
            for channel, chat, sender, kind in [('max', 55, '10', 'dialog'), ('telegram', 123, '42', 'private')]:
                code = self.ids.issue(channel)['command']
                self.ids.command(dict(source=channel, chat_id=chat, sender_id=sender, chat_type=kind, message=code))
                self.ids.update(self.ids.links()[0]['id'], 'confirm')

            def turn(task_id, message, source='telegram', **metadata):
                defaults = dict(sender_id='42', chat_type='private') if source=='telegram' else dict(sender_id='10', chat_type='dialog')
                server.task_service().submit(task_id, message, 123 if source=='telegram' else 55, source, **(defaults | metadata))
                self.assertTrue(server.process_next_task())
                return self.store.get(task_id)

            birth = turn('birth', 'Когда я родился?')
            self.assertIn('12.11.1990', birth['result'])
            self.assertNotIn('conversation_id', birth)
            self.assertIn('адрес из панели', turn('address', 'Какой мой адрес?')['result'])
            self.assertIn('Запомнила', turn('save', 'Запомни: мой адрес — новый адрес')['result'])
            self.assertIn('новый адрес', turn('max-address', 'Где я живу?', 'max')['result'])
            self.assertIn('новый адрес', memory.command('Какой мой адрес?', 'panel-read'))
            def with_context(text, **permissions):
                context = current_execution()
                self.assertTrue(context.local_only)
                self.assertFalse(context.web_enabled)
                self.assertIn('новый адрес', context.personal_context)
                return 'Контекст получен'
            model.side_effect = with_context
            self.assertEqual(turn('generic', 'Что ты знаешь о моём адресе?')['result'], 'Контекст получен')
            model.reset_mock()
            self.assertIn('недоступна', turn('foreign', 'Когда я родился?', sender_id='99')['result'])
            self.assertIn('недоступна', turn('forward', 'Когда я родился?', memory_excluded=True)['result'])
            self.assertIn('недоступна', turn('group', 'Когда я родился?', chat_type='group')['result'])
            self.ids.command(self.task('/unlink'))
            self.assertFalse(self.ids.delivery_allowed(birth))
            self.assertIn('недоступна', turn('revoked', 'Когда я родился?')['result'])
            memory.delete('self.birth_date')
            self.assertIn('Не нашла', turn('deleted', 'Когда я родился?', 'max')['result'])
            model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
