import bot_test_support
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import bot_client
import command_menu as menu
import server
from personal_memory import personal_memory
from project_changes import project_changes
from test_bot import msg


class CommandMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for target,attribute,value in [(server,'TASKS_DIR',self.temp.name),(server,'CHAT_ID',None),
                (menu,'FS_ALLOWED_CHAT_IDS',frozenset({'123456789'})),
                (menu,'DEEPSEEK_API_KEY','synthetic-key'),(menu,'SUMMARY_ENABLED',True),
                (personal_memory,'allowed_chats',frozenset({'123456789'})),
                (project_changes,'allowed_chats',frozenset({'123456789'})),
                (project_changes,'allowed_senders',frozenset({'20'}))]:
            self.stack.enter_context(patch.object(target,attribute,value))
        server.active_tasks.clear();self.addCleanup(server.active_tasks.clear)

    def message(self,text='/start',mid='start'):
        data=msg(mid,text,chat=123456789);data['sender']['user_id']=20
        return {'update_type':'message_created','message':data}

    def callback(self,key='memory',ident='callback-1',sender=20):
        return {'update_type':'message_callback','callback':{'callback_id':ident,'payload':'menu:'+key,'user':{'user_id':sender,'is_bot':False}},
                'message':msg('menu-message','menu',chat=123456789,bot=True)}

    def test_start_returns_real_keyboard_without_model_or_memory(self):
        server.handle_update(self.message())
        with patch.object(server.personal_memory,'turn') as memory,patch.object(server,'process_message') as model,patch.object(server.bot_client,'send_message',return_value={'message':msg('reply','menu')}) as send:
            self.assertTrue(server.process_next_task())
        memory.assert_not_called();model.assert_not_called()
        keyboard=send.call_args.kwargs['attachments'][0]
        self.assertEqual(keyboard['type'],'inline_keyboard')
        payloads={b['payload'] for row in keyboard['payload']['buttons'] for b in row}
        self.assertIn('menu:memory',payloads);self.assertIn('menu:code',payloads)
        self.assertEqual(send.call_args.kwargs['reply_to_message_id'],'start')

    def test_menu_filtered_by_actual_user_and_config(self):
        owner={'source':'max','chat_id':'123456789','sender_id':'20'}
        self.assertIn('code',menu.available_commands(owner))
        self.assertNotIn('code',menu.available_commands(dict(owner,sender_id='other')))
        outsider=dict(owner,chat_id='999')
        self.assertEqual(menu.available_commands(outsider),['chat','help'])
        with patch.object(menu,'DEEPSEEK_API_KEY',''):
            self.assertNotIn('balance',menu.available_commands(owner))

    def test_callback_preserves_clicker_and_original_reply_target_and_deduplicates(self):
        with patch.object(server.bot_client,'answer_callback') as answer:
            server.handle_update(self.callback())
            server.handle_update(self.callback())
        self.assertEqual(len(server.active_tasks),1)
        task=next(iter(server.active_tasks.values()))
        self.assertEqual(task['message'],'/memory');self.assertEqual(task['sender_id'],'20')
        self.assertEqual(task['reply_to_message_id'],'menu-message')
        self.assertEqual(task['source'],'max');self.assertTrue(answer.called)
        with patch.object(server.bot_client,'send_message',return_value={'message':msg('out','data')}) as send:
            task.update(status='completed',result='data',delivered=False)
            server.deliver_result(task)
        self.assertEqual(send.call_args.kwargs['reply_to_message_id'],'menu-message')

    def test_callback_cannot_execute_arbitrary_or_unauthorized_command(self):
        with patch.object(server.bot_client,'answer_callback') as answer:
            server.handle_update(self.callback('code',sender='other'))
            server.handle_update(self.callback('/code apply abcdef123456',ident='evil'))
        self.assertFalse(server.active_tasks)
        self.assertIn('недоступна',answer.call_args.args[1])

    def test_bot_started_has_no_reply_to_fake_message(self):
        server.handle_update({'update_type':'bot_started','timestamp':100,'chat_id':123456789,'user':{'user_id':20,'is_bot':False}})
        with patch.object(server.bot_client,'send_message',return_value={'message':msg('out','menu')}) as send:
            server.process_next_task()
        self.assertIsNone(send.call_args.kwargs['reply_to_message_id'])
        self.assertIn('attachments',send.call_args.kwargs)

    def test_delivery_retry_keeps_keyboard_after_reload(self):
        server.handle_update(self.message())
        with patch.object(server.bot_client,'send_message',return_value=None):server.process_next_task()
        task=next(iter(server.active_tasks.values()))
        self.assertFalse(task['delivered']);self.assertIn('result_attachments',task)
        server.active_tasks.clear();server.load_tasks()
        task=next(iter(server.active_tasks.values()))
        with patch.object(server.bot_client,'send_message',return_value={'message':msg('out','menu')}) as send:
            server.deliver_result(task)
        self.assertIn('attachments',send.call_args.kwargs);self.assertTrue(task['delivered'])

    def test_direct_service_commands_bypass_memory_agent(self):
        task={'source':'max','chat_id':123456789,'sender_id':'20','task_id':'service'}
        execute=Mock(return_value='reminders')
        with patch.object(server.personal_memory,'turn') as memory:
            result=server.execute_with_memory(task,'/reminders',execute,{'allow_files':True})
        self.assertEqual(result,'reminders');memory.assert_not_called()

    def test_api_attachment_callback_and_subscription_contracts(self):
        client=bot_client.MAXBotClient('test-token')
        attachments=[{'type':'inline_keyboard','payload':{'buttons':[[{'type':'callback','text':'Menu','payload':'menu:help'}]]}}]
        with patch.object(client,'_request',return_value={}) as request:
            client.send_message(123,'menu',attachments=attachments)
            self.assertEqual(request.call_args.kwargs['data']['attachments'],attachments)
            client.answer_callback('opaque-id','OK')
            self.assertEqual(request.call_args.args,('POST','/answers'))
            self.assertEqual(request.call_args.kwargs['params'],{'callback_id':'opaque-id'})
            client.get_updates()
            self.assertIn('message_callback',request.call_args.kwargs['params']['types'])
            self.assertIn('bot_started',request.call_args.kwargs['params']['types'])
