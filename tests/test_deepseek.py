import bot_test_support  # Load synthetic configuration before application imports.
import unittest
from unittest.mock import Mock,patch
import requests
import task_processor
from intent_router import RouteDecision


class DeepSeekTests(unittest.TestCase):
    def test_non_thinking_parameters_and_auth(self):
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'Ответ'}}]}
        with patch.object(task_processor.intent_router, 'route', return_value=RouteDecision('deepseek', 'data_analysis', .9, 'test')),patch('config.DEEPSEEK_API_KEY', 'test-key'),patch.object(task_processor,'model_post',return_value=response) as post:
            self.assertEqual(task_processor.process_message('Привет'),'Ответ')
            payload=post.call_args.kwargs['json']
            self.assertEqual(payload['thinking'],{'type':'disabled'})
            self.assertNotIn('reasoning_effort',payload)
            self.assertEqual(post.call_args.kwargs['headers']['Authorization'],'Bearer test-key')

    def test_payment_error_is_actionable(self):
        response=Mock(status_code=402)
        error=requests.HTTPError(response=response)
        with patch.object(task_processor,'model_post',side_effect=error):
            with self.assertRaisesRegex(task_processor.ProcessingError,'недостаточно средств'):
                task_processor.process_message('Привет')

    def test_mood_uses_same_provider_options_without_tools(self):
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'Комментарий'}}]}
        with patch.object(task_processor.intent_router, 'route', return_value=RouteDecision('deepseek', 'general_chat', .9, 'test')),patch.object(task_processor,'model_post',return_value=response) as post:
            task_processor.comment_on_mood('Устал')
            self.assertEqual(post.call_args.kwargs['json']['thinking'],{'type':'disabled'})
            self.assertNotIn('tools',post.call_args.kwargs['json'])
