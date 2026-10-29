"""macOS application tools. Scripts are fixed; user text is passed as argv data."""
from datetime import datetime, timedelta
import re
import shlex
import sys

from command_tools import run_command

REMINDER_SCRIPT = '''on run argv
    tell application "Reminders"
        set targetList to default list
        set itemProperties to {name:item 1 of argv, body:item 2 of argv}
        if item 3 of argv is not "" then
            set dueValue to current date
            set day of dueValue to 1
            set year of dueValue to (item 3 of argv as integer)
            set month of dueValue to (item 4 of argv as integer)
            set day of dueValue to (item 5 of argv as integer)
            set hours of dueValue to (item 6 of argv as integer)
            set minutes of dueValue to (item 7 of argv as integer)
            set seconds of dueValue to 0
            set itemProperties to itemProperties & {due date:dueValue}
        end if
        set newItem to make new reminder at end of reminders of targetList with properties itemProperties
        return id of newItem
    end tell
end run'''


def launch_application(application: str) -> dict:
    """Launch an installed macOS app by name, e.g. Reminders, Safari, Calendar. Does not send messages or modify its contents."""
    if sys.platform != 'darwin':
        return {'error': 'Запуск приложений поддерживается только в macOS.'}
    if not isinstance(application, str) or not application.strip() or len(application) > 200 or '\x00' in application or application.startswith('-'):
        return {'error': 'Укажите название установленного приложения.'}
    aliases = {'напоминания': 'Reminders', 'напоминание': 'Reminders', 'календарь': 'Calendar', 'заметки': 'Notes'}
    name = aliases.get(application.strip().lower(), application.strip())
    result = run_command('/usr/bin/open -a ' + shlex.quote(name))
    if result.get('error') or result.get('exit_code') != 0:
        return {'error': result.get('error') or result.get('stderr') or 'Не удалось запустить приложение.'}
    return {'application': name, 'launched': True}


def create_reminder(title: str, notes: str = '', due_at: str = '') -> dict:
    """Create one reminder in the default macOS Reminders list. Preserve the user's exact text. Optional due_at is local Mac time YYYY-MM-DDTHH:MM; omit when no date was requested. Returns the actual reminder ID. macOS may request Automation permission."""
    if sys.platform != 'darwin':
        return {'error': 'Напоминания поддерживаются только в macOS.'}
    if not isinstance(title, str) or not title.strip() or len(title) > 2000 or not isinstance(notes, str) or len(notes) > 10000 or not isinstance(due_at, str):
        return {'error': 'Укажите текст напоминания (до 2000 символов).'}
    try:
        due = None
        if due_at:
            normalized = due_at.strip()
            relative = re.fullmatch(r'(сегодня|завтра|послезавтра)\s+(?:в\s+)?(\d{1,2}):(\d{2})', normalized, re.I)
            if relative:
                days = {'сегодня':0, 'завтра':1, 'послезавтра':2}[relative[1].lower()]
                due = (datetime.now().astimezone() + timedelta(days=days)).replace(hour=int(relative[2]), minute=int(relative[3]), second=0, microsecond=0)
            else:
                if not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', normalized):
                    raise ValueError()
                due = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
                if due.tzinfo is not None:
                    due = due.astimezone()
            due_at = due.strftime('%Y-%m-%dT%H:%M')
        values = [str(due.year), str(due.month), str(due.day), str(due.hour), str(due.minute)] if due else [''] * 5
        argv = [title, notes, *values]
        if any('\x00' in value for value in argv):
            raise ValueError()
    except ValueError:
        return {'error': 'Дата должна быть в формате YYYY-MM-DDTHH:MM (время Mac).'}
    result = run_command(shlex.join(['/usr/bin/osascript', '-e', REMINDER_SCRIPT, '--', *argv]), timeout=120)
    reminder_id = result.get('stdout', '').strip()
    if result.get('error') or result.get('exit_code') != 0 or not reminder_id:
        return {'error': result.get('error') or result.get('stderr') or 'Создание не подтверждено. Проверьте Напоминания и разрешение macOS: Конфиденциальность и безопасность → Автоматизация. Не повторяйте запрос, пока не проверите отсутствие дубликата.'}
    return {'created': True, 'id': reminder_id, 'title': title, 'due_at': due_at}


TOOLS = {'launch_application': launch_application, 'create_reminder': create_reminder}
TOOL_SCHEMAS = [
    {'type': 'function', 'function': {'name': 'launch_application', 'description': launch_application.__doc__,
     'parameters': {'type': 'object', 'properties': {'application': {'type': 'string'}}, 'required': ['application']}}},
    {'type': 'function', 'function': {'name': 'create_reminder', 'description': create_reminder.__doc__,
     'parameters': {'type': 'object', 'properties': {'title': {'type': 'string'}, 'notes': {'type': 'string'}, 'due_at': {'type': 'string', 'description': 'Local Mac date/time, e.g. 2026-10-20T10:00. Use the notification time, not a time mentioned inside the reminder text.'}}, 'required': ['title']}}},
]


def normalize_reminder_arguments(text, arguments):
    result = dict(arguments)
    # In “Добавь на завтра в 10:00 напоминание ... на 19:00” only the prefix is scheduling.
    prefix = re.split(r'напоминани[ея]', text, maxsplit=1, flags=re.I)[0]
    match = re.search(r'\b(послезавтра|завтра|сегодня)\s+в\s+(\d{1,2}):(\d{2})\b', prefix, re.I)
    if match:
        result['due_at'] = f'{match[1].lower()} в {match[2]}:{match[3]}'
    return result


from contacts_tools import create_contact, CREATE_SCHEMA
TOOLS['create_contact'] = create_contact
TOOL_SCHEMAS.append(CREATE_SCHEMA)
