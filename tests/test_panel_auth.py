import bot_test_support
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from panel_auth import PanelAuth
import server


class PanelAuthenticationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.auth = PanelAuth(directory / 'secrets')
        for patcher in (patch.object(server, 'PANEL_AUTH_ENABLED', True),
                        patch.object(server, 'panel_auth', return_value=self.auth),
                        patch.dict(server.app.config, SECRET_KEY=self.auth.session_key),
                        patch.object(server, 'TASKS_DIR', str(directory / 'tasks')),
                        patch.object(server, 'active_tasks', {})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = server.app.test_client()

    def login(self):
        return self.client.post('/api/login', json={'token': self.auth.token}).get_json()['csrf']

    def test_anonymous_cannot_read_or_control_and_health_is_minimal(self):
        self.assertEqual(self.client.get('/api/status').status_code, 401)
        self.assertEqual(self.client.get('/api/voices/original-06b/sample').status_code, 401)
        self.assertEqual(self.client.post('/api/worker', json={'action':'stop'}).status_code, 401)
        health = self.client.get('/healthz').get_json()
        self.assertEqual(set(health), {'status', 'bot_id', 'ready'})
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_session_requires_csrf_and_rejects_cross_origin(self):
        csrf = self.login()
        self.assertEqual(self.client.get('/api/session').status_code, 200)
        self.assertEqual(self.client.post('/api/logout', json={}).status_code, 403)
        headers = {'X-CSRF-Token': csrf, 'Origin':'https://untrusted.example'}
        self.assertEqual(self.client.post('/api/logout', json={}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/logout', json={}, headers={'X-CSRF-Token':csrf}).status_code, 200)
        self.assertEqual(self.client.get('/api/session').status_code, 401)

    def test_bearer_and_host_validation(self):
        headers = {'Authorization': 'Bearer ' + self.auth.token}
        response = self.client.get('/api/session', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.auth.token, response.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/session', headers=headers, base_url='http://evil.example').status_code, 403)

    def test_login_rate_limit_and_secret_permissions(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/login', json={'token':'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'token':self.auth.token}).status_code, 429)
        self.assertEqual((self.auth.directory / 'panel.token').stat().st_mode & 0o777, 0o600)
        self.assertEqual(PanelAuth(self.auth.directory).token, self.auth.token)

    def test_control_only_after_authorization(self):
        csrf = self.login()
        manager = Mock()
        manager.status.return_value = dict(running=False, pid=None, error=None, restarts=0)
        with patch.object(server, 'WORKER_MODE', 'process'), patch.object(server, 'worker_manager', manager):
            response = self.client.post('/api/worker', json={'action':'stop'}, headers={'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code, 200)
        manager.stop.assert_called_once()
