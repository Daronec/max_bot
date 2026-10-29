import bot_test_support  # Load synthetic configuration before application imports.
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import assistant_identity as identity
import server
import task_processor as processor
import voice_tools
from intent_router import RouteDecision


class IdentityTests(unittest.TestCase):
    def test_optional_addresses_and_aliases(self):
        for text, expected in [
            ('Агафья, проанализируй данные', 'проанализируй данные'),
            ('АГАФЬЯ проанализируй данные', 'проанализируй данные'),
            ('Агафия, открой файл', 'открой файл'),
            ('Привет, Агафья! Проанализируй данные', 'Проанализируй данные'),
            ('Проанализируй данные, Агафья!', 'Проанализируй данные'),
            ('Агафья!', ''), ('Привет, Агафья!', 'Привет'),
            ('Проанализируй данные', 'Проанализируй данные'),
            ('Напиши рассказ про Агафью', 'Напиши рассказ про Агафью'),
            ('Персонажа зовут Агафья', 'Персонажа зовут Агафья'),
            ('Агафьяна, привет', 'Агафьяна, привет'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(identity.strip_assistant_address(text), expected)

    def test_name_only_does_not_route_or_execute(self):
        with patch.object(processor.intent_router, 'route') as route, patch.object(processor, 'model_post') as post:
            self.assertEqual(processor.process_message('Агафья!'), identity.ADDRESS_REPLY)
            route.assert_not_called()
            post.assert_not_called()
        with patch.object(server, 'process_message') as process:
            self.assertEqual(server.execute_task_message('Агафья'), identity.ADDRESS_REPLY)
            process.assert_not_called()

    def test_identity_and_clean_task_for_both_models(self):
        for target in ('qwen', 'deepseek'):
            response=Mock()
            response.json.return_value={'choices':[{'message':{'content':'Меня зовут Агафья.'}}]}
            with patch.object(processor.intent_router, 'route', return_value=RouteDecision(target, 'general_chat', .9, 'test')) as route, \
                 patch.object(processor, 'model_post', return_value=response) as post:
                self.assertEqual(processor.process_message('Агафья, как тебя зовут?'), 'Меня зовут Агафья.')
                route.assert_called_once_with('как тебя зовут?')
                messages=post.call_args.kwargs['json']['messages']
                self.assertIn(identity.IDENTITY_PROMPT, messages[0]['content'])
                self.assertEqual(messages[1]['content'], 'как тебя зовут?')

    def test_message_without_name_still_runs(self):
        with patch.object(processor.intent_router, 'route', return_value=RouteDecision('qwen', 'general_chat', .9, 'test')) as route, \
             patch.object(processor, '_process_message', return_value='ответ') as process:
            self.assertEqual(processor.process_message('Как дела?'), 'ответ')
            route.assert_called_once_with('Как дела?')
            process.assert_called_once_with('Как дела?', allow_files=False, allow_commands=False)

    def test_mood_identity(self):
        response=Mock()
        response.json.return_value={'choices':[{'message':{'content':'Понимаю.'}}]}
        with patch.object(processor.intent_router, 'route', return_value=RouteDecision('qwen', 'general_chat', .9, 'test')), \
             patch.object(processor, 'model_post', return_value=response) as post:
            processor.comment_on_mood('Агафья, я устал')
            messages=post.call_args.kwargs['json']['messages']
            self.assertIn(identity.IDENTITY_PROMPT, messages[0]['content'])
            self.assertEqual(messages[1]['content'], 'я устал')

    def test_named_command_uses_same_permissions(self):
        with patch.object(server, 'process_message', return_value='готово') as process:
            self.assertEqual(server.execute_task_message('Агафья, запусти скрипт', allow_commands=True), 'готово')
            process.assert_called_once_with('запусти скрипт', allow_commands=True)

    def test_asr_vocabulary_hint_preserves_transcript(self):
        model=Mock()
        model.transcribe.return_value=(iter([SimpleNamespace(text=' Агафья, открой файл. ')]), None)
        with patch.object(voice_tools, '_model', model), patch.object(voice_tools, 'download_audio'), \
             patch.object(voice_tools, 'decode_bounded_audio', return_value='samples'):
            self.assertEqual(voice_tools.transcribe({'payload': {'url':'https://example.com/audio'}}), 'Агафья, открой файл.')
            self.assertEqual(model.transcribe.call_args.kwargs['hotwords'], 'Агафья')
            self.assertEqual(model.transcribe.call_args.kwargs['language'], 'ru')
            self.assertTrue(model.transcribe.call_args.kwargs['vad_filter'])

    def test_received_voice_and_text_have_same_task(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'TASKS_DIR', directory), \
             patch.object(server, 'CHAT_ID', None), patch.object(server.mood_checkin, 'match_answer', return_value=None), \
             patch.object(server, 'FS_ALLOWED_CHAT_IDS', frozenset({'123456789'})), \
             patch.object(server, 'transcribe', return_value='Агафия, проанализируй данные') as transcribe, \
             patch.object(server, 'process_message', return_value='ответ') as process, \
             patch.object(server.bot_client, 'send_message', return_value={'message':{'id':'sent'}}):
            previous=dict(server.active_tasks)
            self.addCleanup(lambda: (server.active_tasks.clear(), server.active_tasks.update(previous)))
            server.active_tasks.clear()
            message={'sender':{'is_bot':False},'recipient':{'chat_id':123456789},
                     'body':{'mid':'agafya-voice','text':None,'attachments':[{'type':'audio','payload':{'url':'https://example.com/audio'}}]}}
            server.handle_update({'update_type':'message_created','message':message})
            server.process_next_task()
            task=next(iter(server.active_tasks.values()))
            self.assertEqual(task['status'], 'completed')
            self.assertEqual(task['transcription'], 'Агафия, проанализируй данные')
            self.assertIn('Агафия, проанализируй данные',task['result'])
            self.assertEqual(process.call_args.args, ('проанализируй данные',))
            transcribe.assert_called_once()
            process.reset_mock()
            server.execute_task_message('Агафья, проанализируй данные')
            self.assertEqual(process.call_args.args, ('проанализируй данные',))
