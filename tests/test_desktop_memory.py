import bot_test_support
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop_memory import DesktopMemory
from execution_context import current_execution
import server
from task_service import TaskConflict


class DesktopMemoryTests(unittest.TestCase):
    def test_birth_date_query_reads_existing_panel_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = DesktopMemory(Path(directory) / 'facts.sqlite3')
            memory.save('personal', 'self.date_of_birth', 'Я родился 12.11.1990')
            self.assertIn('12.11.1990', memory.command('Какая моя дата рождения?', 'read'))

    def test_explicit_facts_have_versions_and_secrets_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = DesktopMemory(Path(directory) / 'facts.sqlite3')
            memory.save('personal', 'self.name', 'Меня зовут Олег')
            original = memory.facts()[0]
            self.assertTrue(original['source_id'].startswith('panel:'))
            self.assertIn('Олег', memory.context('Как меня зовут?'))
            with self.assertRaises(TaskConflict):
                memory.save('personal', 'self.name', 'Меня зовут Иван')
            memory.save('personal', 'self.name', 'Меня зовут Иван', original['updated_at'])
            with self.assertRaises(ValueError):
                memory.save('personal', 'self.password', 'Мой пароль: secret-value')
            with patch('desktop_memory.QwenExtractor') as extract:
                self.assertIn('не сохраняются', memory.command('Запомни мой пароль: secret-value', 'event'))
                extract.assert_not_called()
            memory.delete('self.name')
            self.assertEqual(memory.facts(), [])

    def test_private_context_remains_local_in_later_turns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'TASKS_DIR', directory), \
                patch.object(server, 'active_tasks', {}):
            client = server.app.test_client()
            first = client.post('/api/chat', json={'message':'Какой код проекта?', 'model_target':'deepseek'}).get_json()
            server.knowledge_store().ingest('local_owner', 'notes.md', 'Код проекта: сапфир')
            contexts = []
            def answer(text, **permissions):
                contexts.append(current_execution())
                return 'Код: сапфир'
            with patch.object(server, 'process_message', side_effect=answer):
                server.process_next_task()
                second = client.post('/api/chat', json={'message':'Напиши короткий стих',
                    'conversation_id':first['conversation_id'], 'model_target':'deepseek', 'use_knowledge':False})
                self.assertEqual(second.status_code, 200)
                server.process_next_task()
            self.assertTrue(contexts[0].local_only)
            self.assertTrue(contexts[1].local_only)
            self.assertTrue(server.task_store().conversation_privacy(first['conversation_id'], 'local_owner'))
