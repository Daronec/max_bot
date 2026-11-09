import bot_test_support
import tempfile
import unittest
from unittest.mock import patch
import server
from execution_context import current_execution
from project_context import ProjectContext
from task_store import SQLiteTaskStore
from task_service import TaskConflict


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.directory=tmp.name
        self.store=SQLiteTaskStore(tmp.name);self.projects=ProjectContext(self.store)

    def test_project_binding_is_immutable_and_versioned(self):
        first=self.projects.save('One','Approved facts')
        second=self.projects.save('Two','Other facts')
        chat=self.store.submit_chat('hello','local_owner',project_id=first)
        self.assertEqual(chat['project_id'],first)
        with self.assertRaises(ValueError):self.store.submit_chat('switch','local_owner',chat['conversation_id'],project_id=second)
        with self.assertRaises(KeyError):self.store.submit_chat('steal','other',project_id=first)
        self.projects.save('One','Corrected',first,1)
        with self.assertRaises(TaskConflict):self.projects.save('One','Overwrite',first,1)
        self.assertIn('Corrected',self.projects.context(first))

    def test_project_retrieval_and_context_do_not_mix(self):
        with patch.object(server,'TASKS_DIR',self.directory),patch.object(server,'active_tasks',{}):
            one=self.projects.save('One','Project Orion')
            two=self.projects.save('Two','Project Vega')
            server.knowledge_store().ingest(self.projects.scope(one),'notes.md','Orion has marker quartz')
            server.knowledge_store().ingest(self.projects.scope(two),'notes.md','Vega has marker quartz')
            server.knowledge_store().ingest('local_owner','notes.md','General marker quartz')
            task=self.store.submit_chat('quartz','local_owner',project_id=one)
            seen=[]
            with patch.object(server,'process_message',side_effect=lambda *a,**k:(seen.append(current_execution()) or 'reply')):
                server.process_next_task()
            self.assertIn('Orion',seen[0].knowledge)
            self.assertNotIn('Vega',seen[0].knowledge)
            self.assertNotIn('General',seen[0].knowledge)
            self.assertTrue(seen[0].local_only)
            source=server.task_store().get(task['task_id'])['sources'][0]
            self.assertIn('?project_id='+one,source['url'])
            client=server.app.test_client()
            self.assertEqual(client.get(source['url']).status_code,200)
            self.assertEqual(client.get('/api/knowledge/'+source['document_id']).status_code,404)


if __name__=='__main__':unittest.main()
