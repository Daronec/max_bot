import bot_test_support
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from background_services import OutboxClient
from morning_summary import MorningSummary
from mood_checkin import MoodCheckin
from task_store import SQLiteTaskStore


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=SQLiteTaskStore(self.root/'tasks');self.transport=Mock();self.client=OutboxClient(self.transport,self.store)

    def test_durable_enqueue_survives_legacy_replay_without_sending(self):
        one=self.client.send_outbox(123,'message',key='day',purpose='summary')
        two=OutboxClient(self.transport,SQLiteTaskStore(self.root/'tasks')).send_outbox(123,'message',key='day',purpose='summary')
        self.assertEqual(one,two);self.transport.send_message.assert_not_called()
        tasks=self.store.all();self.assertEqual(len(tasks),1)
        task=tasks[0]
        self.assertEqual(task['status'],'completed');self.assertFalse(task['delivered'])

    def test_summary_and_mood_queue_and_actual_mood_receipt(self):
        summary=MorningSummary(self.client,state=self.root/'summary.sqlite3')
        mood=MoodCheckin(self.client,state=self.root/'mood.sqlite3')
        morning=datetime(2026,11,8,6,tzinfo=timezone.utc)
        later=datetime(2026,11,8,23,tzinfo=timezone.utc)
        with patch('morning_summary.SUMMARY_CHAT_ID','123456789'),patch('morning_summary.SUMMARY_TIME','09:00'),patch('morning_summary.build_summary',return_value='summary'),patch('mood_checkin.MOOD_CHAT_ID','123456789'),patch('mood_checkin.MOOD_TIME','18:00'):
            summary.tick(morning);mood.tick(morning)
            summary.tick(later);mood.tick(later)
            summary.tick(later);mood.tick(later)
        tasks=self.store.all();self.assertEqual(len(tasks),2)
        question=next(t for t in tasks if t['background_purpose']=='mood')
        mood.record_outbox_receipt(question['task_id'],'actual-max-id')
        with mood.db() as db:self.assertEqual(db.execute('SELECT mid FROM questions').fetchone()[0],'actual-max-id')
        self.transport.send_message.assert_not_called()


if __name__=='__main__':unittest.main()
