"""Web UI and task storage shared by all launch scripts.

Incoming MAX messages are processed by LM Studio and replied to in their source chat.
"""
if __name__ == '__main__':
    from runtime import use_project_python
    use_project_python()

from functools import lru_cache
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import signal
import sqlite3
import sys
import time
import uuid
import secrets
import subprocess
import hmac
from urllib.parse import urlsplit
from datetime import datetime, timezone

from task_store import SQLiteTaskStore, TaskConflict
from task_service import TaskService, record_lifecycle
from message_service import execute_message
from execution_context import ExecutionContext, execution_session, ExecutionStopped, check_execution
from model_registry import ModelRegistry, ModelPolicyError
from flask import Flask, jsonify, request, session
from werkzeug.exceptions import HTTPException

from bot_client import MAXBotClient, message_id, message_text
from config import BASE_DIR, BOT_TOKEN, BOT_ID, CHAT_ID, HOST, PORT, WORKER_RETRY_SECONDS, TASKS_PATH
from task_processor import process_message, ProcessingError, comment_on_mood
from mood_checkin import MoodCheckin
from agenda_queries import is_today_reminders_query, today_reminders
from contacts_tools import contact_query, search_contacts
from lm_connection import routing_status
from deepseek_balance import is_balance_query, get_balance
from config import WEB_ENABLED
from config import FS_ENABLED, FS_ALLOWED_CHAT_IDS
from file_tools import capture_file_actions
from config import WORKER_MAX_ATTEMPTS, RUN_TIMEOUT_SECONDS, LOCAL_LM_MODEL, DEEPSEEK_MODEL, DEEPSEEK_ENABLED
from config import RAG_EMBEDDING_MODEL, RAG_EMBEDDING_BASE_URL, RAG_EMBEDDING_LOCAL_DIR
from config import TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_SENDER_IDS
from config import COMMANDS_ENABLED, WORKER_MODE, PANEL_AUTH_ENABLED, PANEL_ACCESS_TOKEN, PANEL_ALLOWED_HOSTS
from command_tools import capture_command_actions
from notification_forwarder import NotificationForwarder
from voice_tools import audio_attachment, transcribe, VoiceError
from assistant_identity import strip_assistant_address
from personal_memory import AGE_QUERY, BIRTH_QUERY, personal_memory, current_memory_turn
from config import VOICE_ENABLED, LM_PROVIDER, LM_MODEL
from morning_summary import MorningSummary, build_summary

TASKS_DIR = str(TASKS_PATH)
UPLOAD_DIR = str(BASE_DIR / 'uploads')
app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=22 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict')
bot_client = MAXBotClient(BOT_TOKEN)
notification_forwarder = NotificationForwarder(bot_client)
morning_summary = MorningSummary(bot_client)
mood_checkin = MoodCheckin(bot_client)
active_tasks = {}
lock = threading.RLock()
monitoring_active = False
monitoring_thread = None
monitoring_error = None
worker_thread = None
worker_manager = None
worker_active = False
worker_error = None
telegram_adapter = None
voice_diagnostics = {'empty_message_events': 0, 'audio_messages_received': 0, 'last_empty_event_at': None}
stop_event = threading.Event()
logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def valid_task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise ValueError('task_id: от 1 до 100 латинских букв, цифр, _ или -')
    return value


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Требуется JSON-объект')
    return data


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Требуется непустой текст')
    return value


@lru_cache(maxsize=16)
def store_for(directory):
    return SQLiteTaskStore(directory)


def task_store():
    return store_for(str(Path(TASKS_DIR).resolve()))


def persist(task):
    record_lifecycle(task)
    valid_task_id(task['task_id'])
    task_store().save(task)
    # Compatibility exports are derived data; SQLite is authoritative.
    try:
        task_store().export(task)
    except OSError:
        logger.warning('Task export failed; committed record remains in SQLite')


def refresh_tasks():
    with lock:
        for saved in task_store().all():
            current = active_tasks.get(saved['task_id'])
            if current is None:
                active_tasks[saved['task_id']] = saved
            elif saved['_revision'] > current.get('_revision', 0):
                current.clear()
                current.update(saved)


def task_service():
    return TaskService(active_tasks, lock, persist, lookup=task_store().get)


