import bot_test_support  # Load synthetic configuration before application imports.
import unittest
from unittest.mock import Mock,patch
import requests
import deepseek_balance as balance
import server


class BalanceTests(unittest.TestCase):
    def test_queries(self):
        for text in ('/balance','Покажи баланс','Какой баланс DeepSeek?','баланс','Проверь баланс API'):
            self.assertTrue(balance.is_balance_query(text),text)
        for text in ('Пополни баланс','Что такое баланс белого?','Привет'):
            self.assertFalse(balance.is_balance_query(text),text)

    def test_real_values_and_zero_balance(self):
        data={'is_available':False,'balance_infos':[{'currency':'USD','total_balance':'0.00','granted_balance':'0.00','topped_up_balance':'0.00'}]}
        response=Mock(status_code=200);response.json.return_value=data
        with patch.object(balance,'DEEPSEEK_API_KEY','test'),patch.object(balance.requests,'get',return_value=response) as get:
            answer=balance.get_balance()
            self.assertIn('0.00 USD',answer)
            self.assertIn('недостаточно',answer)
            self.assertEqual(get.call_args.args[0],'https://api.deepseek.com/user/balance')
            self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_transport_errors_do_not_invent_balance(self):
        with patch.object(balance,'DEEPSEEK_API_KEY','test'),patch.object(balance.requests,'get',side_effect=requests.Timeout()):
            self.assertIn('Не удалось',balance.get_balance())

    def test_owner_only_and_no_llm(self):
        with patch.object(server,'get_balance',return_value='Баланс: 1 USD') as read,patch.object(server,'process_message') as model:
            self.assertEqual(server.execute_task_message('Покажи баланс',allow_commands=True),'Баланс: 1 USD')
            server.execute_task_message('/balance')
            read.assert_called_once();model.assert_not_called()
