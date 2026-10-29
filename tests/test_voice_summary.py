import bot_test_support  # Load synthetic configuration before application imports.
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import morning_summary
import server
import voice_tools


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.saved=dict(server.active_tasks)
        server.active_tasks.clear()
        self.addCleanup(lambda: (server.active_tasks.clear(), server.active_tasks.update(self.saved)))
        patcher=patch.object(server,'TASKS_DIR',self.temp.name)
        patcher.start();self.addCleanup(patcher.stop)

    def update(self,chat=123456789):
        return {'update_type':'message_created','message':{'sender':{'is_bot':False},'recipient':{'chat_id':chat},'body':{'mid':'voice1','text':None,'attachments':[{'type':'audio','payload':{'url':'https://example.com/signed'}}]}}}

    def test_voice_without_text_is_persisted_once(self):
        server.handle_update(self.update());server.handle_update(self.update())
        self.assertEqual(len(server.active_tasks),1)
        self.assertIn('audio',next(iter(server.active_tasks.values())))

    def test_other_chat_voice_does_not_get_owner_permissions(self):
        server.handle_update(self.update(123))
        self.assertFalse(server.active_tasks)

    def test_transcript_survives_model_failure_no_retranscription(self):
        server.handle_update(self.update())
        with patch.object(server,'transcribe',return_value='Привет') as transcribe, patch.object(server,'process_message',side_effect=[server.ProcessingError('offline'),'Привет!']), patch.object(server.bot_client,'send_message',return_value={'message':{'id':'sent'}}):
            server.process_next_task()
            task=next(iter(server.active_tasks.values()))
            self.assertEqual(task['transcription'],'Привет')
            self.assertNotIn('audio',task)
            task['retry_at']=0
            server.process_next_task()
            transcribe.assert_called_once()
            self.assertEqual(task['status'],'completed')

    def test_failed_asr_never_executes_a_task(self):
        server.handle_update(self.update())
        with patch.object(server,'transcribe',side_effect=voice_tools.VoiceError('Повторите запись')),patch.object(server,'process_message') as model,patch.object(server.bot_client,'send_message',return_value={'message':{'id':'sent'}}):
            server.process_next_task()
            model.assert_not_called()
            self.assertEqual(next(iter(server.active_tasks.values()))['result'],'Повторите запись')

    def test_private_audio_url_is_rejected(self):
        with patch.object(voice_tools,'public_target',side_effect=ValueError('private')):
            with self.assertRaises(ValueError): voice_tools.download_audio('https://127.0.0.1/a',Path(self.temp.name)/'audio')


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.client=Mock();self.client.send_message.return_value={'message':{'id':'sent'}}
        self.service=morning_summary.MorningSummary(self.client,Path(self.temp.name)/'state')
        self.early=datetime(2026,10,19,7,tzinfo=timezone(timedelta(hours=3)))

    def test_once_per_day_and_restart_dedup(self):
        with patch.object(morning_summary,'build_summary',return_value='Summary') as build:
            self.service.tick(self.early)
            self.service.tick(self.early.replace(hour=8))
            self.service.tick(self.early.replace(hour=9))
            morning_summary.MorningSummary(self.client,self.service.state).tick(self.early.replace(hour=10))
        build.assert_called_once();self.client.send_message.assert_called_once()

    def test_first_install_after_morning_waits_until_next_day(self):
        with patch.object(morning_summary,'build_summary',return_value='Summary'):
            self.service.tick(self.early.replace(hour=15))
            self.client.send_message.assert_not_called()
            self.service.tick((self.early+timedelta(days=1)).replace(hour=8))
            self.client.send_message.assert_called_once()

    def test_failed_send_retries_same_persisted_summary(self):
        self.service.tick(self.early)
        self.client.send_message.side_effect=[None,{'message':{'id':'sent'}}]
        with patch.object(morning_summary,'build_summary',return_value='Summary') as build:
            with self.assertRaises(RuntimeError):self.service.tick(self.early.replace(hour=8))
            self.service.tick(self.early.replace(hour=8,minute=1))
            build.assert_called_once()
        self.assertEqual(self.client.send_message.call_count,2)

    def test_partial_data_errors_explicit_and_single_message_bounded(self):
        agenda={'events':[{'start':'2026-10-19T08:00:00Z','title':'Meeting','all_day':False}]*100,'reminders':[],'errors':['Нет доступа к напоминаниям']}
        with patch.object(morning_summary,'read_agenda',return_value=agenda),patch.object(morning_summary.NotificationForwarder,'_rows',return_value=[]):
            text=morning_summary.build_summary(current=self.early)
        self.assertLessEqual(len(text),3900)
        self.assertIn('Meeting',text)
        self.assertIn('Нет доступа к напоминаниям',text)
        self.assertIn('… ещё',text)

class AudioDecodeTests(unittest.TestCase):
    def test_wave_without_speech_decodes_bounded_samples(self):
        import wave
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'silence.wav'
            with wave.open(str(path),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(b'\0\0'*3200)
            samples=voice_tools.decode_bounded_audio(path)
            self.assertEqual(len(samples),3200)
            self.assertFalse(samples.any())
            with patch.object(voice_tools,'VOICE_MAX_SECONDS',0.1):
                with self.assertRaises(voice_tools.VoiceError):voice_tools.decode_bounded_audio(path)

class AttachmentVariantsTests(unittest.TestCase):
    def test_audio_file_extensions_are_recognized(self):
        for extension in ('m4a','mp3','OGG','opus','wav'):
            attachment={'type':'file','filename':'recording.'+extension,'payload':{'url':'https://example.com/audio'}}
            self.assertEqual(voice_tools.audio_attachment({'body':{'attachments':[attachment]}}),attachment)
        self.assertIsNone(voice_tools.audio_attachment({'body':{'attachments':[{'type':'file','filename':'script.py'}]}}))

    def test_empty_max_event_diagnostic_no_guessed_recipient(self):
        before=server.voice_diagnostics['empty_message_events']
        with patch.object(server.bot_client,'send_message') as send:
            server.handle_update({'update_type':'message_created'})
            send.assert_not_called()
        self.assertEqual(server.voice_diagnostics['empty_message_events'],before+1)
