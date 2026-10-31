"""Web UI and task storage shared by all launch scripts.

Incoming MAX messages are processed by LM Studio and replied to in their source chat.
"""
if __name__ == '__main__':
    from runtime import use_project_python
    use_project_python()

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from bot_client import MAXBotClient, message_id, message_text
from config import BASE_DIR, BOT_TOKEN, BOT_ID, CHAT_ID, HOST, PORT, WORKER_RETRY_SECONDS
from task_processor import process_message, ProcessingError, comment_on_mood
from mood_checkin import MoodCheckin
from agenda_queries import is_today_reminders_query, today_reminders
from contacts_tools import contact_query, search_contacts
from lm_connection import routing_status
from deepseek_balance import is_balance_query, get_balance
from config import WEB_ENABLED
from config import FS_ENABLED, FS_ALLOWED_CHAT_IDS
from file_tools import capture_file_actions
from config import COMMANDS_ENABLED
from command_tools import capture_command_actions
from notification_forwarder import NotificationForwarder
from voice_tools import audio_attachment, transcribe, VoiceError
from assistant_identity import strip_assistant_address, ADDRESS_REPLY
from personal_memory import personal_memory, current_memory_turn
from config import VOICE_ENABLED, LM_PROVIDER, LM_MODEL
from morning_summary import MorningSummary, build_summary

TASKS_DIR = str(BASE_DIR / 'tasks')
UPLOAD_DIR = str(BASE_DIR / 'uploads')
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
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
worker_active = False
worker_error = None
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


def persist(task):
    directory = Path(TASKS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{valid_task_id(task['task_id'])}.json"
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)
    (directory / f"{task['task_id']}.txt").write_text(task['message'], encoding='utf-8')
    if task.get('result') is not None:
        (directory / f"{task['task_id']}_result.md").write_text(task['result'], encoding='utf-8')


def load_tasks():
    with lock:
        for path in Path(TASKS_DIR).glob('*.json'):
            try:
                task = json.loads(path.read_text(encoding='utf-8'))
                tid = valid_task_id(task['task_id'])
                if tid != path.stem or not isinstance(task.get('message'), str):
                    continue
                task.setdefault('source', 'max' if tid.startswith('max_') else 'web')
                if task.get('status') == 'processing':
                    if task.get('file_actions') or task.get('command_actions'):
                        task.update(status='failed', error='Обработка прервана после файловой операции или запуска команды; повторный запуск остановлен')
                    else:
                        task['status'] = 'queued'
                active_tasks[tid] = task
            except (ValueError, KeyError, TypeError, OSError):
                logger.warning('Не удалось прочитать файл задачи %s', path.name)


def get_task_record(tid):
    tid = valid_task_id(tid)
    if tid not in active_tasks:
        raise KeyError(tid)
    return active_tasks[tid]


@app.errorhandler(ValueError)
def invalid_input(error):
    return jsonify(success=False, error=str(error)), 400


@app.errorhandler(KeyError)
def missing_task(error):
    return jsonify(success=False, error='Задача не найдена'), 404


@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(success=False, error=error.description), error.code


@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/api/status')
def status():
    return jsonify(status='ok', bot_id=BOT_ID, monitoring_active=monitoring_active,
                   monitoring_error=monitoring_error, chat_configured=CHAT_ID is not None,
                   worker_active=worker_active, worker_error=worker_error,
                   personal_memory_enabled=personal_memory.enabled, personal_memory_agent_enabled=personal_memory.use_agent, personal_memory_allowed_chats=len(personal_memory.allowed_chats),
                   model_routing=routing_status(), model_provider=LM_PROVIDER, model_name=LM_MODEL, mood_checkin=mood_checkin.status(), morning_summary=morning_summary.status(), voice_diagnostics=dict(voice_diagnostics), voice_enabled=VOICE_ENABLED, notifications=notification_forwarder.status(),
                   web_enabled=WEB_ENABLED, file_tools_enabled=FS_ENABLED,
                   file_tools_authorized_chats=len(FS_ALLOWED_CHAT_IDS), commands_enabled=COMMANDS_ENABLED)


@app.route('/start', methods=['POST'])
def start_bot():
    data = body()
    chat = bot_client.validate_chat_id(data.get('chat_id', CHAT_ID))
    if bot_client.get_chat_info(chat) is None:
        return jsonify(success=False, error=bot_client.last_error), 502
    start_monitoring()
    start_worker()
    return jsonify(success=True, message='Мониторинг запущен')


