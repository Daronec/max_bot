import bot_test_support
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from data_admin import delete_conversation,retention_candidates
from storage_admin import restore
from task_store import SQLiteTaskStore
from task_service import TaskConflict


class DataAdminTests(unittest.TestCase):
    def test_delete_and_restore_do_not_resurrect_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SQLiteTaskStore(Path(directory)/'tasks')
            task=store.submit_chat('private','local_owner')
            with self.assertRaises(TaskConflict):delete_conversation(store,task['conversation_id'])
            task.update(status='completed',result='answer',completed_at='now',delivered=True);store.save(task)
            snapshot=Path(directory)/'snapshot.sqlite3';store.backup(snapshot)
            self.assertEqual(delete_conversation(store,task['conversation_id']),1)
            self.assertEqual(store.conversations('local_owner'),[])
            restore(store.directory,snapshot)
            self.assertEqual(store.conversations('local_owner'),[])
            self.assertIsNone(store.get(task['task_id']))

    def test_retention_is_bounded_and_excludes_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tasks=root/'tasks';tasks.mkdir();(root/'logs').mkdir()
            (root/'.env').write_text('private');(root/'logs'/'old.log').write_text('old');(root/'logs'/'keep.sqlite3').write_text('db')
            result=retention_candidates(root,tasks,1,now=9999999999)
            self.assertEqual(result,[(root/'logs'/'old.log').resolve()])


if __name__=='__main__':unittest.main()
