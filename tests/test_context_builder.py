import tempfile
import unittest
from context_builder import ContextBuilder
from task_store import SQLiteTaskStore


class ContextTests(unittest.TestCase):
    def test_bounded_versioned_excerpts_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SQLiteTaskStore(directory);conversation=None
            for index in range(12):
                task=store.submit_chat(('Запись '+str(index)+' ')*100,'local_owner',conversation)
                conversation=task['conversation_id'];task.update(status='completed',result='Ответ '*100,completed_at='now',delivered=True);store.save(task)
            store.submit_chat('Текущий вопрос','local_owner',conversation)
            builder=ContextBuilder(store)
            first=builder.build(conversation,'local_owner',recent_bytes=1200,rollup_bytes=1200)
            self.assertEqual(first['summary_version'],1)
            self.assertIn('Неполный архивный обзор',first['history'][0]['content'])
            self.assertLess(sum(len(item['content'].encode()) for item in first['history']),3200)
            self.assertEqual(builder.build(conversation,'local_owner',recent_bytes=1200,rollup_bytes=1200),first)
            with self.assertRaises(KeyError):builder.build(conversation,'other')


if __name__=='__main__':unittest.main()
