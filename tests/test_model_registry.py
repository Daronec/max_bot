import bot_test_support
import tempfile
import unittest
from unittest.mock import Mock, patch

import lm_connection
import server
import task_processor
from execution_context import ExecutionContext, execution_session
from model_registry import ModelRegistry, ModelPolicyError
from task_store import SQLiteTaskStore
from task_service import TaskConflict


class RegistryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = SQLiteTaskStore(tmp.name)
        self.registry = ModelRegistry(self.store)
        self.model = dict(id='extra',model='example',base_url='https://models.example/v1',local=False,
                          context_tokens=32768,output_tokens=1024,input_price=1,output_price=2,daily_budget=.01)
        self.registry.update([self.model], 0)

    def test_validation_secrets_privacy_and_versions(self):
        with self.assertRaises(TaskConflict): self.registry.update([],0)
        for change in (dict(base_url='http://remote/v1'),dict(base_url='https://key:secret@remote/v1'),dict(local=True),dict(context_tokens=True),dict(key_env='actual-secret')):
            with self.assertRaises(ValueError): self.registry.update([{**self.model,**change}],1)
        for private, remote in ((True,True),(False,False)):
            with self.assertRaises(ModelPolicyError): self.registry.backend('extra',private,remote)
        self.registry.update([{**self.model,'key_env':'EXTRA_API_KEY'}],1)
        with patch.dict('os.environ',{'EXTRA_API_KEY':'secret'}):
            self.assertEqual(self.registry.backend('extra')['api_key'],'secret')
            self.assertNotIn('secret',str(self.registry.get()))

    def test_conservative_budget_reserved_before_network_and_context_cap(self):
        backend = self.registry.backend('extra')
        payload = dict(messages=[dict(role='user',content='hello')],max_tokens=10000)
        metrics = self.registry.reserve(backend,payload)
        self.assertEqual(payload['max_tokens'],1024)
        self.assertGreater(metrics['estimated_cost'],0)
        with self.assertRaises(ModelPolicyError): self.registry.reserve(backend,dict(messages=[{'content':'x'*40000}]))
        for _ in range(3): self.registry.reserve(backend,payload)
        with self.assertRaises(ModelPolicyError): self.registry.reserve(backend,payload)

    def test_registered_request_uses_endpoint_and_metrics(self):
        backend = self.registry.backend('extra')
        observed = []
        context = ExecutionContext(model_target='model:extra',selected_backend=backend,budget_reserver=self.registry.reserve,model_observer=observed.append)
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'message':{'content':'hello'}}],'usage':{'total_tokens':20}}
        with execution_session(context), patch.object(lm_connection.requests,'post',return_value=response) as post, patch.object(task_processor,'WEB_ENABLED',False):
            self.assertEqual(task_processor.process_message('Hello'),'hello')
        self.assertEqual(post.call_args.args[0],'https://models.example/v1/chat/completions')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        self.assertEqual(post.call_args.kwargs['json']['model'],'example')
        self.assertIn('estimated_cost',observed[0])

    def test_auto_capabilities_privacy_and_cooldown(self):
        local=dict(self.model,id='local',local=True,base_url='http://127.0.0.1:1234/v1',intents=['default'],tools=False,priority=10)
        remote=dict(self.model,intents=['default'],priority=20)
        self.registry.update([local,remote],1)
        self.assertEqual(self.registry.route(None,private=True)['id'],'local')
        self.assertEqual(self.registry.route(None)['id'],'extra')
        self.assertIsNone(self.registry.route(None,private=True,needs_tools=True))
        self.registry.unavailable(self.registry.backend('extra'))
        self.assertEqual(self.registry.route(None)['id'],'local')

    def test_private_request_cannot_use_registered_remote(self):
        context=ExecutionContext(local_only=True,selected_backend=self.registry.backend('extra'))
        with execution_session(context), patch.object(task_processor,'_process_message',side_effect=lambda *a,**k:lm_connection.current_backend()['base_url']):
            target=task_processor.process_message('hello')
        self.assertNotEqual(target,self.model['base_url'])


if __name__ == '__main__': unittest.main()
