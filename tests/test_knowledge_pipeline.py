import tempfile
from pathlib import Path
import unittest

from knowledge_pipeline import KnowledgePipeline
from knowledge_store import KnowledgeStore
from task_store import SQLiteTaskStore


class PipelineTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.docs=self.root/'docs';self.docs.mkdir()
        self.knowledge=KnowledgeStore(SQLiteTaskStore(self.root/'tasks'))
        self.pipeline=KnowledgePipeline(self.knowledge)
        self.ident=self.pipeline.add(str(self.docs))

    def test_import_update_delete_and_explicit_deletion_not_resurrected(self):
        path=self.docs/'notes.md';path.write_text('Маркер: аметист',encoding='utf-8')
        self.assertTrue(self.pipeline.tick(100))
        hits=self.knowledge.search('local_owner','аметист');self.assertEqual(len(hits),1)
        ident=hits[0]['document_id']
        path.write_text('Маркер: сапфир',encoding='utf-8');self.pipeline.tick(102)
        self.assertEqual(self.knowledge.search('local_owner','аметист'),[])
        self.assertEqual(self.knowledge.search('local_owner','сапфир')[0]['version'],2)
        self.knowledge.delete('local_owner',ident)
        path.write_text('Сапфир и аметист',encoding='utf-8');self.pipeline.tick(104)
        self.assertEqual(self.knowledge.list('local_owner'),[])
        path.unlink();self.pipeline.tick(140)
        self.assertEqual(self.pipeline.list()[0]['files'],[])

    def test_hidden_symlinks_and_bad_updates(self):
        (self.docs/'.secret.txt').write_text('тайна')
        (self.docs/'link.txt').symlink_to(self.docs/'.secret.txt')
        (self.docs/'valid.txt').write_text('Original кварц')
        self.pipeline.tick(100)
        self.assertEqual(len(self.knowledge.list('local_owner')),1)
        (self.docs/'valid.txt').write_bytes(b'\xff')
        self.pipeline.tick(102)
        self.assertEqual(self.knowledge.search('local_owner','кварц'),[])
        self.assertTrue(self.pipeline.list()[0]['files'][0]['error'])
        (self.docs/'valid.txt').write_text('New кварц')
        self.pipeline.tick(104)
        self.assertEqual(len(self.knowledge.search('local_owner','кварц')),1)
        self.pipeline.toggle(self.ident,False)
        (self.docs/'new.txt').write_text('disabled')
        self.assertFalse(self.pipeline.tick(999))

    def test_removed_source_file_clears_index(self):
        path=self.docs/'notes.txt';path.write_text('лунолёт')
        self.pipeline.tick(100);path.unlink();self.pipeline.tick(102)
        self.assertEqual(self.knowledge.search('local_owner','лунолёт'),[])


if __name__=='__main__':unittest.main()
