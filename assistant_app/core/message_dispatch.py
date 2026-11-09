"""Dispatch application commands and scope personal memory before model execution."""
from assistant_app.projects import project_changes
from assistant_app.channels.max_menu import handle_menu_command
from assistant_app.core.assistant_identity import strip_assistant_address
from assistant_app.memory.owner import is_memory_request
from assistant_app.memory.personal import AGE_QUERY, BIRTH_QUERY, current_memory_turn


def dispatch_message(task, text, execute, permissions, *, desktop_memory, personal_memory, project_notice):
    query = strip_assistant_address(text)
    source = task.get('source')
    shared = source == 'desktop' or bool(task.get('memory_link'))
    # Memory only consumes original MAX messages/ASR, never tool output or HTTP tasks.
    sender = task.get('sender_id') if source == 'max' and not task.get('memory_excluded') else None
    if shared:
        direct = desktop_memory().command(query, task['task_id'])
        if direct is not None:
            return direct
    if source == 'telegram' or task.get('channel_link'):
        if source == 'telegram' and not shared and is_memory_request(query):
            return ('Общая личная память недоступна в Telegram без подтверждённой привязки личного чата. '
                    'Откройте «Привязка каналов» в панели.')
        if not shared and source == 'max' and (BIRTH_QUERY.fullmatch(query) or AGE_QUERY.fullmatch(query)):
            with personal_memory.turn(query, task['chat_id'], sender, task['task_id']) as direct:
                return direct
        return execute(text, **permissions)
    menu_result = handle_menu_command(task, query)
    if menu_result is not None:
        return menu_result
    if text.strip().lower() in ('/contacts','/reminders','/summary','/voice','/balance'):
        return execute(text, **permissions)
    project_result = project_changes.handle_project_request(task, query, notify=project_notice)
    if project_result is not None:
        return project_result
    if shared:
        return execute(text, **permissions)
    with personal_memory.turn(text, task['chat_id'], sender, task['task_id']) as direct:
        if direct is not None:
            return direct
        answer = execute(text, **permissions)
        notice = current_memory_turn().notice
        return answer + ('\n\n' + notice if notice else '')
