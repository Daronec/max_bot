import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from assistant_app.api.auth import PanelAuth
from assistant_app.api.control import create_app


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.auth = PanelAuth(temporary.name)
        self.manager = Mock()
        self.manager.status.return_value = dict(running=False, pid=None, error=None, restarts=0)
        self.app = create_app(self.manager, self.auth, 'http://127.0.0.1:5001', bot_id='test')
        self.client = self.app.test_client()
        self.headers = {'Authorization':'Bearer ' + self.auth.token}

    def test_control_plane_survives_unreachable_api(self):
        with patch('assistant_app.api.control.requests.request', side_effect=requests.ConnectionError):
            response = self.client.get('/api/status', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.get_json()['server_active'])
            self.assertTrue(response.get_json()['supervisor']['active'])
            self.assertEqual(self.client.get('/api/runs', headers=self.headers).status_code, 503)
        response = self.client.post('/api/server', headers=self.headers, json={'action':'restart'})
        self.assertEqual(response.status_code, 200)
        self.manager.stop.assert_called_once()
        self.manager.start.assert_called_once()

    def test_control_and_proxy_require_auth_and_csrf(self):
        self.assertEqual(self.client.post('/api/server', json={'action':'stop'}).status_code, 401)
        csrf = self.client.post('/api/login', json={'token':self.auth.token}).get_json()['csrf']
        self.assertEqual(self.client.post('/api/server', json={'action':'stop'}).status_code, 403)
        self.assertEqual(self.client.post('/api/server', json={'action':'stop'}, headers={'X-CSRF-Token':csrf}).status_code, 200)

    def test_proxy_preserves_path_and_body_but_not_browser_credentials(self):
        response = Mock(status_code=200, content=b'{"success":true}', headers={'Content-Type':'application/json'})
        with patch('assistant_app.api.control.requests.request', return_value=response) as forward:
            result = self.client.post('/api/chat', json={'message':'test'}, headers=self.headers)
            self.assertEqual(result.status_code, 200)
        args, kwargs = forward.call_args
        self.assertEqual(args, ('POST','http://127.0.0.1:5001/api/chat'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + self.auth.token)
        self.assertNotIn('Cookie', kwargs['headers'])
        self.assertFalse(kwargs['allow_redirects'])

    def test_rebinding_and_remote_proxy_are_rejected(self):
        self.assertEqual(self.client.get('/api/session', headers=self.headers, base_url='http://evil.example').status_code, 403)
        with self.assertRaises(ValueError):
            create_app(self.manager, self.auth, 'https://outside.example')