def load_tasks(recover=True, recover_submissions=True, active_delivery_pid=None):
    with lock:
        report = task_store().migrate_json()
        if report['rejected']:
            logger.warning('Legacy tasks not imported: %s', ', '.join(report['rejected']))
        refresh_tasks()
        if not recover and not recover_submissions:
            return
        for task in active_tasks.values():
            if (recover and task.get('delivery_inflight')
                    and (active_delivery_pid is None or task.get('delivery_owner_pid') != active_delivery_pid)):
                task.update(delivery_inflight=False, delivery_uncertain=True, delivered=False)
                persist(task)
            if recover_submissions and task.get('status') == 'sending':
                task.update(status='failed', error='Отправка прервана; исход неизвестен. Автоматический повтор остановлен.')
                persist(task)
            elif recover and task.get('status') == 'processing':
                if task.get('file_actions') or task.get('command_actions'):
                    task.update(status='failed', error='Обработка прервана после файловой операции или запуска команды; повторный запуск остановлен')
                else:
                    task['status'] = 'queued'
                persist(task)
                if task.get('kind') == 'project_action' and task.get('change_action') == 'test':
                    try:
                        service = project_service()
                        state = service.load(task['change_id'])
                        if state['phase'] == 'testing':
                            state.update(phase='failed', summary='Проверка прервана; запустите тесты повторно')
                            service.manager.save(state)
                    except (ValueError, PermissionError, OSError):
                        logger.warning('Could not recover interrupted project test state')


def get_task_record(tid):
    tid = valid_task_id(tid)
    if tid not in active_tasks:
        raise KeyError(tid)
    return active_tasks[tid]


@lru_cache(maxsize=1)
def panel_auth():
    from panel_auth import PanelAuth
    auth = PanelAuth(BASE_DIR / '.secrets', PANEL_ACCESS_TOKEN)
    app.secret_key = auth.session_key
    return auth


@app.before_request
def authorize_request():
    if PANEL_AUTH_ENABLED:
        auth = panel_auth()
        if (urlsplit('http://' + request.host).hostname or '').lower() not in PANEL_ALLOWED_HOSTS:
            return jsonify(success=False, error='Недопустимый адрес панели'), 403
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(success=False, error='Недопустимый источник запроса'), 403
        public = request.path in ('/', '/api/login', '/healthz') or request.path.startswith('/static/')
        if not public:
            bearer = request.headers.get('Authorization', '')
            authenticated_token = bearer.startswith('Bearer ') and auth.matches(bearer[7:])
            if not authenticated_token and not session.get('authenticated'):
                return jsonify(success=False, error='Требуется вход в панель'), 401
            if not authenticated_token and request.method not in ('GET', 'HEAD', 'OPTIONS'):
                csrf = request.headers.get('X-CSRF-Token', '')
                if not hmac.compare_digest(session.get('csrf', ''), csrf) or not csrf:
                    return jsonify(success=False, error='Недопустимый CSRF-токен'), 403
    if request.path not in ('/', '/healthz', '/api/login') and not request.path.startswith('/static/'):
        refresh_tasks()


@lru_cache(maxsize=16)
def desktop_memory_for(directory, enabled):
    from desktop_memory import DesktopMemory
    return DesktopMemory(Path(directory) / 'personal-memory.sqlite3', enabled)


def desktop_memory():
    return desktop_memory_for(str(Path(TASKS_DIR).resolve()), personal_memory.enabled)


def import_linked_max_memory(ident=None):
    memory = desktop_memory()
    if not memory.enabled:
        return
    for link in identity_service().links():
        if link['channel'] == 'max' and link['shared_memory'] and (ident is None or ident == link['id']):
            memory.import_max(personal_memory.store, link['chat'], link['sender'])


def assistant_settings():
    from assistant_settings import AssistantSettings
    return AssistantSettings(task_store(), dict(local_model=LOCAL_LM_MODEL, remote_model=DEEPSEEK_MODEL,
        remote_enabled=DEEPSEEK_ENABLED, max_attempts=WORKER_MAX_ATTEMPTS, run_timeout=RUN_TIMEOUT_SECONDS,
        file_tools_enabled=FS_ENABLED, commands_enabled=COMMANDS_ENABLED, web_enabled=WEB_ENABLED))


@lru_cache(maxsize=1)
def project_service():
    from project_service import ProjectService
    from config import CODE_CHANGES_ENABLED
    return ProjectService(BASE_DIR, CODE_CHANGES_ENABLED)


@lru_cache(maxsize=16)
def knowledge_for(directory):
    from knowledge_store import E5Embeddings, KnowledgeStore, LocalEmbeddings
    embeddings = None
    if RAG_EMBEDDING_MODEL:
        embeddings = (E5Embeddings(BASE_DIR / RAG_EMBEDDING_LOCAL_DIR, RAG_EMBEDDING_MODEL)
                      if RAG_EMBEDDING_LOCAL_DIR else LocalEmbeddings(RAG_EMBEDDING_BASE_URL, RAG_EMBEDDING_MODEL))
    return KnowledgeStore(store_for(directory), embeddings)


def knowledge_store():
    return knowledge_for(str(Path(TASKS_DIR).resolve()))


