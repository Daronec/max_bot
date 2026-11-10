import bot_test_support
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from assistant_app.core.task_service import TaskConflict
from assistant_app.services.background_services import BackgroundCoordinator, BackgroundSettings, OutboxClient
from assistant_app.services.morning_summary import MorningSummary
from assistant_app.services.mood_checkin import MoodCheckin
from assistant_app.core.task_store import SQLiteTaskStore


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

    @patch('assistant_app.config.SUMMARY_ENABLED', True)
    def test_deleted_service_stays_absent_and_does_not_run_after_restart(self):
        settings = BackgroundSettings(self.store)
        settings.update('mood', True, 0)
        with self.assertRaises(TaskConflict):
            settings.delete('summary', 0)
        settings.delete('summary', 1)

        reopened = BackgroundSettings(SQLiteTaskStore(self.store.directory))
        self.assertEqual(reopened.get()['deleted'], ['summary'])
        self.assertFalse(reopened.get()['enabled']['summary'])
        with self.assertRaisesRegex(ValueError, 'уже удалён'):
            reopened.update('summary', True, 2)

        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        summary, mood = Mock(), Mock()
        BackgroundCoordinator([('summary', summary, 30), ('mood', mood, 30)], reopened).run(stop)
        summary.tick.assert_not_called()
        mood.tick.assert_called_once()

    def test_summary_and_mood_queue_and_actual_mood_receipt(self):
        summary=MorningSummary(self.client,state=self.root/'summary.sqlite3')
        mood=MoodCheckin(self.client,state=self.root/'mood.sqlite3')
        morning=datetime(2026,11,8,6,tzinfo=timezone.utc)
        later=datetime(2026,11,8,23,tzinfo=timezone.utc)
        with patch('assistant_app.services.morning_summary.SUMMARY_CHAT_ID','123456789'),patch('assistant_app.services.morning_summary.SUMMARY_TIME','09:00'),patch('assistant_app.services.morning_summary.build_summary',return_value='summary'),patch('assistant_app.services.mood_checkin.MOOD_CHAT_ID','123456789'),patch('assistant_app.services.mood_checkin.MOOD_TIME','18:00'):
            summary.tick(morning);mood.tick(morning)
            summary.tick(later);mood.tick(later)
            summary.tick(later);mood.tick(later)
        tasks=self.store.all();self.assertEqual(len(tasks),2)
        question=next(t for t in tasks if t['background_purpose']=='mood')
        mood.record_outbox_receipt(question['task_id'],'actual-max-id')
        with mood.db() as db:self.assertEqual(db.execute('SELECT mid FROM questions').fetchone()[0],'actual-max-id')
        self.transport.send_message.assert_not_called()


if __name__=='__main__':unittest.main()
