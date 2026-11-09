import bot_test_support
from pathlib import Path
import tempfile
import unittest

from project_service import ProjectService


class PanelProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'run.py').write_text("print('old')\n")
        (self.root / '.env').write_text('MAX_BOT_TOKEN=private-example-token\n')
        self.service = ProjectService(self.root)

    def test_draft_edit_changes_copy_only_and_rejects_stale_version(self):
        draft = self.service.create()
        original = self.service.read('run.py', draft['id'])
        self.service.save_file(draft['id'], 'run.py', "print('new')\n", original['sha256'])
        self.assertEqual((self.root / 'run.py').read_text(), "print('old')\n")
        self.assertIn("print('new')", self.service.status(draft['id'])['diff'])
        with self.assertRaises(ValueError):
            self.service.save_file(draft['id'], 'run.py', 'stale', original['sha256'])

    def test_secret_files_and_private_values_are_rejected(self):
        draft = self.service.create()
        self.assertNotIn('.env', draft['files'])
        for path in ('.env', '../outside.py', 'tasks/private.json'):
            with self.assertRaises(ValueError):
                self.service.save_file(draft['id'], path, 'content', '')
        original = self.service.read('run.py', draft['id'])
        with self.assertRaises(ValueError):
            self.service.save_file(draft['id'], 'run.py', 'private-example-token', original['sha256'])

    def test_application_requires_tests_and_current_fingerprint(self):
        draft = self.service.create()
        with self.assertRaises(ValueError):
            self.service.execute(draft['id'], 'apply', dict(task_id='local'))

    def test_disabled_project_control_is_enforced(self):
        disabled = ProjectService(self.root, enabled=False)
        with self.assertRaises(PermissionError):
            disabled.files()
