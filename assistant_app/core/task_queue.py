"""Task claiming, execution and recovery for the standalone worker."""
import logging
import subprocess
import time

from assistant_app.channels.max_client import message_id
from assistant_app.tools.command_tools import capture_command_actions
from assistant_app.config import BASE_DIR, TELEGRAM_BOT_TOKEN, WORKER_RETRY_SECONDS
from assistant_app.tools.file_tools import capture_file_actions
from assistant_app.models.registry import ModelPolicyError
from assistant_app.core.task_processor import ProcessingError
from assistant_app.core.task_service import timestamp
from assistant_app.speech.recognition import VoiceError

from .execution_context import ExecutionStopped

logger = logging.getLogger(__name__)


def refresh_tasks(store, tasks, lock):
    with lock:
        for saved in store.all():
            current = tasks.get(saved['task_id'])
            if current is None:
                tasks[saved['task_id']] = saved
            elif saved['_revision'] > current.get('_revision', 0):
                current.clear()
                current.update(saved)


def load_tasks(*, store, tasks, lock, persist, project_service, recover=True,
               recover_submissions=True, active_delivery_pid=None):
    """Load legacy records and recover work left by a stopped process."""
    with lock:
        report = store.migrate_json()
        if report['rejected']:
            logger.warning('Legacy tasks not imported: %s', ', '.join(report['rejected']))
        refresh_tasks(store, tasks, lock)
        if not recover and not recover_submissions:
            return
        for task in tasks.values():
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


def complete_task(task, text, *, persist, pending_delivery=False):
    # Save before delivery: a transport failure must not lose the computed result.
    completed = dict(task, events=list(task.get('events', [])), result=text, status='completed', completed_at=timestamp(),
                     delivered=not pending_delivery)
    persist(completed)
    task.update(completed)


def process_next_task(*, store, tasks, lock, persist, settings, execute_run,
                      execute_message, comment_on_mood, transcribe, allowed_tool_chats,
                      deliver, send_reply, set_error):
    """Claim one task under lock and release it before inference."""
    with lock:
        refresh_tasks(store, tasks, lock)
        pending_delivery = next((t for t in tasks.values()
                                 if t['status'] == 'completed' and not t.get('delivered', True)
                                 and not t.get('delivery_uncertain') and not t.get('delivery_inflight')
                                 and t.get('delivery_retry_at', 0) <= time.time()), None)
        from assistant_app.projects.project_changes import deployment_pending
        if pending_delivery is None and deployment_pending():
            return False
        task = next((t for t in tasks.values()
                     if t['status'] == 'queued' and t.get('retry_at', 0) <= time.time()), None)
        if pending_delivery is None and task is None:
            return False
        if pending_delivery is None:
            if task.get('attempts', 0) >= settings()['max_attempts']:
                if task.get('error'):
                    task['last_processing_error'] = task['error']
                task.update(status='failed', error='Достигнут лимит попыток обработки')
                persist(task)
                return True
            if not task.get('_revision'):
                persist(task)
            claimed = store.claim(task['task_id'], time.time())
            if claimed is None:
                refresh_tasks(store, tasks, lock)
                return False
            task.clear()
            task.update(claimed)
    if pending_delivery is not None:
        deliver(pending_delivery)
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
            policy = settings()
            if (task.get('source', 'max') in ('max', 'web') and str(task['chat_id']) in allowed_tool_chats
                    or task.get('source') == 'desktop' and task.get('user_id') == 'local_owner' and task.get('allow_tools')):
                if policy['file_tools_enabled']:
                    permissions['allow_files'] = True
                if policy['commands_enabled']:
                    permissions['allow_commands'] = True
            execute = (lambda text, **kw: comment_on_mood(text)) if task.get('mood_question_id') else execute_message
            if (task.get('audio') or task.get('telegram_audio') or task.get('audio_upload')) and not task.get('transcription'):
                try:
                    if task.get('audio'):
                        transcript = transcribe(task['audio'])
                    else:
                        from assistant_app.speech.service import transcribe_task
                        transcript = transcribe_task(task, store, TELEGRAM_BOT_TOKEN)
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
                        from assistant_app.speech.service import synthesize_reply
                        task['speech_file'] = synthesize_reply(answer, task['task_id'], store, BASE_DIR)
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
        set_error(str(error))
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
            response = send_reply(task, notice)
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
        needs_delivery = task.get('source') != 'desktop' or bool(task.get('delivery_channel'))
        complete_task(task, answer, persist=persist, pending_delivery=needs_delivery)
    set_error(None)
    if needs_delivery:
        deliver(task)
    return True


def run_worker_loop(*, stop_event, run_once, tasks, lock, persist, set_error):
    """Poll until shutdown, recovering tasks after unexpected worker errors."""
    while not stop_event.is_set():
        try:
            did_work = run_once()
        except Exception:
            logger.exception('Ошибка обработчика задач')
            set_error('Ошибка обработки задачи; проверьте журнал сервера')
            # Reset claims after unexpected failures so tasks cannot remain stuck.
            with lock:
                for task in tasks.values():
                    if task['status'] == 'processing':
                        if task.get('file_actions') or task.get('command_actions'):
                            task.update(status='failed', error='Обработка прервана после файловой операции или запуска команды')
                        else:
                            task.update(status='queued', retry_at=time.time() + WORKER_RETRY_SECONDS)
                        persist(task)
            did_work = False
        stop_event.wait(0.1 if did_work else 1)
