import bot_test_support  # Load synthetic configuration before application imports.
import json
import unittest
from unittest.mock import Mock,patch
import contacts_tools
import server


class ContactsTests(unittest.TestCase):
    def test_queries_and_mutations(self):
        for text in ('/contacts Иван Иванов','Найди контакт Иван Иванов','Покажи контакт Иван Иванов','Телефон Иван Иванов'):
            self.assertEqual(contacts_tools.contact_query(text),'Иван Иванов')
        self.assertEqual(contacts_tools.contact_query('/contacts'),'')
        self.assertIsNone(contacts_tools.contact_query('Удали контакт Иван'))
        self.assertIsNone(contacts_tools.contact_query('Экспортируй все контакты'))

    def test_results_come_from_helper_and_query_is_argument(self):
        result=Mock(stdout=json.dumps({'contacts':[{'name':'Иван','phones':['123'],'emails':['test@example.com']}],'truncated':False}))
        with patch.object(contacts_tools.subprocess,'run',return_value=result) as run:
            answer=contacts_tools.search_contacts('Иван $(false)')
        self.assertIn('123',answer)
        self.assertEqual(run.call_args.args[0][-1],'Иван $(false)')
        self.assertNotIn('shell',run.call_args.kwargs)

    def test_access_errors_and_empty_queries(self):
        with patch.object(contacts_tools.subprocess,'run') as run:
            self.assertIn('Укажите имя',contacts_tools.search_contacts(''))
            run.assert_not_called()
        with patch.object(contacts_tools.subprocess,'run',return_value=Mock(stdout=json.dumps({'error':'Нет доступа'}))):
            self.assertEqual(contacts_tools.search_contacts('Иван'),'Нет доступа')

    def test_owner_only_and_bypasses_model(self):
        with patch.object(server,'search_contacts',return_value='Телефон: 123') as search,patch.object(server,'process_message') as model:
            self.assertEqual(server.execute_task_message('Найди контакт Иван',allow_commands=True),'Телефон: 123')
            server.execute_task_message('Найди контакт Иван')
            search.assert_called_once_with('Иван');model.assert_not_called()

class ContactCreationTests(unittest.TestCase):
    def test_validation_prevents_save(self):
        import command_tools
        with patch.object(command_tools,'run_command') as run:
            self.assertIn('error',contacts_tools.create_contact(''))
            self.assertIn('error',contacts_tools.create_contact('Иван',email='invalid'))
            run.assert_not_called()

    def test_saved_receipt_and_duplicate(self):
        import command_tools
        with patch.object(command_tools,'run_command',return_value={'exit_code':0,'stdout':json.dumps({'created':True,'id':'real','name':'Иван','phone':'123'})}) as run:
            self.assertEqual(contacts_tools.create_contact('Иван',phone='123')['id'],'real')
            self.assertIn('--create',run.call_args.args[0])
        with patch.object(command_tools,'run_command',return_value={'exit_code':0,'stdout':json.dumps({'duplicate':True})}):
            self.assertTrue(contacts_tools.create_contact('Иван',phone='123')['duplicate'])

    def test_model_creation_receipt_and_permissions(self):
        import task_processor,app_tools
        response=Mock();response.json.return_value={'choices':[{'message':{'content':None,'tool_calls':[{'id':'1','function':{'name':'create_contact','arguments':json.dumps({'given_name':'Иван','phone':'123'})}}]}}]}
        create=Mock(return_value={'created':True,'id':'real','name':'Иван','phone':'123'})
        with patch.object(task_processor,'model_post',return_value=response) as post,patch.dict(app_tools.TOOLS,{'create_contact':create}):
            answer=task_processor.process_message('Создай контакт Иван телефон 123',allow_commands=True)
            self.assertIn('Создан контакт: Иван',answer)
            self.assertEqual(post.call_args.kwargs['json']['tools'][0]['function']['name'],'create_contact')
            create.assert_called_once_with(given_name='Иван',phone='123')
        response.json.return_value={'choices':[{'message':{'content':'Нет доступа'}}]}
        with patch.object(task_processor,'model_post',return_value=response) as post:
            task_processor.process_message('Создай контакт Иван телефон 123')
            self.assertNotIn('create_contact',[t['function']['name'] for t in post.call_args.kwargs['json'].get('tools',[])])

class ContactsBridgeTests(unittest.TestCase):
    def test_launchservices_result_and_cleanup(self):
        import contacts_bridge
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as temp,patch.object(contacts_bridge,'BASE_DIR',Path(temp)):
            def opened(argv,**kwargs):
                request=Path(argv[-1])
                self.assertEqual(json.loads(request.read_text())['args'],['Иван'])
                Path(str(request)+'.result.json').write_text(json.dumps({'contacts':[]}))
            with patch.object(contacts_bridge.subprocess,'run',side_effect=opened) as run:
                self.assertEqual(contacts_bridge.invoke(['Иван']),{'contacts':[]})
                self.assertEqual(run.call_args.args[0][:3],['/usr/bin/open','-W','-n'])
            self.assertFalse(list((Path(temp)/'contacts_requests').glob('*.json')))
