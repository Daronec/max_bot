import bot_test_support  # Load synthetic configuration before application imports.
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import mood_checkin
import task_processor


class MoodTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client=Mock();self.client.send_message.return_value={'message':{'body':{'mid':'question'}}}
        self.service=mood_checkin.MoodCheckin(self.client,Path(self.tmp.name)/'state')
        self.early=datetime(2026,10,22,8,tzinfo=timezone(timedelta(hours=3)))

    def send_question(self):
        self.service.tick(self.early)
        self.service.tick(self.early.replace(hour=9))

    def test_daily_once_and_restart(self):
        self.send_question()
        self.service.tick(self.early.replace(hour=10))
        mood_checkin.MoodCheckin(self.client,self.service.state).tick(self.early.replace(hour=11))
        self.client.send_message.assert_called_once_with('123456789','Как у тебя настроение?')

    def test_install_after_nine_starts_next_morning(self):
        self.service.tick(self.early.replace(hour=15))
        self.client.send_message.assert_not_called()
        self.service.tick((self.early+timedelta(days=1)).replace(hour=9))
        self.client.send_message.assert_called_once()

    def test_next_answer_once_and_duplicate_event_keeps_classification(self):
        self.send_question()
        current=self.early.replace(hour=9,minute=5)
        self.assertEqual(self.service.match_answer(123456789,'answer',current=current),'question')
        self.assertEqual(self.service.match_answer(123456789,'answer',current=current),'question')
        self.assertIsNone(self.service.match_answer(123456789,'other',current=current))

    def test_commands_other_replies_other_chats_and_old_events_ignored(self):
        self.send_question()
        current=self.early.replace(hour=10)
        self.assertIsNone(self.service.match_answer(123,'a',current=current))
        self.assertIsNone(self.service.match_answer(123456789,'a',is_command=True,current=current))
        self.assertIsNone(self.service.match_answer(123456789,'a',reply_mid='another',current=current))
        self.assertIsNone(self.service.match_answer(123456789,'a',message_time=self.early.timestamp(),current=current))
        self.assertEqual(self.service.match_answer(123456789,'a',reply_mid='question',current=current),'question')

    def test_failed_delivery_retries(self):
        self.service.tick(self.early)
        self.client.send_message.return_value=None
        with self.assertRaises(RuntimeError):self.service.tick(self.early.replace(hour=9))
        self.client.send_message.return_value={'message':{'id':'ok'}}
        self.service.tick(self.early.replace(hour=9,minute=1))
        self.assertEqual(self.client.send_message.call_count,2)

    def test_mood_reply_exposes_no_action_tools(self):
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'Понимаю, день непростой.'}}]}
        with patch.object(task_processor,'model_post',return_value=response) as post:
            answer=task_processor.comment_on_mood('Устал сегодня')
        self.assertIn('непростой',answer)
        self.assertNotIn('tools',post.call_args.kwargs['json'])

class MoodRoutingTests(unittest.TestCase):
    def test_incoming_answer_uses_comment_pipeline_and_replies_to_answer(self):
        import server
        with tempfile.TemporaryDirectory() as temp, patch.object(server,'TASKS_DIR',temp),patch.object(server,'CHAT_ID',None),patch.object(server.mood_checkin,'match_answer',return_value='question'),patch.object(server,'comment_on_mood',return_value='Понимаю тебя.') as comment,patch.object(server,'process_message') as execute,patch.object(server.bot_client,'send_message',return_value={'message':{'id':'sent'}}) as send:
            previous=dict(server.active_tasks)
            try:
                server.active_tasks.clear()
                server.handle_update({'update_type':'message_created','message':{'sender':{'is_bot':False},'recipient':{'chat_id':123456789},'body':{'mid':'answer','text':'Сегодня устал'},'link':{'type':'reply','mid':'question'}}})
                server.process_next_task()
                comment.assert_called_once_with('Сегодня устал')
                execute.assert_not_called()
                send.assert_called_once_with(123456789,'Понимаю тебя.',reply_to_message_id='answer')
            finally:
                server.active_tasks.clear();server.active_tasks.update(previous)
