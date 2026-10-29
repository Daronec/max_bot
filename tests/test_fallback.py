import bot_test_support  # Load synthetic configuration before application imports.
import unittest
from unittest.mock import Mock,patch
import requests
import lm_connection as lm
from intent_router import RouteDecision, DeepSeekAvailability


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.token=lm._local_session.set(False)
        self.addCleanup(lm._local_session.reset,self.token)
        self.patches=[patch.object(lm,'LM_PROVIDER','deepseek'),patch.object(lm,'LM_FALLBACK_ENABLED',True),patch.object(lm,'_retry_deepseek_at',0),patch.object(lm,'LOCAL_LM_API_KEY','')]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.url='https://api.deepseek.com/v1/chat/completions'
        self.payload={'model':'deepseek-flash','thinking':{'type':'disabled'},'messages':[{'role':'user','content':'task'},{'role':'assistant','content':None,'tool_calls':[{'id':'call'}]},{'role':'tool','tool_call_id':'call','content':'already done'}]}

    def test_payment_error_preserves_receipts_and_strips_remote_secret(self):
        primary=Mock(status_code=402);local=Mock(status_code=200)
        with patch.object(lm,'_request',side_effect=[primary,local]) as request:
            self.assertIs(lm.model_post(self.url,json=self.payload,headers={'Authorization':'Bearer REMOTE'}),local)
        call=request.call_args
        self.assertEqual(call.args[1],lm.LOCAL_LM_BASE_URL+'/chat/completions')
        self.assertNotIn('Authorization',call.kwargs['headers'])
        self.assertEqual(call.kwargs['json']['messages'],self.payload['messages'])
        self.assertNotIn('thinking',call.kwargs['json'])
        self.assertEqual(call.kwargs['json']['reasoning_effort'],'none')
        self.assertEqual(self.payload['model'],'deepseek-flash')

    def test_connection_timeout_rate_limit_and_server_failure_switch(self):
        for result in (requests.Timeout(),requests.ConnectionError(),Mock(status_code=429),Mock(status_code=503)):
            lm._local_session.set(False)
            with patch.object(lm,'_retry_deepseek_at',0),patch.object(lm,'_request',side_effect=[result,Mock(status_code=200)]) as request:
                lm.model_post(self.url,json=self.payload)
                self.assertEqual(request.call_count,2)

    def test_bad_request_is_not_hidden_by_fallback(self):
        response=Mock(status_code=400)
        with patch.object(lm,'_request',return_value=response) as request:
            self.assertIs(lm.model_post(self.url,json=self.payload),response)
            request.assert_called_once()

    def test_session_sticks_to_local_and_new_session_returns_after_cooldown(self):
        @lm.model_session
        def task():
            lm.model_post(self.url,json=self.payload)
            lm.model_post(self.url,json=self.payload)
        with patch.object(lm,'_request',side_effect=[Mock(status_code=402),Mock(status_code=200),Mock(status_code=200)]) as request:
            task()
            self.assertEqual([c.args[1] for c in request.call_args_list],[self.url,lm.LOCAL_LM_BASE_URL+'/chat/completions',lm.LOCAL_LM_BASE_URL+'/chat/completions'])
        with patch.object(lm,'_retry_deepseek_at',0),patch.object(lm,'_request',return_value=Mock(status_code=200)) as request:
            lm.model_post(self.url,json=self.payload)
            self.assertEqual(request.call_args.args[1],self.url)

    def test_local_failure_propagates_without_replaying_task(self):
        with patch.object(lm,'_request',side_effect=[Mock(status_code=402),requests.ConnectionError('local offline')]) as request:
            with self.assertRaises(requests.ConnectionError):lm.model_post(self.url,json=self.payload)
            self.assertEqual(request.call_count,2)

    def test_failure_after_command_continues_without_second_execution(self):
        import task_processor,command_tools
        first=Mock(status_code=200)
        first.json.return_value={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'cmd','function':{'name':'run_command','arguments':'{"command":"pwd"}'}}]}}]}
        last=Mock(status_code=200)
        last.json.return_value={'choices':[{'message':{'content':'/project'}}]}
        with patch.object(task_processor.intent_router, 'route', return_value=RouteDecision('deepseek', 'web_search', .9, 'test')),patch('intent_router.deepseek_availability', DeepSeekAvailability(True)),patch.object(lm,'_request',side_effect=[first,Mock(status_code=402),last]) as request,patch.object(command_tools,'run_command',return_value={'exit_code':0,'stdout':'/project','stderr':''}) as command:
            self.assertEqual(task_processor.process_message('Выполни команду pwd',allow_commands=True),'/project')
            command.assert_called_once_with(command='pwd')
            history=request.call_args.kwargs['json']['messages']
            self.assertEqual(history[-1]['role'],'tool')
            self.assertIn('/project',history[-1]['content'])

    def test_local_autostart_target_works_with_remote_primary(self):
        with patch.object(lm,'LM_BASE_URL','https://api.deepseek.com/v1'):
            self.assertEqual(lm._local_target(lm.LOCAL_LM_BASE_URL+'/chat/completions').hostname,'127.0.0.1')
            self.assertIsNone(lm._local_target('http://127.0.0.1:9999/v1/models'))