def identity_service():
    from identity_service import IdentityService
    return IdentityService(task_store())


def automation_service():
    from automation_service import AutomationService
    return AutomationService(task_store())


def model_registry():
    return ModelRegistry(task_store())


def knowledge_pipeline():
    from knowledge_pipeline import KnowledgePipeline
    return KnowledgePipeline(knowledge_store())


def project_context():
    from project_context import ProjectContext
    return ProjectContext(task_store())


def background_settings():
    from background_services import BackgroundSettings
    return BackgroundSettings(task_store())


def complete_task(task, text, deliver=False, pending_delivery=False):
    # Save before delivery: a transport failure must not lose the computed result.
    completed = dict(task, events=list(task.get('events', [])), result=text, status='completed', completed_at=now(),
                     delivered=not (deliver or pending_delivery))
    persist(completed)
    task.update(completed)
    if deliver:
        deliver_result(task)


def send_task_reply(task, text, **options):
    if not identity_service().delivery_allowed(task):
        return None
    speech_path = options.pop('speech_path', None)
    if task.get('delivery_channel', task.get('source')) == 'telegram':
        from telegram_adapter import TelegramClient, TelegramError
        try:
            client = TelegramClient(TELEGRAM_BOT_TOKEN)
            reply_id = task.get('bot_message_id')
            response = client.send_audio(task['chat_id'], speech_path, text, reply_id) if speech_path else client.send(task['chat_id'], text, reply_id)
            return {'message': {'id': str(response['message_id'])}} if response.get('message_id') else None
        except TelegramError:
            return None
    if speech_path:
        attachment = bot_client.upload_audio(speech_path)
        if attachment is None:
            return None
        options['attachments'] = [attachment]
    return bot_client.send_message(task['chat_id'], text,
        reply_to_message_id=task.get('reply_to_message_id', task.get('bot_message_id')), **options)


def deliver_result(task):
    # Record successful chunks so a failed later chunk does not resend the beginning.
    with lock:
        if not task.get('_revision'):
            persist(task)
        claimed = task_store().claim_delivery(task['task_id'])
        if claimed is None:
            refresh_tasks()
            return
        task.clear()
        task.update(claimed)
        chunks = [task['result'][i:i + 4000] for i in range(0, len(task['result']), 4000)]
        automatic_voice = bool(task.get('auto_voice_reply') and task.get('speech_file'))
        if automatic_voice:
            chunks.append('Голосовой ответ')
        sent = task.get('sent_chunks', 0)
        for index in range(sent, len(chunks)):
            options = {}
            try:
                if task.get('speech_file') and index == (len(chunks) - 1 if automatic_voice else 0):
                    if task['speech_file'] != task['task_id'] + '.mp3':
                        raise ValueError('Некорректный аудиофайл доставки')
                    options['speech_path'] = str(task_store().directory / 'speech' / task['speech_file'])
                if index == 0 and task.get('result_attachments'):
                    options['attachments'] = task['result_attachments']
                response = send_task_reply(task, chunks[index], **options)
            except Exception:
                task.update(delivered=False, delivery_inflight=False, delivery_uncertain=True)
                persist(task)
                logger.warning('Delivery interrupted; remote receipt is unknown')
                return
            if not message_id((response or {}).get('message', {})):
                # A missing receipt does not prove the remote side rejected the message.
                task.update(delivered=False, delivery_inflight=False, delivery_uncertain=True)
                persist(task)
                return
            task['sent_chunks'] = index + 1
            task.setdefault('delivery_receipts', []).append(str(message_id((response or {}).get('message', {}))))
            persist(task)
        if task.get('background_purpose') == 'mood' and task.get('delivery_receipts'):
            mood_checkin.record_outbox_receipt(task['task_id'], task['delivery_receipts'][0])
        task['delivered'] = True
        task['delivery_inflight'] = False
        task.pop('delivery_retry_at', None)
        persist(task)