@app.route('/api/task', methods=['POST'])
@app.route('/send_task', methods=['POST'])
def send_task():
    data = body()
    tid = valid_task_id(data.get('task_id') or f'task_{uuid.uuid4().hex[:12]}')
    text = required_text(data.get('message'))
    chat = bot_client.validate_chat_id(data.get('chat_id', CHAT_ID))
    # Keep submit and polling serialized so a fast reply cannot arrive before registration.
    with lock:
        if tid in active_tasks:
            return jsonify(success=False, error='task_id уже существует'), 409
        task = dict(task_id=tid, message=text, chat_id=chat, status='sending', source='web',
                    timestamp=now(), result=None)
        persist(task)
        active_tasks[tid] = task
        mid = bot_client.send_task(text, f'{tid}.txt', chat_id=chat)
        if mid is None:
            task.update(status='failed', error=bot_client.last_error or 'MAX не вернул ID сообщения')
            persist(task)
            return jsonify(success=False, task_id=tid, error=task['error']), 502
        task.update(status='queued', bot_message_id=str(mid))
        persist(task)
    return jsonify(success=True, task_id=tid, bot_message_id=str(mid), timestamp=task['timestamp'])


@app.route('/api/tasks')
def list_tasks():
    with lock:
        return jsonify(success=True, tasks=[dict(t) for t in active_tasks.values()
                                            if t['status'] == 'queued'])


def complete_task(task, text, deliver=False, pending_delivery=False):
    # Save before delivery: a transport failure must not lose the computed result.
    completed = dict(task, result=text, status='completed', completed_at=now(),
                     delivered=not (deliver or pending_delivery))
    persist(completed)
    task.update(completed)
    if deliver:
        deliver_result(task)


def deliver_result(task):
    # Record successful chunks so a failed later chunk does not resend the beginning.
    with lock:
        chunks = [task['result'][i:i + 4000] for i in range(0, len(task['result']), 4000)]
        sent = task.get('sent_chunks', 0)
        for index in range(sent, len(chunks)):
            response = bot_client.send_message(task['chat_id'], chunks[index],
                                              reply_to_message_id=task['bot_message_id'])
            if not message_id((response or {}).get('message', {})):
                task.update(delivered=False, delivery_retry_at=time.time() + WORKER_RETRY_SECONDS)
                persist(task)
                return
            task['sent_chunks'] = index + 1
            persist(task)
        task['delivered'] = True
        task.pop('delivery_retry_at', None)
        persist(task)


@app.route('/api/result', methods=['POST'])
def publish_result():
    data = body()
    text = required_text(data.get('result'))
    with lock:
        task = get_task_record(data.get('task_id'))
        if task['status'] not in ('queued', 'completed'):
            return jsonify(success=False, error='Задача не ожидает результата'), 409
        if task['status'] == 'completed' and task['result'] != text:
            return jsonify(success=False, error='Результат уже сохранён'), 409
        if not task.get('delivered'):
            complete_task(task, text, deliver=True)
        return jsonify(success=True, result=task['result'], delivered=task.get('delivered', False))


@app.route('/api/result')
@app.route('/api/get_result/<task_id>')
@app.route('/get_response', methods=['POST'])
def get_response(task_id=None):
    data = body() if request.method == 'POST' else request.args
    tid = task_id or data.get('task_id')
    with lock:
        task = get_task_record(tid)
        # Always use the stored message ID and chat, never IDs supplied by another task.
        if task['status'] == 'queued' and task.get('source') != 'max':
            messages = bot_client.get_last_messages(task['chat_id'], limit=100)
            if messages is None:
                return jsonify(success=False, error=bot_client.last_error), 502
            reply = bot_client.find_reply(messages.get('messages', []), task['bot_message_id'])
            if reply is not None:
                complete_task(task, message_text(reply))
        return jsonify(success=True, task_id=tid, status=task['status'], result=task.get('result'))


@app.route('/task_status/<task_id>')
def get_task_status(task_id):
    with lock:
        task = get_task_record(task_id)
        return jsonify(success=True, status=task['status'], timestamp=task['timestamp'])


@app.route('/get_task')
def get_task():
    with lock:
        task = get_task_record(request.args.get('task_id'))
        return jsonify(success=True, content=task['message'])


