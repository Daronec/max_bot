import bot_test_support
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from assistant_app.operations.process_manager import ProcessManager
from assistant_app.core.task_store import SQLiteTaskStore


class WorkerProcessTests(unittest.TestCase):
    def test_worker_heartbeat_exclusive_lock_restart_and_shutdown(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = SQLiteTaskStore(directory)
            environment = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
            command = [sys.executable, '-u', str(root / 'worker.py'),
                       '--tasks-dir', str(directory), '--parent-pid', str(os.getpid())]
            manager = ProcessManager(command, root, directory / 'worker.log', environment)
            self.addCleanup(manager.stop)
            manager.start()
            def wait_until(predicate, timeout=12):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.05)
                self.fail((directory / 'worker.log').read_text())
            wait_until(lambda: (store.runtime('worker') or {}).get('active'))
            first_pid = manager.status()['pid']
            # A second worker must fail before recovering any running tasks.
            import subprocess
            duplicate = subprocess.run(command, env=environment, capture_output=True, timeout=10)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn(b'already owns', duplicate.stderr)
            manager.process.kill()
            wait_until(lambda: manager.status()['restarts'] == 1)
            wait_until(lambda: (store.runtime('worker') or {}).get('pid') == manager.status()['pid'])
            self.assertNotEqual(first_pid, manager.status()['pid'])
            manager.stop()
            self.assertFalse(manager.status()['running'])
            self.assertFalse(store.runtime('worker')['active'])