def handle_update(update):
    from command_menu import normalize_menu_update
    update = normalize_menu_update(update, bot_client)
    if update is None:
        return
    if update.get('update_type') != 'message_created':
        return
    message = update.get('message') or {}
    if not message:
        voice_diagnostics['empty_message_events'] += 1
        voice_diagnostics['last_empty_event_at'] = now()
        logger.warning('MAX delivered message_created without message; no chat or media available')
        return
    if message.get('sender', {}).get('is_bot'):
        return
    text, mid = message_text(message), message_id(message)
    chat = message.get('recipient', {}).get('chat_id')
    audio = audio_attachment(message) if VOICE_ENABLED and str(chat) in FS_ALLOWED_CHAT_IDS else None
    if (not text.strip() and audio is None) or not mid or chat is None:
        return
    # A configured target limits processing to that chat. Without one, allow discovery.
    if CHAT_ID is not None and str(chat) != str(CHAT_ID):
        return
    with lock:
        refresh_tasks()
        for task in active_tasks.values():
            if (task.get('source') == 'web' and str(task['chat_id']) == str(chat)
                    and bot_client.find_reply([message], task.get('bot_message_id', ''))):
                if task['status'] == 'queued':
                    complete_task(task, text)
                return
        # Deterministic IDs make delivery of the same event idempotent, including restarts.
        tid = 'max_' + hashlib.sha256(f'{chat}:{mid}'.encode()).hexdigest()[:24]
        linked = (message.get('link') or {}).get('message') or {}
        reply_mid = (message.get('link') or {}).get('mid') or message_id(linked) or None
        stamp = message.get('timestamp')
        stamp = stamp / 1000 if isinstance(stamp, (int,float)) else None
        mood_question = mood_checkin.match_answer(chat, mid, reply_mid=reply_mid,
            is_command=text.lstrip().startswith('/') or is_today_reminders_query(text) or contact_query(text) is not None or is_balance_query(text), message_time=stamp)
        if tid not in active_tasks:
            metadata = dict(bot_message_id=mid, chat_type=message.get('recipient', {}).get('chat_type'))
            if '_menu_reply_to' in update:
                metadata['reply_to_message_id'] = update['_menu_reply_to']
            if (message.get('link') or {}).get('type') in {'forward', 'forwarded'} or message.get('forwarded_from'):
                metadata['memory_excluded'] = True
            sender = message.get('sender') or {}
            sender_id = sender.get('user_id') or sender.get('id')
            if sender_id is not None:
                metadata['sender_id'] = str(sender_id)
            if mood_question:
                metadata['mood_question_id'] = mood_question
            if audio is not None:
                voice_diagnostics['audio_messages_received'] += 1
                metadata['audio'] = {'type': 'audio', 'payload': {'url': (audio.get('payload') or {}).get('url')}}
                metadata['message'] = text or '[Голосовое сообщение]'
            task_service().submit(tid, metadata.pop('message', text), chat, 'max', **metadata)


def execute_task_message(text, **permissions):
    return execute_message(
        text, process_message=process_message, is_balance_query=is_balance_query,
        get_balance=get_balance, contact_query=contact_query, search_contacts=search_contacts,
        is_today_reminders_query=is_today_reminders_query, today_reminders=today_reminders,
        build_summary=build_summary, bot_client=bot_client, permissions=permissions)


def execute_with_memory(task, text, execute, permissions):
    query = strip_assistant_address(text)
    shared = task.get('source') == 'desktop' or bool(task.get('memory_link'))
    if shared:
        direct = desktop_memory().command(query, task['task_id'])
        if direct is not None:
            return direct
    if task.get('source') == 'telegram' or task.get('channel_link'):
        if task.get('source') == 'telegram' and not shared:
            from desktop_memory import is_memory_request
            if is_memory_request(query):
                return 'Общая личная память недоступна в Telegram без подтверждённой привязки личного чата. Откройте «Привязка каналов» в панели.'
        if not shared and task.get('source') == 'max' and (BIRTH_QUERY.fullmatch(query) or AGE_QUERY.fullmatch(query)):
            sender = task.get('sender_id') if task.get('source') == 'max' and not task.get('memory_excluded') else None
            with personal_memory.turn(query, task['chat_id'], sender, task['task_id']) as direct:
                return direct
        return execute(text, **permissions)
    from command_menu import handle_menu_command
    menu_result = handle_menu_command(task, strip_assistant_address(text))
    if menu_result is not None:
        return menu_result
    if text.strip().lower() in ('/contacts','/reminders','/summary','/voice','/balance'):
        return execute(text, **permissions)
    from project_changes import handle_project_request
    def project_notice(notice):
        if task.get('code_notice_sent'):
            return
        response = bot_client.send_message(task['chat_id'], notice, reply_to_message_id=task['bot_message_id'])
        if message_id((response or {}).get('message', {})):
            with lock:
                task['code_notice_sent'] = True
                persist(task)
    project_result = handle_project_request(task, strip_assistant_address(text), notify=project_notice)
    if project_result is not None:
        return project_result
    if shared:
        return execute(text, **permissions)
    # Memory only consumes original MAX messages/ASR, never tool output or HTTP tasks.
    sender_id = task.get('sender_id') if task.get('source') == 'max' and not task.get('memory_excluded') else None
    with personal_memory.turn(text, task['chat_id'], sender_id, task['task_id']) as direct:
        if direct is not None:
            return direct
        answer = execute(text, **permissions)
        notice = current_memory_turn().notice
        return answer + ('\n\n' + notice if notice else '')


