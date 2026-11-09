from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from assistant_app.api.auth import PanelAuth
from assistant_app.api.secret_vault import SecretVault, vault_values
from assistant_app.api.control import create_app


class SecretVaultTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / '.env').write_text('TELEGRAM_BOT_TOKEN=sample-old-value\n')
        self.auth = PanelAuth(self.root / '.secrets')
        self.vault = SecretVault(self.root, {'UNRELATED_TOKEN': 'outside-project',
                                             'LOCAL_LM_API_KEY': 'external-project-value'})
        self.client = create_app(Mock(), self.auth, 'http://localhost:5001', vault=self.vault).test_client()

    def test_first_login_pin_and_credential_lifecycle(self):
        bearer = {'Authorization': 'Bearer ' + self.auth.token}
        self.assertEqual(self.client.get('/api/vault', headers=bearer).status_code, 401)
        csrf = self.client.post('/api/login', json={'token': self.auth.token}).json['csrf']
        headers = {'X-CSRF-Token': csrf}
        self.assertTrue(self.client.get('/api/session').json['pin_setup_required'])
        self.assertEqual(self.client.get('/api/maintenance').status_code, 403)
        pin = {'pin': '012345', 'confirmation': '012345'}
        self.assertEqual(self.client.post('/api/vault/setup', json=pin).status_code, 403)
        self.assertEqual(self.client.post('/api/vault/setup', json=pin, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/vault/setup', json=pin, headers=headers).status_code, 400)
        self.assertNotIn(b'012345', (self.root / '.secrets/vault.pin').read_bytes())
        self.assertFalse(self.client.get('/api/session').json['pin_setup_required'])
        listing = self.client.get('/api/vault')
        self.assertEqual(listing.headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'sample-old-value', listing.data)
        self.assertNotIn(b'UNRELATED_TOKEN', listing.data)
        reveal = {'action': 'reveal', 'name': 'TELEGRAM_BOT_TOKEN'}
        self.assertEqual(self.client.post('/api/vault', json=reveal, headers=headers).json['value'], 'sample-old-value')
        save = {**reveal, 'action': 'save', 'value': 'sample-new-value'}
        self.assertEqual(self.client.post('/api/vault', json=save, headers=headers).status_code, 200)
        self.assertEqual(vault_values(self.root)['TELEGRAM_BOT_TOKEN'], 'sample-new-value')
        self.assertNotIn(b'sample-new-value', (self.root / '.secrets/project.vault').read_bytes())
        self.assertIn('sample-old-value', (self.root / '.env').read_text())
        self.assertEqual(self.vault.reveal('UNRELATED_TOKEN'), '')
        self.assertEqual(self.vault.reveal('LOCAL_LM_API_KEY'), 'external-project-value')
        with self.assertRaises(ValueError):
            self.vault.save('LOCAL_LM_API_KEY', 'replacement')
        self.client.post('/api/vault/lock', json={}, headers=headers)
        self.assertEqual(self.client.post('/api/vault', json=reveal, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/vault/unlock', json={'pin': '012345'}, headers=headers).status_code, 200)

    def test_pin_attempt_limit(self):
        self.vault.setup('012345', '012345')
        for _ in range(5):
            self.assertFalse(self.vault.unlock('543210'))
        self.assertIsNone(self.vault.unlock('012345'))
