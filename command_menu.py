"""MAX inline command menu. Callback payloads select only fixed, read-only commands."""
import re
from config import (FS_ALLOWED_CHAT_IDS, FS_ENABLED, COMMANDS_ENABLED, VOICE_ENABLED,
                    SUMMARY_ENABLED, DEEPSEEK_API_KEY)

MENU_COMMANDS = {
    'help': ('❔ Все команды', '/help'),
    'chat': ('💬 Общение', '/chat'),
    'memory': ('🧠 Моя память', '/memory'),
    'remember': ('💾 Как запомнить', '/remember_help'),
    'reminders': ('📋 Напоминания', '/reminders'),
    'summary': ('☀️ Сводка', '/summary'),
    'contacts': ('📇 Контакты', '/contacts'),
    'voice': ('🎤 Голосовые', '/voice'),
    'balance': ('💳 Баланс API', '/balance'),
    'code': ('🛠 Улучшить бота', '/code help'),
}


def available_commands(task):
    from personal_memory import personal_memory
    from project_changes import project_changes
    allowed = ['chat','help']
    chat = str(task.get('chat_id'))
    if (personal_memory.enabled and chat in personal_memory.allowed_chats and task.get('sender_id') is not None
            and not task.get('memory_excluded')):
        allowed += ['memory','remember']
    if chat in FS_ALLOWED_CHAT_IDS:
        if FS_ENABLED or COMMANDS_ENABLED:
            allowed += ['reminders','contacts']
            if SUMMARY_ENABLED:allowed.append('summary')
            if DEEPSEEK_API_KEY:allowed.append('balance')
        if VOICE_ENABLED:allowed.append('voice')
    try:
        project_changes.owner(task)
        allowed.append('code')
    except PermissionError:pass
    return allowed


def menu(task):
    keys = available_commands(task)
    buttons = [{'type':'callback','text':MENU_COMMANDS[key][0],'payload':'menu:'+key} for key in keys]
    rows = [buttons[index:index+2] for index in range(0,len(buttons),2)]
    attachment = {'type':'inline_keyboard','payload':{'buttons':rows}}
    text = ('Я Агафья. Выберите команду кнопкой или напишите мне обычное сообщение.\n'
            'Имя в обращении необязательно. Вернуться к меню: /start.\n\n' +
            '\n'.join(MENU_COMMANDS[key][1]+' — '+MENU_COMMANDS[key][0].split(' ',1)[-1] for key in keys))
    return text,[attachment]


def command_text(text):
    text = text.strip()
    return bool(re.fullmatch(r'/(?:start|help)(?:@[\w-]+)?',text,re.I))


def handle_menu_command(task,text):
    if command_text(text):
        answer,attachments = menu(task)
        task['result_attachments'] = attachments
        return answer
    if text.strip().lower() == '/chat':
        return 'Напишите, что вы хотите обсудить или сделать. Можно отправить текст или голосовое сообщение; обращаться по имени Агафья необязательно.'
    if text.strip().lower() == '/remember_help':
        return ('Расскажите о себе — явные устойчивые факты сохраняются автоматически. '
                'Для адреса и чувствительных сведений попросите явно: «Запомни мой адрес: …».\n'
                'Посмотреть: /memory. Исправить: «Поправь мой адрес в памяти: …». '
                'Удалить сведения: «Забудь …».')
    return None


def normalize_menu_update(update,client):
    kind = update.get('update_type')
    if kind not in ('message_callback','bot_started'):return update
    if kind == 'message_callback':
        callback = update.get('callback') or {}
        original = update.get('message') or {}
        chat = original.get('recipient',{}).get('chat_id')
        user = callback.get('user') or {}
        payload = callback.get('payload') or ''
        ident = callback.get('callback_id')
        if chat is None or not ident or user.get('user_id') is None or user.get('is_bot'):return None
        key = payload[5:] if isinstance(payload,str) and payload.startswith('menu:') else None
        task = {'source':'max','chat_id':chat,'sender_id':str(user['user_id'])}
        if key not in MENU_COMMANDS or key not in available_commands(task):
            client.answer_callback(ident,'Команда недоступна. Откройте /start.')
            return None
        client.answer_callback(ident,'Выполняю')
        text = MENU_COMMANDS[key][1]
        mid = 'menu_callback:'+str(ident)
        reply = original.get('body',{}).get('mid') or original.get('id')
        if not reply:return None
    else:
        chat,user = update.get('chat_id'),update.get('user') or {}
        if chat is None or user.get('user_id') is None or user.get('is_bot'):return None
        mid = 'menu_start:'+str(user['user_id'])+':'+str(update.get('timestamp',0))
        text,reply = '/start',None
    return {'update_type':'message_created','_menu_reply_to':reply,
            'message':{'body':{'mid':mid,'text':text},'recipient':{'chat_id':chat},'sender':user}}