def execute_run(task, text, execute, permissions):
    if task.get('kind') == 'speech':
        from speech_service import synthesize_task
        try:
            task['speech_file'] = synthesize_task(task, task_store(), BASE_DIR)
            persist(task)
            return 'Озвучен начальный фрагмент ответа (до 600 символов). Полный текст сохранен в диалоге.'
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            return 'Озвучивание не выполнено: ' + str(error)
    identity = identity_service()
    direct = identity.command(task)
    if direct is not None:
        return direct
    try:
        identity.attach(task)
        memory_link = identity.memory_link(task) if personal_memory.enabled else None
        if task.get('memory_link') and task['memory_link'] != memory_link:
            raise PermissionError('Доступ к общей личной памяти отозван.')
        if memory_link or task.get('source') == 'desktop':
            import_linked_max_memory()
        if memory_link and task.get('memory_link') != memory_link:
            task['memory_link'] = memory_link
            persist(task)
    except (PermissionError, ValueError) as error:
        return str(error)
    if task.get('source') in ('max', 'telegram') and task.get('sender_id') and text.strip() == '/speak':
        candidates = [item for item in task_store().all() if item.get('status') == 'completed'
            and item.get('delivery_channel', item.get('source')) == task['source']
            and str(item.get('chat_id')) == str(task['chat_id'])
            and str(item.get('sender_id')) == str(task['sender_id'])
            and item.get('kind') != 'speech' and isinstance(item.get('result'), str)]
        if not candidates or not identity.delivery_allowed(candidates[-1]):
            return 'Нет доступного ответа для озвучивания. Сначала отправьте текстовый запрос.'
        task.update(kind='speech', speech_source=candidates[-1]['task_id'])
        persist(task)
        return execute_run(task, text, execute, permissions)
    if task.get('kind') == 'project_action':
        intent = dict(id=uuid.uuid4().hex, command='project:' + task['change_action'], started=True)
        task.setdefault('command_actions', []).append(intent)
        persist(task)
        try:
            result = project_service().execute(task['change_id'], task['change_action'], task)
        except (ValueError, PermissionError, OSError) as error:
            result = 'Действие с проектом не выполнено: ' + str(error)
        intent['completed'] = True
        persist(task)
        return result
    settings = assistant_settings().get()['values']
    deadline = time.monotonic() + settings['run_timeout']
    history = []
    sources = []
    private_conversation = False
    selected_project = project_context().conversation_project(task['conversation_id']) if task.get('conversation_id') else None
    if task.get('conversation_id'):
        from context_builder import ContextBuilder
        built = ContextBuilder(task_store()).build(task['conversation_id'], task['user_id'])
        history = built['history']
        if built['summary_version'] is not None:
            task['context_summary_version'] = built['summary_version']
            persist(task)
        if task.get('use_knowledge', True):
            sources = knowledge_store().search(project_context().scope(selected_project), text)
    def observed_route(decision):
        with lock:
            task['routing'] = dict(target=decision.target, reason=decision.reason,
                                   label=decision.label, score=decision.score)
            persist(task)
    project_notes = project_context().context(selected_project) if selected_project else ''
    shared_memory = task.get('source') == 'desktop' or bool(task.get('memory_link'))
    owner_context = desktop_memory().context(text) if shared_memory else ''
    personal_context = '\n\n'.join(filter(None, (project_notes, owner_context)))
    if task.get('conversation_id'):
        from personal_memory import PERSONAL, SECRET
        private_input = any(PERSONAL.search(value) or SECRET.search(value) for value in [text, *[item['content'] for item in history]])
        private_conversation = task_store().conversation_privacy(task['conversation_id'], task['user_id'],
            mark_private=bool(permissions or sources or personal_context or private_input))
    knowledge = '\n\n'.join(source['citation'] + ' ' + source['title'] + '\n' + source['content'] for source in sources)
    if sources:
        task['sources'] = [{key:value for key,value in source.items() if key != 'content'} for source in sources]
        persist(task)
    def observed_model(details):
        with lock:
            task.setdefault('model_calls', []).append(dict(details, timestamp=now()))
            persist(task)
    def observed_tool(details):
        with lock:
            events = task.setdefault('tool_executions', [])
            events.append(dict(details, timestamp=now()))
            persist(task)
    def observed_progress(content):
        with lock:
            task['preview'] = content[:24000]
            persist(task)
    context = ExecutionContext(history=history, knowledge=knowledge, personal_context=personal_context, model_target=task.get('model_target', 'auto'),
        local_only=bool(permissions) or private_conversation or bool(personal_context) or bool(sources) or bool(task.get('memory_link')) or task.get('source') == 'telegram' or task.get('source') == 'desktop' and task.get('model_target', 'auto') in ('auto', 'qwen'),
        route_observer=observed_route, model_observer=observed_model, deadline=deadline,
        tool_observer=observed_tool,
        progress_observer=observed_progress,
        remote_enabled=settings['remote_enabled'], model_overrides=settings,
        web_enabled=settings['web_enabled'] and not bool(permissions or private_conversation or personal_context or sources),
        cancel_check=lambda: bool((task_store().runtime('cancel_' + task['run_id']) or {}).get('requested')))
    from config import LOCAL_AGENT_ENABLED
    context.local_agent = LOCAL_AGENT_ENABLED
    registry = model_registry()
    context.budget_reserver = registry.reserve
    def backend_failed(backend):
        registry.unavailable(backend)
        with lock:
            task.setdefault('routing', {}).update(fallback_reason='registered_model_error', fallback_target='qwen')
            persist(task)
    context.backend_failure = backend_failed
    context.backend_selector = lambda label, force_local: registry.route(label,
        private=context.local_only or force_local, remote_enabled=context.remote_enabled, needs_tools=bool(permissions))
    if context.model_target.startswith('model:'):
        backend = registry.backend(context.model_target[6:])
        if backend['local'] or not context.local_only and context.remote_enabled:
            context.selected_backend = backend
            context.budget_reserver = registry.reserve
        else:
            context.local_only = True
    with execution_session(context):
        check_execution()
        answer = execute_with_memory(task, text, execute, permissions)
        check_execution()
    if sources:
        answer += '\n\nИсточники, переданные модели:\n' + '\n'.join(
            source['citation'] + ' ' + source['title'] + ' · строки ' + str(source['line_start']) + '–'
            + str(source['line_end']) + ' · ' + source['url'] for source in sources)
    return answer


