import bot_test_support
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant_app.core import server
from assistant_app.channels.telegram import TelegramAdapter, TelegramClient, TelegramError


class SpeechTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.directory=tmp.name
        for patcher in (patch.object(server,'TASKS_DIR',tmp.name),patch.object(server,'active_tasks',{})):
            patcher.start();self.addCleanup(patcher.stop)
        self.client=server.app.test_client()

    def test_upload_transcript_durable_and_no_tool_permission(self):
        response=self.client.post('/api/chat/audio',data={'audio':(io.BytesIO(b'fake-audio'),'sample.wav')})
        self.assertEqual(response.status_code,200)
        tid=response.get_json()['task_id']
        path=Path(self.directory)/'audio'/server.task_store().get(tid)['audio_upload']
        seen=[]
        def respond(text,**permissions):seen.append((text,permissions));return 'Text reply'
        with patch('assistant_app.speech.service.transcribe_local',return_value='Распознанная речь'),patch.object(server,'process_message',side_effect=respond):
            self.assertTrue(server.process_next_task())
        self.assertFalse(path.exists())
        task=server.task_store().get(tid)
        self.assertEqual(task['transcription'],'Распознанная речь')
        self.assertFalse(task.get('allow_tools'))
        self.assertEqual(seen,[('Распознанная речь',{})])
        self.assertEqual(server.task_store().messages(task['conversation_id'],'local_owner')[0]['content'],'Распознанная речь')

    def test_empty_audio_and_invalid_conversation_leave_no_file(self):
        self.assertEqual(self.client.post('/api/chat/audio',data={'audio':(io.BytesIO(b''),'empty.wav')}).status_code,400)
        self.assertEqual(self.client.post('/api/chat/audio',data={'audio':(io.BytesIO(b'abc'),'a.wav'),'conversation_id':'bad'}).status_code,400)
        self.assertEqual(list((Path(self.directory)/'audio').iterdir()),[])

    def test_telegram_audio_persists_file_id_not_token_url(self):
        adapter=TelegramAdapter(Mock(),server.task_service,server.task_store,{'42'})
        adapter.handle(dict(message=dict(message_id=1,chat=dict(type='private',id=42),voice=dict(file_id='file-reference',duration=12,file_size=99),**{'from':dict(id=42)})))
        task=next(iter(server.active_tasks.values()))
        self.assertEqual(task['telegram_audio'],'file-reference')
        self.assertNotIn('bot',task['telegram_audio'])
        with patch.object(TelegramClient,'download_audio'),patch('assistant_app.speech.service.transcribe_local',return_value='Hello'),patch.object(server,'process_message',return_value='Reply'),patch('assistant_app.speech.service.synthesize_reply',return_value=task['task_id']+'.mp3') as synthesize,patch.object(server,'send_task_reply',return_value={'message':{'id':'ok'}}) as deliver:
            server.process_next_task()
        synthesize.assert_called_once_with('Reply',task['task_id'],server.task_store(),server.BASE_DIR)
        self.assertEqual(deliver.call_count,2)
        self.assertIn('Reply',deliver.call_args_list[0].args[1])
        self.assertNotIn('speech_path',deliver.call_args_list[0].kwargs)
        self.assertTrue(deliver.call_args_list[1].kwargs['speech_path'].endswith(task['task_id']+'.mp3'))
        self.assertEqual(server.task_store().get(task['task_id'])['transcription'],'Hello')

    def test_telegram_voice_synthesis_failure_keeps_text_delivery(self):
        task,_=server.task_service().submit('tg_audio','voice',42,'telegram',sender_id='42',bot_message_id=1,telegram_audio='asset')
        with patch('assistant_app.speech.service.transcribe_task',return_value='Вопрос'),patch.object(server,'process_message',return_value='Ответ'),patch('assistant_app.speech.service.synthesize_reply',side_effect=ValueError),patch.object(server,'send_task_reply',return_value={'message':{'id':'ok'}}) as deliver:
            server.process_next_task()
        self.assertEqual(deliver.call_count,1)
        self.assertTrue(server.task_store().get(task['task_id'])['delivered'])

    def test_speak_cannot_select_another_senders_reply(self):
        one,_=server.task_service().submit('tg_old','original',42,'telegram',sender_id='other',bot_message_id=1)
        server.complete_task(one,'private reply')
        command,_=server.task_service().submit('tg_speak','/speak',42,'telegram',sender_id='42',bot_message_id=2)
        with patch('assistant_app.speech.service.synthesize_task') as synthesis,patch.object(server,'send_task_reply',return_value={'message':{'id':'ok'}}):
            server.process_next_task()
        synthesis.assert_not_called()
        self.assertIn('Нет доступного ответа',server.task_store().get(command['task_id'])['result'])

    def test_telegram_download_error_never_exposes_token(self):
        client=TelegramClient('private-token')
        with patch.object(client,'call',return_value={'file_path':'voice/audio.oga'}),patch('assistant_app.channels.telegram.requests.get',side_effect=__import__('requests').ConnectionError('private-token')):
            with self.assertRaises(TelegramError) as error:client.download_audio('id','unused')
        self.assertNotIn('private-token',str(error.exception))


if __name__=='__main__':unittest.main()
