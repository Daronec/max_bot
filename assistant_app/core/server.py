"""Web UI and task storage shared by all launch scripts.

Incoming MAX messages are processed by LM Studio and replied to in their source chat.
"""
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
import subprocess
from datetime import datetime, timezone

from assistant_app.core.task_store import SQLiteTaskStore, TaskConflict
from assistant_app.core.task_service import TaskService, record_lifecycle
from assistant_app.core.message_service import execute_message
from assistant_app.core import delivery, task_queue
from assistant_app.models.registry import ModelRegistry
from assistant_app.api.auth import PanelAuth, create_panel_app
from assistant_app.channels.max_client import MAXBotClient, message_id, message_text
from assistant_app.core.message_dispatch import dispatch_message
from assistant_app.core.run_context import run_conversation
from assistant_app.config import BASE_DIR, BOT_TOKEN, BOT_ID, CHAT_ID, HOST, PORT, TASKS_PATH
from assistant_app.core.task_processor import process_message, ProcessingError, comment_on_mood
from assistant_app.services.mood_checkin import MoodCheckin
from assistant_app.tools.agenda_queries import is_today_reminders_query, today_reminders
from assistant_app.tools.contacts_tools import contact_query, search_contacts
from assistant_app.models.gateway import routing_status
from assistant_app.models.deepseek_balance import is_balance_query, get_balance
from assistant_app.config import WEB_ENABLED
from assistant_app.config import FS_ENABLED, FS_ALLOWED_CHAT_IDS
from assistant_app.config import WORKER_MAX_ATTEMPTS, RUN_TIMEOUT_SECONDS, LOCAL_LM_MODEL, DEEPSEEK_MODEL, DEEPSEEK_ENABLED
from assistant_app.config import RAG_EMBEDDING_MODEL, RAG_EMBEDDING_BASE_URL, RAG_EMBEDDING_LOCAL_DIR
from assistant_app.config import TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_SENDER_IDS
from assistant_app.config import COMMANDS_ENABLED, WORKER_MODE, PANEL_AUTH_ENABLED, PANEL_ACCESS_TOKEN, PANEL_ALLOWED_HOSTS
from assistant_app.services.notification_forwarder import NotificationForwarder
from assistant_app.speech.recognition import audio_attachment, transcribe
from assistant_app.memory.personal import personal_memory
from assistant_app.config import VOICE_ENABLED, LM_PROVIDER, LM_MODEL
from assistant_app.services.morning_summary import MorningSummary, build_summary

TASKS_DIR = str(TASKS_PATH)
UPLOAD_DIR = str(BASE_DIR / 'uploads')
app = create_panel_app(__name__)
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
    task_queue.refresh_tasks(task_store(), active_tasks, lock)


def task_service():
    return TaskService(active_tasks, lock, persist, lookup=task_store().get)


def load_tasks(recover=True, recover_submissions=True, active_delivery_pid=None):
    task_queue.load_tasks(
        store=task_store(), tasks=active_tasks, lock=lock, persist=persist,
        project_service=project_service, recover=recover, recover_submissions=recover_submissions,
        active_delivery_pid=active_delivery_pid,
    )


def get_task_record(tid):
    tid = valid_task_id(tid)
    if tid not in active_tasks:
        raise KeyError(tid)
    return active_tasks[tid]


@lru_cache(maxsize=1)
def panel_auth():
    auth = PanelAuth(BASE_DIR / '.secrets', PANEL_ACCESS_TOKEN)
    app.secret_key = auth.session_key
    return auth


@lru_cache(maxsize=16)
def desktop_memory_for(directory, enabled):
    from assistant_app.memory.owner import DesktopMemory
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
    from assistant_app.core.assistant_settings import AssistantSettings
    return AssistantSettings(task_store(), dict(local_model=LOCAL_LM_MODEL, remote_model=DEEPSEEK_MODEL,
        remote_enabled=DEEPSEEK_ENABLED, max_attempts=WORKER_MAX_ATTEMPTS, run_timeout=RUN_TIMEOUT_SECONDS,
        file_tools_enabled=FS_ENABLED, commands_enabled=COMMANDS_ENABLED, web_enabled=WEB_ENABLED))


@lru_cache(maxsize=1)
def project_service():
    from assistant_app.projects.project_service import ProjectService
    from assistant_app.config import CODE_CHANGES_ENABLED
    return ProjectService(BASE_DIR, CODE_CHANGES_ENABLED)


