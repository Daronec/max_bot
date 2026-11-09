import bot_test_support
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant_app.core import server
from assistant_app.core import task_processor
from assistant_app.core.execution_context import ExecutionContext, current_execution, execution_session
from assistant_app.knowledge.embeddings import LocalEmbeddings
from assistant_app.knowledge.index import KnowledgeStore
from assistant_app.core.task_store import SQLiteTaskStore, TaskConflict


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = KnowledgeStore(SQLiteTaskStore(temporary.name))

    def test_search_is_scoped_and_updates_replace_old_chunks(self):
        document = self.store.ingest('owner', 'notes.md', 'Секретный код проекта: аметист.')
        self.store.ingest('other', 'private.md', 'Аметист: чужие личные сведения.')
        hits = self.store.search('owner', 'аметист')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['document_id'], document['id'])
        self.assertIn('аметист', hits[0]['content'])
        updated = self.store.ingest('owner', 'notes.md', 'Новый код: сапфир.', document['id'])
        self.assertEqual(updated['version'], 2)
        self.assertEqual(self.store.search('owner', 'аметист'), [])
        self.assertEqual(len(self.store.search('owner', 'сапфир')), 1)
        with self.assertRaises(KeyError):
            self.store.document('other', document['id'])

    def test_deletion_removes_text_vector_and_search_records(self):
        document = self.store.ingest('owner', 'notes.txt', 'Уникальный маркер лунолёт')
        with self.assertRaises(KeyError):
            self.store.delete('other', document['id'])
        self.store.delete('owner', document['id'])
        self.assertEqual(self.store.list('owner'), [])
        self.assertEqual(self.store.search('owner', 'лунолёт'), [])
        with self.store.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_chunks').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_fts').fetchone()[0], 0)

    def test_hybrid_search_can_find_semantic_hit_without_word_overlap(self):
        embedder = Mock(model='local-embedding')
        embedder.embed.side_effect = [[[1., 0.]], [[1., 0.]]]
        store = KnowledgeStore(self.store.store, embedder)
        result = store.ingest('owner', 'notes.txt', 'Питомца зовут Барсик.')
        self.assertEqual(result['status'], 'hybrid_ready')
        hits = store.search('owner', 'имя кота')
        self.assertEqual(hits[0]['document_id'], result['id'])

    def test_embedding_failure_preserves_lexical_search(self):
        embedder = Mock(model='local', embed=Mock(side_effect=ValueError('not loaded')))
        store = KnowledgeStore(self.store.store, embedder)
        result = store.ingest('owner', 'notes.txt', 'Маркер: хризолит')
        self.assertEqual(result['status'], 'embedding_failed_text_ready')
        self.assertEqual(len(store.search('owner', 'хризолит')), 1)

    def test_embedding_endpoint_must_be_local(self):
        for url in ('https://remote.example/v1', 'http://remote.example/v1', 'http://user:secret@localhost/v1'):
            with self.assertRaises(ValueError):
                LocalEmbeddings(url, 'embed')


class DesktopChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patcher in (patch.object(server, 'TASKS_DIR', temporary.name),
                        patch.object(server, 'active_tasks', {})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = server.app.test_client()

    def test_desktop_turn_does_not_contact_max_and_next_turn_has_history(self):
        first = self.client.post('/api/chat', json={'message':'Меня зовут Олег'}).get_json()
        observed = []
        def respond(text, **permissions):
            observed.append(current_execution())
            return 'Здравствуйте'
        with patch.object(server, 'process_message', side_effect=respond), \
                patch.object(server.bot_client, 'send_message') as send, \
                patch.object(server.bot_client, 'get_last_messages') as poll:
            self.assertEqual(self.client.get('/api/result?task_id='+first['task_id']).status_code, 200)
            self.assertTrue(server.process_next_task())
            self.assertEqual(observed[0].history, [])
            second = self.client.post('/api/chat', json={'message':'Как меня зовут?', 'conversation_id':first['conversation_id']})
            self.assertEqual(second.status_code, 200)
            server.process_next_task()
            self.assertEqual([entry['role'] for entry in observed[1].history], ['user', 'assistant'])
            self.assertTrue(observed[1].local_only)
            send.assert_not_called()
            poll.assert_not_called()
        messages = self.client.get('/api/conversations/'+first['conversation_id']).get_json()['messages']
        self.assertEqual(len(messages), 4)

    def test_tool_context_taints_future_turns_and_disables_web_export(self):
        first=self.client.post('/api/chat',json={'message':'Прочитай файл','allow_tools':True,'use_knowledge':False}).get_json()
        seen=[]
        def respond(text,**permissions):seen.append(current_execution());return 'Содержимое файла: кварц'
        with patch.object(server,'process_message',side_effect=respond):
            server.process_next_task()
            self.client.post('/api/chat',json={'message':'Продолжи','conversation_id':first['conversation_id'],'model_target':'deepseek','use_knowledge':False})
            server.process_next_task()
        self.assertTrue(seen[1].local_only)
        self.assertFalse(seen[1].web_enabled)

    def test_turn_serialization_and_owner_isolation(self):
        first = server.task_store().submit_chat('one', 'owner')
        with self.assertRaises(TaskConflict):
            server.task_store().submit_chat('two', 'owner', first['conversation_id'])
        with self.assertRaises(KeyError):
            server.task_store().submit_chat('steal', 'other', first['conversation_id'])
        with self.assertRaises(KeyError):
            server.task_store().messages(first['conversation_id'], 'other')

    def test_knowledge_is_local_and_sources_have_provenance(self):
        source = server.knowledge_store().ingest('local_owner', 'notes.md', 'Пароль проекта не хранится здесь. Название проекта: Гелиос.')
        task = self.client.post('/api/chat', json={'message':'Гелиос', 'model_target':'deepseek'}).get_json()
        def respond(text, **permissions):
            context = current_execution()
            self.assertTrue(context.local_only)
            self.assertIn('Гелиос', context.knowledge)
            return 'Найдено'
        with patch.object(server, 'process_message', side_effect=respond), patch.object(server.bot_client, 'send_message') as send:
            server.process_next_task()
            send.assert_not_called()
        result = self.client.get('/api/result?task_id='+task['task_id']).get_json()
        self.assertEqual(result['sources'][0]['document_id'], source['id'])
        self.assertIn('Источники, переданные модели', result['result'])

    def test_privacy_policy_overrides_cloud_choice_before_routing(self):
        with execution_session(ExecutionContext(local_only=True, model_target='deepseek')), \
                patch.object(task_processor, '_process_message', return_value='answer'), \
                patch.object(task_processor, 'intent_session') as selection, \
                patch.object(task_processor.intent_router, 'route') as classify:
            self.assertEqual(task_processor.process_message('private'), 'answer')
            self.assertEqual(selection.call_args.args[0].target, 'qwen')
            classify.assert_not_called()

    def test_local_tool_permissions_keep_execution_on_local_backend(self):
        self.client.post('/api/chat', json={'message':'Прочитай файл notes.txt',
            'allow_tools':True, 'model_target':'deepseek'})
        def respond(text, **permissions):
            self.assertTrue(permissions)
            self.assertTrue(current_execution().local_only)
            return 'Локальная обработка'
        with patch.object(server, 'process_message', side_effect=respond):
            server.process_next_task()
