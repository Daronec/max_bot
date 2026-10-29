import bot_test_support  # Load synthetic configuration before application imports.
import json
import shlex
import unittest
from unittest.mock import patch, MagicMock
import app_tools
import task_processor


class AppToolsTests(unittest.TestCase):
    def test_reminder_text_is_data_not_script(self):
        title = 'Купить "молоко"\n$(touch /tmp/bad)'
        with patch.object(app_tools.sys, 'platform', 'darwin'), patch.object(app_tools, 'run_command', return_value={'exit_code':0,'stdout':'reminder-id'}) as run:
            result = app_tools.create_reminder(title, due_at='2026-10-20T15:30')
        argv = shlex.split(run.call_args.args[0])
        self.assertEqual(argv[2], app_tools.REMINDER_SCRIPT)
        self.assertEqual(argv[4], title)
        self.assertEqual(result['id'], 'reminder-id')
        self.assertNotIn(title, app_tools.REMINDER_SCRIPT)

    def test_invalid_date_has_no_side_effects(self):
        with patch.object(app_tools, 'run_command') as run:
            self.assertIn('error', app_tools.create_reminder('test', due_at='2026-02-31T12:00'))
            run.assert_not_called()

    def test_denied_automation_not_success(self):
        with patch.object(app_tools, 'run_command', return_value={'exit_code':1,'stderr':'Not authorized (-1743)' }):
            self.assertIn('error', app_tools.create_reminder('test'))

    def test_alias_and_quoted_application(self):
        with patch.object(app_tools, 'run_command', return_value={'exit_code':0}) as run:
            self.assertTrue(app_tools.launch_application('Напоминания')['launched'])
            self.assertEqual(shlex.split(run.call_args.args[0]), ['/usr/bin/open','-a','Reminders'])

    def completion(self, content=None, name=None):
        response=MagicMock()
        message={'content':content}
        if name:
            message['tool_calls']=[{'id':'1','function':{'name':name,'arguments':json.dumps({'title':'Купить молоко'})}}]
        response.json.return_value={'choices':[{'message':message}]}
        return response

    def test_explicit_reminder_forces_tool_and_returns_receipt(self):
        with patch.object(task_processor, 'model_post', return_value=self.completion(name='create_reminder')) as post, patch.dict(app_tools.TOOLS, {'create_reminder':lambda **kw: {'created':True,'id':'real-id','title':kw['title'],'due_at':''}}):
            answer=task_processor.process_message('Запусти Напоминания и создай напоминание Купить молоко', allow_commands=True)
            self.assertIn('real-id',answer)
            payload=post.call_args.kwargs['json']
            self.assertEqual(payload['tool_choice'],'required')
            self.assertEqual([t['function']['name'] for t in payload['tools']],['create_reminder'])

    def test_other_chats_and_explanations_have_no_app_tools(self):
        for text, allowed in [('Создай напоминание Купить молоко',False),('Объясни как создать напоминание',True)]:
            with patch.object(task_processor, 'model_post', return_value=self.completion('Ответ')) as post:
                task_processor.process_message(text, allow_commands=allowed)
                names=[t['function']['name'] for t in post.call_args.kwargs['json'].get('tools',[])]
                self.assertNotIn('create_reminder', names)
                self.assertNotIn('launch_application', names)

class ReminderDateTests(unittest.TestCase):
    def test_iso_seconds_and_timezone_are_accepted(self):
        with patch.object(app_tools, 'run_command', return_value={'exit_code':0,'stdout':'id'}):
            result=app_tools.create_reminder('test',due_at='2026-10-20T10:00:00')
            self.assertEqual(result['due_at'],'2026-10-20T10:00')
            result=app_tools.create_reminder('test',due_at='2026-10-20T10:00:00+03:00')
            self.assertTrue(result.get('created'))

    def test_user_schedule_overrides_model_confusing_body_time(self):
        text='Добавь на завтра в 10:00 напоминание записать в парикмахерскую на 19:00.'
        args=app_tools.normalize_reminder_arguments(text,{'title':'записать в парикмахерскую на 19:00','due_at':'tomorrow 19:00'})
        self.assertEqual(args['due_at'],'завтра в 10:00')
        with patch.object(app_tools, 'run_command', return_value={'exit_code':0,'stdout':'id'}):
            result=app_tools.create_reminder(**args)
        self.assertTrue(result['due_at'].endswith('T10:00'))
        self.assertIn('19:00',result['title'])