@lru_cache(maxsize=16)
def knowledge_for(directory):
    from assistant_app.knowledge.embeddings import E5Embeddings, LocalEmbeddings
    from assistant_app.knowledge.index import KnowledgeStore
    embeddings = None
    if RAG_EMBEDDING_MODEL:
        embeddings = (E5Embeddings(BASE_DIR / RAG_EMBEDDING_LOCAL_DIR, RAG_EMBEDDING_MODEL)
                      if RAG_EMBEDDING_LOCAL_DIR else LocalEmbeddings(RAG_EMBEDDING_BASE_URL, RAG_EMBEDDING_MODEL))
    return KnowledgeStore(store_for(directory), embeddings)


def knowledge_store():
    return knowledge_for(str(Path(TASKS_DIR).resolve()))


def identity_service():
    from assistant_app.channels.identities import IdentityService
    return IdentityService(task_store())


def automation_service():
    from assistant_app.services.automation_service import AutomationService
    return AutomationService(task_store())


def model_registry():
    return ModelRegistry(task_store())


def knowledge_pipeline():
    from assistant_app.knowledge.pipeline import KnowledgePipeline
    return KnowledgePipeline(knowledge_store())


def project_context():
    from assistant_app.projects.project_context import ProjectContext
    return ProjectContext(task_store())


def background_settings():
    from assistant_app.services.background_services import BackgroundSettings
    return BackgroundSettings(task_store())


def complete_task(task, text, deliver=False, pending_delivery=False):
    task_queue.complete_task(task, text, persist=persist, pending_delivery=deliver or pending_delivery)
    if deliver:
        deliver_result(task)


def send_task_reply(task, text, **options):
    return delivery.send_reply(
        task, text, allowed=identity_service().delivery_allowed,
        max_client=bot_client, telegram_token=TELEGRAM_BOT_TOKEN, **options,
    )


def deliver_result(task):
    delivery.deliver_result(
        task, store=task_store(), lock=lock, persist=persist, refresh_tasks=refresh_tasks,
        send=send_task_reply, record_mood_receipt=mood_checkin.record_outbox_receipt,
    )


def handle_update(update):
    from assistant_app.channels.max_menu import normalize_menu_update
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
    def project_notice(notice):
        if task.get('code_notice_sent'):
            return
        response = bot_client.send_message(task['chat_id'], notice, reply_to_message_id=task['bot_message_id'])
        if message_id((response or {}).get('message', {})):
            with lock:
                task['code_notice_sent'] = True
                persist(task)

    return dispatch_message(
        task, text, execute, permissions, desktop_memory=desktop_memory,
        personal_memory=personal_memory, project_notice=project_notice,
    )


def execute_run(task, text, execute, permissions):
    if task.get('kind') == 'speech':
        from assistant_app.speech.service import synthesize_task
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
    return run_conversation(
        task, text, permissions,
        respond=lambda: execute_with_memory(task, text, execute, permissions),
        store=task_store(), settings=assistant_settings().get()['values'],
        knowledge_search=lambda scope, query: knowledge_store().search(scope, query),
        memory_context=lambda query: desktop_memory().context(query),
        persist=persist, lock=lock,
    )


def set_worker_error(error):
    global worker_error
    worker_error = error


def process_next_task():
    automation_service().tick()
    knowledge_pipeline().tick()
    return task_queue.process_next_task(
        store=task_store(), tasks=active_tasks, lock=lock, persist=persist,
        settings=lambda: assistant_settings().get()['values'], execute_run=execute_run,
        execute_message=execute_task_message, comment_on_mood=comment_on_mood,
        transcribe=transcribe, allowed_tool_chats=FS_ALLOWED_CHAT_IDS,
        deliver=deliver_result, send_reply=send_task_reply, set_error=set_worker_error,
    )


def task_worker():
    global worker_active
    try:
        task_queue.run_worker_loop(
            stop_event=stop_event, run_once=process_next_task, tasks=active_tasks,
            lock=lock, persist=persist, set_error=set_worker_error,
        )
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
            from assistant_app.operations.process_manager import ProcessManager
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
    from assistant_app.channels.telegram import TelegramAdapter, TelegramClient
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
    from assistant_app.services.background_services import BackgroundCoordinator, OutboxClient
    from assistant_app.config import SUMMARY_ENABLED, MOOD_ENABLED, NOTIFICATIONS_ENABLED, NOTIFICATIONS_POLL_SECONDS
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
from assistant_app.api.routes import register_api
globals().update(register_api(sys.modules[__name__]))


# Initialize signing keys before Flask opens the first request session.
if PANEL_AUTH_ENABLED:
    panel_auth()


if __name__ == '__main__':
    run_server()