def process_next_task():
    """Claim one task under lock; perform inference without blocking MAX polling."""
    global worker_error
    automation_service().tick()
    knowledge_pipeline().tick()
    with lock:
        refresh_tasks()
        pending_delivery = next((t for t in active_tasks.values()
                                 if t['status'] == 'completed' and not t.get('delivered', True)
                                 and not t.get('delivery_uncertain') and not t.get('delivery_inflight')
                                 and t.get('delivery_retry_at', 0) <= time.time()), None)
        from project_changes import deployment_pending
        if pending_delivery is None and deployment_pending():
            return False
        task = next((t for t in active_tasks.values()
                     if t['status'] == 'queued' and t.get('retry_at', 0) <= time.time()), None)
        if pending_delivery is None and task is None:
            return False
        if pending_delivery is None:
            if task.get('attempts', 0) >= assistant_settings().get()['values']['max_attempts']:
                if task.get('error'):
                    task['last_processing_error'] = task['error']
                task.update(status='failed', error='Достигнут лимит попыток обработки')
                persist(task)
                return True
            if not task.get('_revision'):
                persist(task)
            claimed = task_store().claim(task['task_id'], time.time())
            if claimed is None:
                refresh_tasks()
                return False
            task.clear()
            task.update(claimed)
    if pending_delivery is not None:
        deliver_result(pending_delivery)
        return True
    file_actions = []
    command_actions = []
    def record_file_action(action):
        with lock:
            actions = task.setdefault('file_actions', [])
            index = next((i for i, item in enumerate(actions) if action.get('id') and item.get('id') == action['id']), None)
            if index is None:
                actions.append(action)
            else:
                actions[index] = action
            persist(task)
    def record_command_action(action):
        with lock:
            actions = task.setdefault('command_actions', [])
            index = next((i for i, item in enumerate(actions) if item['id'] == action['id']), None)
            if index is None:
                actions.append(action)
            else:
                actions[index] = action
            persist(task)
    try:
        with capture_file_actions(record_file_action) as file_actions, capture_command_actions(record_command_action) as command_actions:
            permissions = {}
            policy = assistant_settings().get()['values']
            if (task.get('source', 'max') in ('max', 'web') and str(task['chat_id']) in FS_ALLOWED_CHAT_IDS
                    or task.get('source') == 'desktop' and task.get('user_id') == 'local_owner' and task.get('allow_tools')):
                if policy['file_tools_enabled']:
                    permissions['allow_files'] = True
                if policy['commands_enabled']:
                    permissions['allow_commands'] = True
            execute = (lambda text, **kw: comment_on_mood(text)) if task.get('mood_question_id') else execute_task_message
            if (task.get('audio') or task.get('telegram_audio') or task.get('audio_upload')) and not task.get('transcription'):
                try:
                    if task.get('audio'):
                        transcript = transcribe(task['audio'])
                    else:
                        from speech_service import transcribe_task
                        transcript = transcribe_task(task, task_store(), TELEGRAM_BOT_TOKEN)
                except VoiceError as error:
                    answer = str(error)
                else:
                    with lock:
                        task['transcription'] = transcript
                        task['message'] = transcript
                        # Drop expiring signed media URLs once speech has been persisted.
                        task.pop('audio', None)
                        task.pop('telegram_audio', None)
                        task.pop('audio_upload', None)
                        persist(task)
                    answer = execute_run(task, transcript, execute, permissions)

            else:
                answer = execute_run(task, task['message'], execute, permissions)
            if task.get('transcription'):
                if task.get('source') == 'telegram' and task.get('kind') != 'speech':
                    try:
                        from speech_service import synthesize_reply
                        task['speech_file'] = synthesize_reply(answer, task['task_id'], task_store(), BASE_DIR)
                        task['auto_voice_reply'] = True
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        logger.warning('Telegram voice synthesis unavailable; delivering text')
                answer = 'Распознано: ' + task['transcription'] + '\n\n' + answer
    except ModelPolicyError as error:
        with lock:
            task.update(status='failed', error=str(error))
            persist(task)
        return True
    except ExecutionStopped as error:
        with lock:
            task.update(status='cancelled' if error.reason == 'cancelled' else 'failed', error=str(error))
            persist(task)
        return True
    except ProcessingError as error:
        worker_error = str(error)
        notice = str(error)
        with lock:
            if file_actions or command_actions:
                task.update(status='failed', error=str(error), file_actions=file_actions, command_actions=command_actions)
                notice = ('Инструменты уже выполняли файловые операции или команды, но завершить ответ не удалось. '
                          'Повторная обработка остановлена, чтобы не дублировать действия.\n' +
                          '\n'.join(action.get('path', action.get('command', ''))[:200] for action in [*file_actions, *command_actions]))
            else:
                task.update(status='queued', error=str(error), retry_at=time.time() + WORKER_RETRY_SECONDS)
            persist(task)
        # Inform the sender once, while preserving the original task for automatic retries.
        if task.get('source') != 'desktop' and not task.get('error_notified'):
            response = send_task_reply(task, notice)
            with lock:
                task['error_notified'] = bool(message_id((response or {}).get('message', {})))
                persist(task)
        return True
    with lock:
        if file_actions:
            task['file_actions'] = file_actions
        if command_actions:
            task['command_actions'] = command_actions
        task.pop('error', None)
        task.pop('retry_at', None)
        complete_task(task, answer, pending_delivery=task.get('source') != 'desktop' or bool(task.get('delivery_channel')))
    worker_error = None
    if task.get('source') != 'desktop' or task.get('delivery_channel'):
        deliver_result(task)
    return True


