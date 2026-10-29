import bot_test_support  # Load synthetic configuration before application imports.
import unittest
from unittest.mock import patch
import agenda_queries
import server


class AgendaQueryTests(unittest.TestCase):
    def test_original_typo_and_common_queries(self):
        for text in ('Какие есть на сегодня напоминая','Какие напоминания на сегодня?','Покажи напоминания','/reminders'):
            self.assertTrue(agenda_queries.is_today_reminders_query(text),text)
        for text in ('Создай напоминание на сегодня','Какие напоминания на завтра','Покажи файл напоминаний','Сегодня грустно'):
            self.assertFalse(agenda_queries.is_today_reminders_query(text),text)

    def test_today_query_routes_to_real_reader_without_model(self):
        with patch.object(server,'today_reminders',return_value='Сегодня: встреча') as read,patch.object(server,'process_message') as model:
            self.assertEqual(server.execute_task_message('Какие есть на сегодня напоминая',allow_commands=True),'Сегодня: встреча')
            read.assert_called_once();model.assert_not_called()

    def test_other_chat_cannot_read_personal_reminders(self):
        with patch.object(server,'today_reminders') as read,patch.object(server,'process_message',return_value='Нет доступа'):
            server.execute_task_message('Какие напоминания на сегодня?')
            read.assert_not_called()

    def test_actual_titles_all_day_and_permission_errors(self):
        data={'reminders':[{'title':'Позвонить','due':'2026-10-22T10:00:00+03:00','all_day':False,'list':'Личное'},{'title':'Купить молоко','due':'2026-10-22T00:00:00+03:00','all_day':True}], 'errors':[]}
        with patch.object(agenda_queries,'read_agenda',return_value=data) as read:
            answer=agenda_queries.today_reminders()
            read.assert_called_once_with('--today-reminders')
            self.assertIn('Позвонить',answer);self.assertIn('Весь день',answer)
        with patch.object(agenda_queries,'read_agenda',return_value={'reminders':[],'errors':['Нет разрешения']}):
            answer=agenda_queries.today_reminders()
            self.assertIn('Нет разрешения',answer)
            self.assertNotIn('напоминаний нет',answer)
