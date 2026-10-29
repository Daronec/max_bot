import bot_test_support  # Load synthetic configuration before application imports.
import unittest
from unittest.mock import MagicMock, patch
import requests
import lm_connection


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        configured = patch.object(lm_connection, 'LM_BASE_URL', 'http://127.0.0.1:1234/v1')
        configured.start()
        self.addCleanup(configured.stop)

    def test_connection_refusal_recovers_and_retries_once(self):
        response=MagicMock()
        with patch.object(lm_connection.requests,'post',side_effect=[requests.ConnectionError('Connection refused'),response]) as post, \
                patch.object(lm_connection,'recover_local_server',return_value=True) as recover:
            result=lm_connection.model_post('http://127.0.0.1:1234/v1/chat/completions',json={'model':'local'})
        self.assertIs(result,response)
        self.assertEqual(post.call_count,2)
        recover.assert_called_once()

    def test_other_connection_errors_do_not_launch_services(self):
        with patch.object(lm_connection.requests,'post',side_effect=requests.ConnectionError('DNS failed')), \
                patch.object(lm_connection,'recover_local_server') as recover:
            with self.assertRaises(requests.ConnectionError):
                lm_connection.model_post('https://remote.invalid/v1/chat/completions')
            recover.assert_not_called()

    def test_remote_or_disabled_targets_are_not_started(self):
        with patch.object(lm_connection,'LM_AUTO_START',True), patch.object(lm_connection.subprocess,'run') as run:
            self.assertFalse(lm_connection.recover_local_server('https://example.com/v1/models'))
            self.assertFalse(lm_connection.recover_local_server('http://127.0.0.1:9999/v1/models'))
            run.assert_not_called()
        with patch.object(lm_connection,'LM_AUTO_START',False):
            self.assertFalse(lm_connection.recover_local_server('http://127.0.0.1:1234/v1/models'))

    def test_running_listener_needs_no_restart(self):
        with patch.object(lm_connection,'LM_AUTO_START',True), \
                patch.object(lm_connection,'_ready',return_value=True), \
                patch.object(lm_connection.subprocess,'run') as run:
            self.assertTrue(lm_connection.recover_local_server('http://127.0.0.1:1234/v1/models'))
            run.assert_not_called()

    def test_startup_is_detached_and_port_is_configured(self):
        with patch.object(lm_connection,'LM_AUTO_START',True), \
                patch.object(lm_connection,'_last_attempt',None), \
                patch.object(lm_connection,'_ready',side_effect=[False,True]), \
                patch.object(lm_connection.Path,'is_file',return_value=True), \
                patch.object(lm_connection.subprocess,'run',return_value=MagicMock(returncode=0)) as run:
            self.assertTrue(lm_connection.recover_local_server('http://127.0.0.1:1234/v1/chat/completions'))
            self.assertIn('1234',run.call_args.args[0])
            self.assertTrue(run.call_args.kwargs['start_new_session'])

    def test_failed_start_does_not_create_restart_loop(self):
        with patch.object(lm_connection,'LM_AUTO_START',True), \
                patch.object(lm_connection,'_ready',return_value=False), \
                patch.object(lm_connection,'_last_attempt',lm_connection.time.monotonic()), \
                patch.object(lm_connection.subprocess,'run') as run:
            with self.assertRaises(lm_connection.ModelServerUnavailable):
                lm_connection.recover_local_server('http://127.0.0.1:1234/v1/models')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