def task_worker():
    global worker_active, worker_error
    try:
        while not stop_event.is_set():
            try:
                did_work = process_next_task()
            except Exception:
                logger.exception('Ошибка обработчика задач')
                worker_error = 'Ошибка обработки задачи; проверьте журнал сервера'
                # Reset claims after unexpected failures so tasks cannot remain stuck.
                with lock:
                    for task in active_tasks.values():
                        if task['status'] == 'processing':
                            if task.get('file_actions') or task.get('command_actions'):
                                task.update(status='failed', error='Обработка прервана после файловой операции или запуска команды')
                            else:
                                task.update(status='queued', retry_at=time.time() + WORKER_RETRY_SECONDS)
                            persist(task)
                did_work = False
            stop_event.wait(0.1 if did_work else 1)
    finally:
        worker_active = False


def model_routing_status():
    state = routing_status()
    if worker_manager is not None:
        heartbeat = task_store().runtime('worker') or {}
        if heartbeat.get('pid') == worker_manager.status()['pid']:
            state = heartbeat.get('model_routing') or state
    settings = assistant_settings().get()['values']
    return dict(state, local_model=settings['local_model'], remote_model=settings['remote_model'],
                remote_allowed=settings['remote_enabled'])


def worker_status():
    if worker_manager is None:
        return dict(mode=WORKER_MODE, active=worker_active if WORKER_MODE == 'thread' else False,
                    error=worker_error)
    process = worker_manager.status()
    heartbeat = task_store().runtime('worker') or {}
    ready = (process['running'] and heartbeat.get('active', False)
             and heartbeat.get('pid') == process['pid']
             and time.time() - heartbeat.get('heartbeat', 0) < 10)
    return dict(mode='process', active=bool(ready), error=process['error'] or heartbeat.get('error'),
                pid=process['pid'], restarts=process['restarts'],
                heartbeat=heartbeat.get('heartbeat'))


