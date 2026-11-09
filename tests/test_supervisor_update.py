import bot_test_support
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from supervisor_update import prepare,apply,rollback


class SupervisorReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.base=Path(tmp.name)
        self.root=self.base/'current';self.root.mkdir();self.source=self.base/'new';self.source.mkdir()
        for name in ('supervisor.py','server.py','http_api.py','process_manager.py','panel_auth.py'):
            (self.root/name).write_text('version = 1\n');(self.source/name).write_text('version = 2\n')
        (self.root/'.env').write_text('secret=preserve')
        self.tasks=self.root/'tasks';self.tasks.mkdir()

    def ready(self):
        with patch('supervisor_update.subprocess.run',return_value=Mock(returncode=0)):
            return prepare(self.root,self.source)

    def test_prepare_apply_and_verified_rollback_leave_secrets(self):
        state=self.ready();self.assertEqual(state['phase'],'ready')
        apply(self.root,self.tasks,state['id'])
        self.assertEqual((self.root/'supervisor.py').read_text(),'version = 2\n')
        rollback(self.root,self.tasks,state['id'])
        self.assertEqual((self.root/'supervisor.py').read_text(),'version = 1\n')
        self.assertEqual((self.root/'.env').read_text(),'secret=preserve')

    def test_live_supervisor_and_hash_changes_block_apply(self):
        state=self.ready();logs=self.root/'logs';logs.mkdir()
        with (logs/'.supervisor.lock').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):apply(self.root,self.tasks,state['id'])
        (self.root/'server.py').write_text('manual = True\n')
        with self.assertRaises(ValueError):apply(self.root,self.tasks,state['id'])
        self.assertEqual((self.root/'supervisor.py').read_text(),'version = 1\n')

    def test_failed_tests_never_produce_ready_release(self):
        with patch('supervisor_update.subprocess.run',return_value=Mock(returncode=1)):
            with self.assertRaises(ValueError):prepare(self.root,self.source)
        self.assertEqual(list((self.root/'.supervisor_releases').glob('*/release.json')),[])


if __name__=='__main__':unittest.main()
