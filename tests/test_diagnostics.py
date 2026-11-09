from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from diagnostics import log_tail


class DiagnosticsTests(unittest.TestCase):
    def test_credentials_redacted_and_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'logs').mkdir()
            (root/'.env').write_text('EXTRA_API_KEY=configured-test-key\n')
            (root/'logs/server.log').write_text('old\n'*40000+'configured-test-key Bearer another-secret https://host/file?token=signed-token&ok=yes\n')
            data=log_tail(root,'server.log')
            self.assertTrue(data['truncated'])
            for secret in ('configured-test-key','another-secret','signed-token'):self.assertNotIn(secret,data['text'])
            self.assertIn('[REDACTED]',data['text'])
            with self.assertRaises(ValueError):log_tail(root,'../.env')


if __name__=='__main__':unittest.main()