def start_worker():
    global worker_active, worker_thread, worker_manager
    with lock:
        if WORKER_MODE == 'process':
            from process_manager import ProcessManager
            if worker_manager is None:
                worker_manager = ProcessManager(
                    [sys.executable, '-u', str(BASE_DIR / 'worker.py'),
                     '--tasks-dir', str(Path(TASKS_DIR).resolve()), '--parent-pid', str(os.getpid())],
                    BASE_DIR, Path(TASKS_DIR) / 'worker.log')
            worker_manager.start()
            return
        if worker_thread is not None and worker_thread.is_alive():
            return
        stop_event.clear()
        worker_active = True
        worker_thread = threading.Thread(target=task_worker, daemon=True)
        worker_thread.start()


def monitor_bot():
    global monitoring_active, monitoring_error
    try:
        while not stop_event.is_set():
            data = bot_client.get_updates()
            if data is None:
                monitoring_error = bot_client.last_error
                stop_event.wait(2)
                continue
            try:
                for update in data.get('updates', []):
                    handle_update(update)
                # Commit cursor only after all events were saved successfully.
                marker = data.get('marker', bot_client.marker)
                directory = Path(TASKS_DIR)
                directory.mkdir(parents=True, exist_ok=True)
                temporary = directory / '.marker.tmp'
                temporary.write_text(json.dumps(marker), encoding='utf-8')
                os.replace(temporary, directory / '.marker')
                bot_client.marker = marker
                monitoring_error = None
            except (OSError, ValueError, TypeError, KeyError, TaskConflict, sqlite3.Error):
                monitoring_error = 'Не удалось обработать обновление; будет повторная попытка'
                logger.exception('Ошибка обработки обновлений')
            stop_event.wait(0.5)
    finally:
        monitoring_active = False


def start_monitoring():
    global monitoring_active, monitoring_thread
    with lock:
        if monitoring_thread is not None and monitoring_thread.is_alive():
            return
        stop_event.clear()
        monitoring_active = True
        monitoring_thread = threading.Thread(target=monitor_bot, daemon=True)
        monitoring_thread.start()


def telegram_status():
    configured = TELEGRAM_ENABLED and bool(TELEGRAM_BOT_TOKEN) and bool(TELEGRAM_ALLOWED_SENDER_IDS)
    return dict(enabled=TELEGRAM_ENABLED, configured=configured,
                **(telegram_adapter.status() if telegram_adapter is not None else dict(active=False, error=None)))


def start_telegram():
    global telegram_adapter
    if not telegram_status()['configured']:
        return
    from telegram_adapter import TelegramAdapter, TelegramClient
    if telegram_adapter is None:
        telegram_adapter = TelegramAdapter(TelegramClient(TELEGRAM_BOT_TOKEN), task_service,
                                           task_store, TELEGRAM_ALLOWED_SENDER_IDS)
    telegram_adapter.start(stop_event)


def run_server(port=PORT):
    directory = Path(TASKS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.api.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('An API process or restore operation already owns this task store') from None
        if WORKER_MODE == 'thread':
            with (directory / '.worker.lock').open('a') as worker_guard:
                fcntl.flock(worker_guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return serve_api(port)
        return serve_api(port)


def serve_api(port):
    if PANEL_AUTH_ENABLED:
        panel_auth()
    if threading.current_thread() is threading.main_thread():
        def shutdown(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, shutdown)
    load_tasks(recover=WORKER_MODE == 'thread')
    marker = Path(TASKS_DIR) / '.marker'
    if marker.exists():
        try:
            bot_client.marker = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            logger.warning('Не удалось загрузить marker')
    if BOT_TOKEN:
        start_monitoring()
    start_telegram()
    start_worker()
    from background_services import BackgroundCoordinator, OutboxClient
    from config import SUMMARY_ENABLED, MOOD_ENABLED, NOTIFICATIONS_ENABLED, NOTIFICATIONS_POLL_SECONDS
    queued_client = OutboxClient(bot_client, task_store())
    services = []
    for name, service, enabled, interval in (
            ('notifications', notification_forwarder, NOTIFICATIONS_ENABLED, NOTIFICATIONS_POLL_SECONDS),
            ('summary', morning_summary, SUMMARY_ENABLED, 30),
            ('mood', mood_checkin, MOOD_ENABLED, 30)):
        service.client = queued_client
        services.append((name, service, interval))
    coordinator = BackgroundCoordinator(services, background_settings())
    coordinator.start(stop_event)
    try:
        app.run(host=HOST, port=port, debug=False, use_reloader=False)
    finally:
        stop_event.set()
        if worker_manager is not None:
            worker_manager.stop()


# Keep the public server facade compatible while HTTP routes live separately.
from http_api import register_api
globals().update(register_api(sys.modules[__name__]))


# Initialize signing keys before Flask opens the first request session.
if PANEL_AUTH_ENABLED:
    panel_auth()


if __name__ == '__main__':
    run_server()
