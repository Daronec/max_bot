import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from backup_bundle import create, extract, prepare_installation
from task_store import SQLiteTaskStore


class BundleTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)/'project';self.root.mkdir()
        self.store=SQLiteTaskStore(self.root/'tasks')
        (self.root/'app.py').write_text('print("hello")')
        (self.root/'.env').write_text('SECRET=private')
        (self.root/'.secrets').mkdir();(self.root/'.secrets'/'panel.token').write_text('private')
        self.bundle=Path(tmp.name)/'backup.zip'

    def test_export_includes_memories_and_sources_without_credentials(self):
        path=self.root/'tasks'/'personal-memory.sqlite3'
        with sqlite3.connect(path) as db: db.execute('CREATE TABLE facts (value TEXT)');db.execute("INSERT INTO facts VALUES('hello')")
        manifest=create(self.root,self.root/'tasks',self.bundle)
        self.assertIn('data/personal-memory.sqlite3',manifest['files'])
        self.assertIn('project/app.py',manifest['files'])
        self.assertNotIn('project/.env',manifest['files'])
        destination=self.root.parent/'export'
        self.assertEqual(extract(self.bundle,destination),manifest)
        self.assertEqual((destination/'project'/'app.py').read_text(),'print("hello")')
        self.assertEqual(self.bundle.stat().st_mode&0o777,0o600)
        with self.assertRaises(ValueError):extract(self.bundle,destination)

    def test_clean_installation_preserves_data_but_does_not_replay_work(self):
        from automation_service import AutomationService
        task=self.store.submit_chat('pending','local_owner')
        AutomationService(self.store).create(dict(title='daily',prompt='hello',kind='interval',interval=60))
        create(self.root,self.root/'tasks',self.bundle)
        destination=self.root.parent/'new-installation'
        prepare_installation(self.bundle,destination)
        restored=SQLiteTaskStore(destination/'tasks')
        self.assertEqual(restored.get(task['task_id'])['status'],'failed')
        self.assertFalse(AutomationService(restored).list()[0]['enabled'])
        self.assertEqual((destination/'app.py').read_text(),'print("hello")')
        self.assertFalse((destination/'.env').exists())

    def test_requires_stopped_runtime(self):
        with (self.root/'tasks'/'.worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):create(self.root,self.root/'tasks',self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_corrupt_and_traversal_archives_are_rejected_without_partial_export(self):
        for name,sha in (('../escape','bad'),('data/file','wrong')):
            with zipfile.ZipFile(self.bundle,'w') as archive:
                archive.writestr(name,'content')
                archive.writestr('manifest.json',json.dumps(dict(format=1,files={name:dict(sha256=sha,bytes=7)})))
            destination=self.root.parent/'export'
            with self.assertRaises(ValueError):extract(self.bundle,destination)
            self.assertFalse(destination.exists())


if __name__=='__main__':unittest.main()