def handle_update(update):
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
        for task in active_tasks.values():
            if (task.get('source') != 'max' and str(task['chat_id']) == str(chat)
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
            task = dict(task_id=tid, message=text, chat_id=chat, bot_message_id=mid, source='max',
                        status='queued', timestamp=now(), result=None)
            if (message.get('link') or {}).get('type') in {'forward', 'forwarded'} or message.get('forwarded_from'):
                task['memory_excluded'] = True
            sender = message.get('sender') or {}
            sender_id = sender.get('user_id') or sender.get('id')
            if sender_id is not None:
                task['sender_id'] = str(sender_id)
            if mood_question:
                task['mood_question_id'] = mood_question
            if audio is not None:
                voice_diagnostics['audio_messages_received'] += 1
                task['audio'] = {'type': 'audio', 'payload': {'url': (audio.get('payload') or {}).get('url')}}
                task['message'] = text or '[Голосовое сообщение]'
            persist(task)
            active_tasks[tid] = task


def execute_task_message(text, **permissions):
    text = strip_assistant_address(text)
    if not text:
        return ADDRESS_REPLY
    if is_balance_query(text):
        return get_balance() if permissions else 'Баланс API доступен только разрешённому чату.'
    query = contact_query(text)
    if query is not None:
        return search_contacts(query) if permissions else 'Контакты доступны только разрешённому чату.'
    if permissions and is_today_reminders_query(text):
        return today_reminders()
    if text.strip().lower() == '/voice':
        return 'Если голосовое не доходит из MAX, отправьте запись как аудиофайл M4A, MP3, OGG или WAV (до 5 минут, 20 МБ). Нативные голосовые MAX иногда передаёт ботам пустым событием без аудио.'
    if permissions and re.search(r'^/summary$|(?:пришли|покажи|составь|дай).*сводк|утренн[яю][яю] сводк', text, re.I):
        return build_summary(bot_client)
    return process_message(text, **permissions)


def execute_with_memory(task, text, execute, permissions):
    # Memory only consumes original MAX messages/ASR, never tool output or HTTP tasks.
    sender_id = task.get('sender_id') if task.get('source') == 'max' and not task.get('memory_excluded') else None
    with personal_memory.turn(text, task['chat_id'], sender_id, task['task_id']) as direct:
        if direct is not None:
            return direct
        answer = execute(text, **permissions)
        notice = current_memory_turn().notice
        return answer + ('\n\n' + notice if notice else '')


def process_next_task():
    """Claim one task under lock; perform inference without blocking MAX polling."""
    global worker_error
    with lock:
        pending_delivery = next((t for t in active_tasks.values()
                                 if t['status'] == 'completed' and not t.get('delivered', True)
                                 and t.get('delivery_retry_at', 0) <= time.time()), None)
        task = next((t for t in active_tasks.values()
                     if t['status'] == 'queued' and t.get('retry_at', 0) <= time.time()), None)
        if pending_delivery is None and task is None:
            return False
        if pending_delivery is None:
            claimed = dict(task, status='processing')
            persist(claimed)
            task.update(claimed)
    if pending_delivery is not None:
        deliver_result(pending_delivery)
        return True
    file_actions = []
    command_actions = []
    def record_file_action(action):
        with lock:
            task.setdefault('file_actions', []).append(action)
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
            if str(task['chat_id']) in FS_ALLOWED_CHAT_IDS:
                if FS_ENABLED:
                    permissions['allow_files'] = True
                if COMMANDS_ENABLED:
                    permissions['allow_commands'] = True
            execute = (lambda text, **kw: comment_on_mood(text)) if task.get('mood_question_id') else execute_task_message
            if task.get('audio') and not task.get('transcription'):
                try:
                    transcript = transcribe(task['audio'])
                except VoiceError as error:
                    answer = str(error)
                else:
                    with lock:
                        task['transcription'] = transcript
                        task['message'] = transcript
                        # Drop expiring signed media URLs once speech has been persisted.
                        task.pop('audio', None)
                        persist(task)
                    answer = execute_with_memory(task, transcript, execute, permissions)

            else:
                answer = execute_with_memory(task, task['message'], execute, permissions)
            if task.get('transcription'):
                answer = 'Распознано: ' + task['transcription'] + '\n\n' + answer
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
        if not task.get('error_notified'):
            response = bot_client.send_message(task['chat_id'], notice,
                                              reply_to_message_id=task['bot_message_id'])
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
        complete_task(task, answer, pending_delivery=True)
    worker_error = None
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


def start_worker():
    global worker_active, worker_thread
    with lock:
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
            except (OSError, ValueError, TypeError, KeyError):
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


def run_server(port=PORT):
    load_tasks()
    marker = Path(TASKS_DIR) / '.marker'
    if marker.exists():
        try:
            bot_client.marker = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            logger.warning('Не удалось загрузить marker')
    start_monitoring()
    start_worker()
    notification_forwarder.start(stop_event)
    morning_summary.start(stop_event)
    mood_checkin.start(stop_event)
    try:
        app.run(host=HOST, port=port, debug=False, use_reloader=False)
    finally:
        stop_event.set()


if __name__ == '__main__':
    run_server()
