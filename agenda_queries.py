"""Direct, read-only personal agenda queries, with no model-generated access claims."""
from datetime import datetime
import re
from morning_summary import read_agenda


def is_today_reminders_query(text):
    if text.strip().lower() == '/reminders': return True
    if not re.search(r'напомин',text,re.I): return False
    if re.search(r'созда|добав|запиш|измени|удали|отметь|выполн|файл|завтра|вчера',text,re.I): return False
    return bool(re.search(r'каки|покаж|список|что.*сегодня|есть.*сегодня|сегодня.*есть',text,re.I))


def today_reminders():
    data=read_agenda('--today-reminders')
    if data['errors']:
        return 'Не удалось полностью прочитать Напоминания:\n'+'\n'.join(data['errors'])
    lines=['Незавершённые напоминания на сегодня · '+datetime.now().astimezone().strftime('%d.%m.%Y')]
    if not data['reminders']:
        return lines[0]+'\nНа сегодня напоминаний нет.'
    for item in data['reminders']:
        try:
            due=datetime.fromisoformat(item['due'].replace('Z','+00:00')).astimezone()
            stamp='Весь день' if item.get('all_day') else due.strftime('%H:%M')
            lines.append('• '+stamp+' — '+item['title']+(' ('+item['list']+')' if item.get('list') else ''))
        except (KeyError,ValueError,TypeError):
            lines.append('• Не удалось разобрать одно напоминание.')
    return '\n'.join(lines)
