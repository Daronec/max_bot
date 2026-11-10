import tempfile
import unittest
from datetime import datetime, timezone
from assistant_app.services.automation_service import AutomationService
from assistant_app.core.task_store import SQLiteTaskStore
from assistant_app.core.task_service import TaskConflict


class AutomationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = SQLiteTaskStore(tmp.name)
        self.service = AutomationService(self.store)
        self.config = dict(title='Review', prompt='Summarize notes', kind='interval', interval=120, missed='once')

    def test_restart_dedup_and_no_overlap(self):
        ident = self.service.create(self.config, now=1000)
        tasks = self.service.tick(1120)
        self.assertEqual(len(tasks), 1)
        again = AutomationService(SQLiteTaskStore(self.store.directory))
        self.assertEqual(again.tick(1120), [])
        self.assertEqual(again.tick(1240), [])
        task = self.store.get(tasks[0]); task.update(status='completed', result='done', completed_at='now')
        self.store.save(task)
        self.assertEqual(len(again.tick(1360)), 1)
        self.assertEqual(task['automation_id'], ident)
        self.assertFalse(task['allow_tools'])

    def test_skip_and_catchup_once(self):
        self.service.create(dict(self.config, missed='skip'), now=1000)
        self.assertEqual(self.service.tick(2000), [])
        self.service.create(self.config, now=1000)
        self.assertEqual(len(self.service.tick(2000)), 1)
        self.assertEqual(self.service.tick(2000), [])

    def test_toggle_cas_and_disable(self):
        ident = self.service.create(self.config, now=1000)
        self.service.toggle(ident, False, 1)
        with self.assertRaises(TaskConflict):
            self.service.toggle(ident, True, 1)
        self.assertEqual(self.service.tick(9999999999), [])

    def test_delete_stops_future_runs_and_preserves_queued_run(self):
        ident = self.service.create(self.config, now=1000)
        [task_id] = self.service.tick(1120)
        self.service.toggle(ident, True, 1)
        with self.assertRaises(TaskConflict):
            self.service.delete(ident, 1)
        due = self.service.list()[0]['next_due']
        self.service.delete(ident, 2)

        restarted = AutomationService(SQLiteTaskStore(self.store.directory))
        self.assertEqual(restarted.list(), [])
        task = self.store.get(task_id)
        self.assertEqual(task['status'], 'queued')
        task.update(status='completed', result='done', completed_at='now')
        self.store.save(task)
        self.assertEqual(restarted.tick(due), [])
        self.assertEqual(self.store.get(task_id)['result'], 'done')

    def test_linked_channel_is_frozen_and_revocation_disables_schedule(self):
        from assistant_app.channels.identities import IdentityService
        initial=self.store.submit_chat('start','local_owner')
        initial.update(status='completed',result='done',completed_at='now',delivered=True);self.store.save(initial)
        identities=IdentityService(self.store)
        code=identities.issue('telegram')
        identities.command(dict(source='telegram',sender_id='42',chat_id=42,message=code['command']))
        link=identities.links()[0]['id'];identities.update(link,'confirm');identities.update(link,'conversation',initial['conversation_id'])
        ident=self.service.create(dict(self.config,channel_link=link),now=1000)
        tasks=self.service.tick(1120);self.assertEqual(len(tasks),1)
        task=self.store.get(tasks[0]);self.assertEqual(task['delivery_channel'],'telegram')
        self.assertEqual(task['conversation_id'],initial['conversation_id'])
        task.update(status='completed',result='scheduled',completed_at='now',delivered=True);self.store.save(task)
        identities.update(link,'revoke')
        self.assertEqual(self.service.tick(1240),[])
        self.assertFalse(self.service.list()[0]['enabled'])
        self.assertFalse(identities.delivery_allowed(task))

    def test_daily_timezone_and_dst(self):
        config = dict(title='daily', prompt='hello', kind='daily', time='02:30', timezone='America/New_York')
        before = datetime(2026, 3, 8, 5, tzinfo=timezone.utc).timestamp()
        # Spring transition removes 02:30; next valid date is March 9.
        due = datetime.fromtimestamp(self.service.following(config, before), timezone.utc)
        self.assertEqual(due.isoformat(), '2026-03-09T06:30:00+00:00')
        for bad in (dict(config, timezone='unknown'), dict(config, time='99:00'), dict(self.config, interval=True)):
            with self.assertRaises(ValueError): self.service.create(bad)


if __name__ == '__main__': unittest.main()
