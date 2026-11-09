"""HTTP routes bound to an application runtime; no model or worker is started here."""

import os
import re
import uuid

from flask import jsonify, request, session
from werkzeug.exceptions import HTTPException

from assistant_app.core.task_service import TaskConflict

from .auth import authorize_panel, is_public_panel_path, login_with_token


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('Требуется JSON-объект')
    return data


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Требуется непустой текст')
    return value


def register_api(runtime):
    @runtime.app.before_request
    def authorize_request():
        if runtime.PANEL_AUTH_ENABLED:
            denied = authorize_panel(runtime.panel_auth(), runtime.PANEL_ALLOWED_HOSTS)
            if denied is not None:
                return denied
        if not is_public_panel_path(request.path):
            runtime.refresh_tasks()

    @runtime.app.route('/api/login', methods=['POST'])
    def login():
        if not runtime.PANEL_AUTH_ENABLED:
            return jsonify(success=True)
        return login_with_token(runtime.panel_auth(), body().get('token'))

    @runtime.app.route('/api/session')
    def panel_session():
        return jsonify(success=True, csrf=session.get('csrf'), authentication_enabled=runtime.PANEL_AUTH_ENABLED)

    @runtime.app.route('/api/logout', methods=['POST'])
    def logout():
        session.clear()
        return jsonify(success=True)

    @runtime.app.route('/healthz')
    def health():
        return jsonify(status='ok', bot_id=runtime.BOT_ID, ready=runtime.worker_status()['active'] and (not runtime.BOT_TOKEN or runtime.monitoring_active))

    @runtime.app.errorhandler(TaskConflict)
    def task_conflict(error):
        runtime.refresh_tasks()
        return (jsonify(success=False, error='Задача изменена другим процессом. Обновите состояние.'), 409)

    @runtime.app.errorhandler(PermissionError)
    def permission_denied(error):
        return (jsonify(success=False, error=str(error)), 403)

    @runtime.app.errorhandler(ValueError)
    def invalid_input(error):
        return (jsonify(success=False, error=str(error)), 400)

    @runtime.app.errorhandler(KeyError)
    def missing_task(error):
        return (jsonify(success=False, error='Задача не найдена'), 404)

    @runtime.app.errorhandler(HTTPException)
    def http_error(error):
        return (jsonify(success=False, error=error.description), error.code)

    @runtime.app.route('/')
    def index():
        return runtime.app.send_static_file('index.html')

    @runtime.app.route('/api/status')
    def status():
        from assistant_app.projects.project_changes import project_changes as code_manager
        runtime_worker = runtime.worker_status()
        return jsonify(status='ok', bot_id=runtime.BOT_ID, monitoring_active=runtime.monitoring_active, monitoring_error=runtime.monitoring_error, chat_configured=runtime.CHAT_ID is not None, worker_active=runtime_worker['active'], worker_error=runtime_worker['error'], worker=runtime_worker, telegram=runtime.telegram_status(), code_changes_enabled=code_manager.enabled, code_changes_authorized_chats=len(code_manager.allowed_chats), code_changes_authorized_senders=len(code_manager.allowed_senders), personal_memory_enabled=runtime.personal_memory.enabled, personal_memory_agent_enabled=runtime.personal_memory.use_agent, personal_memory_allowed_chats=len(runtime.personal_memory.allowed_chats), model_routing=runtime.model_routing_status(), model_provider=runtime.LM_PROVIDER, model_name=runtime.LM_MODEL, mood_checkin=runtime.mood_checkin.status(), morning_summary=runtime.morning_summary.status(), voice_diagnostics=dict(runtime.voice_diagnostics), voice_enabled=runtime.VOICE_ENABLED, notifications=runtime.notification_forwarder.status(), web_enabled=runtime.WEB_ENABLED, file_tools_enabled=runtime.FS_ENABLED, file_tools_authorized_chats=len(runtime.FS_ALLOWED_CHAT_IDS), commands_enabled=runtime.COMMANDS_ENABLED)

    @runtime.app.route('/start', methods=['POST'])
    def start_bot():
        data = body()
        chat = runtime.bot_client.validate_chat_id(data.get('chat_id', runtime.CHAT_ID))
        if runtime.bot_client.get_chat_info(chat) is None:
            return (jsonify(success=False, error=runtime.bot_client.last_error), 502)
        runtime.start_monitoring()
        runtime.start_worker()
        return jsonify(success=True, message='Мониторинг запущен')

    @runtime.app.route('/api/task', methods=['POST'])
    @runtime.app.route('/send_task', methods=['POST'])
    def send_task():
        data = body()
        tid = runtime.valid_task_id(data.get('task_id') or f'task_{uuid.uuid4().hex[:12]}')
        text = required_text(data.get('message'))
        chat = runtime.bot_client.validate_chat_id(data.get('chat_id', runtime.CHAT_ID))
        with runtime.lock:
            task, created = runtime.task_service().submit(tid, text, chat, 'web', status='sending')
            if not created:
                return (jsonify(success=False, error='task_id уже существует'), 409)
            mid = runtime.bot_client.send_task(text, f'{tid}.txt', chat_id=chat)
            if mid is None:
                task.update(status='failed', error=runtime.bot_client.last_error or 'MAX не вернул ID сообщения')
                runtime.persist(task)
                return (jsonify(success=False, task_id=tid, error=task['error']), 502)
            task.update(status='queued', bot_message_id=str(mid))
            runtime.persist(task)
        return jsonify(success=True, task_id=tid, run_id=task['run_id'], bot_message_id=str(mid), timestamp=task['timestamp'])

    @runtime.app.route('/api/tasks')
    def list_tasks():
        with runtime.lock:
            return jsonify(success=True, tasks=[dict(t) for t in runtime.active_tasks.values() if t['status'] == 'queued'])

    @runtime.app.route('/api/memory', methods=['GET', 'POST', 'DELETE'])
    def manage_memory():
        runtime.import_linked_max_memory()
        memory = runtime.desktop_memory()
        if request.method == 'GET':
            return jsonify(success=True, enabled=memory.enabled, facts=memory.facts())
        data = body()
        if request.method == 'DELETE':
            memory.delete(required_text(data.get('slot')))
        else:
            memory.save(data.get('category'), data.get('slot'), data.get('evidence'), data.get('updated_at'))
        return jsonify(success=True)

    @runtime.app.route('/api/project/files')
    def project_files():
        return jsonify(success=True, files=runtime.project_service().files(request.args.get('draft')))

    @runtime.app.route('/api/project/file')
    def project_file():
        return jsonify(success=True, file=runtime.project_service().read(required_text(request.args.get('path')), request.args.get('draft')))

    @runtime.app.route('/api/project/drafts', methods=['POST'])
    def create_project_draft():
        return jsonify(success=True, draft=runtime.project_service().create())

    @runtime.app.route('/api/project/drafts/<ident>', methods=['GET', 'POST'])
    def project_draft(ident):
        service = runtime.project_service()
        if request.method == 'GET':
            return jsonify(success=True, draft=service.status(ident))
        data = body()
        return jsonify(success=True, **service.save_file(ident, required_text(data.get('path')), data.get('content'), data.get('sha256')))

    @runtime.app.route('/api/project/drafts/<ident>/action', methods=['POST'])
    def project_action(ident):
        service = runtime.project_service()
        service.load(ident)
        action = body().get('action')
        if action not in ('test', 'apply', 'rollback'):
            raise ValueError('Неизвестное действие')
        tid = 'project_' + uuid.uuid4().hex
        task, _ = runtime.task_service().submit(tid, 'Проект: ' + action + ' ' + ident, 'desktop:local_owner', 'desktop', user_id='local_owner', kind='project_action', change_id=ident, change_action=action, use_knowledge=False)
        return jsonify(success=True, task_id=task['task_id'], run_id=task['run_id'])

    @runtime.app.route('/api/settings', methods=['GET', 'POST'])
    def manage_settings():
        settings = runtime.assistant_settings()
        if request.method == 'GET':
            return jsonify(success=True, **settings.get())
        data = body()
        return jsonify(success=True, **settings.update(data.get('values'), data.get('version')))

    @runtime.app.route('/api/models', methods=['GET', 'POST'])
    def models():
        registry = runtime.model_registry()
        if request.method == 'GET':
            return jsonify(success=True, **registry.get(), availability=registry.status())
        data = body()
        return jsonify(success=True, **registry.update(data.get('models'), data.get('version')))

    @runtime.app.route('/api/tools')
    def tools():
        from assistant_app.tools.tool_registry import public_catalogue
        return jsonify(success=True, tools=public_catalogue())

    @runtime.app.route('/api/logs')
    def logs():
        from assistant_app.operations.diagnostics import log_tail
        return jsonify(success=True, **log_tail(runtime.BASE_DIR, request.args.get('name', 'server.log')))

    @runtime.app.route('/api/background', methods=['GET', 'POST'])
    def background():
        service = runtime.background_settings()
        if request.method == 'GET':
            return jsonify(success=True, **service.get())
        data = body()
        return jsonify(success=True, **service.update(data.get('name'), data.get('enabled'), data.get('version')))

    @runtime.app.route('/api/knowledge', methods=['GET', 'POST'])
    def knowledge_documents():
        store = runtime.knowledge_store()
        scope = runtime.project_context().scope(request.args.get('project_id') if request.method == 'GET' else body().get('project_id'))
        if request.method == 'GET':
            return jsonify(success=True, documents=store.list(scope), hybrid_enabled=bool(store.embeddings))
        data = body()
        result = store.ingest(scope, data.get('title'), data.get('text'), data.get('document_id'))
        return jsonify(success=True, document=result)

    @runtime.app.route('/api/knowledge/<document_id>', methods=['GET', 'DELETE'])
    def knowledge_document(document_id):
        scope = runtime.project_context().scope(request.args.get('project_id'))
        if request.method == 'DELETE':
            runtime.knowledge_store().delete(scope, document_id)
            return jsonify(success=True)
        return jsonify(success=True, document=runtime.knowledge_store().document(scope, document_id))

    @runtime.app.route('/api/knowledge/search', methods=['POST'])
    def knowledge_search():
        data = body()
        return jsonify(success=True, results=runtime.knowledge_store().search(runtime.project_context().scope(data.get('project_id')), required_text(data.get('query'))))

    @runtime.app.route('/api/knowledge/sources', methods=['GET', 'POST'])
    def knowledge_sources():
        pipeline = runtime.knowledge_pipeline()
        scope = runtime.project_context().scope(request.args.get('project_id') if request.method == 'GET' else body().get('project_id'))
        if request.method == 'GET':
            return jsonify(success=True, sources=pipeline.list(scope))
        data = body()
        if data.get('action') == 'add':
            return jsonify(success=True, id=pipeline.add(data.get('path'), scope))
        pipeline.toggle(data.get('id'), data.get('enabled'), scope)
        return jsonify(success=True)

    @runtime.app.route('/api/chat', methods=['POST'])
    def submit_chat():
        data = body()
        text = required_text(data.get('message'))
        if len(text) > 32000:
            raise ValueError('Сообщение превышает 32000 символов')
        if not isinstance(data.get('use_knowledge', True), bool):
            raise ValueError('use_knowledge должен быть логическим значением')
        if not isinstance(data.get('allow_tools', False), bool):
            raise ValueError('allow_tools должен быть логическим значением')
        with runtime.lock:
            task = runtime.task_store().submit_chat(text, 'local_owner', conversation_id=data.get('conversation_id'), allow_tools=data.get('allow_tools', False), model_target=data.get('model_target', 'auto'), use_knowledge=data.get('use_knowledge', True), project_id=data.get('project_id'))
            runtime.active_tasks[task['task_id']] = task
        return jsonify(success=True, task_id=task['task_id'], run_id=task['run_id'], conversation_id=task['conversation_id'])

    @runtime.app.route('/api/chat/audio', methods=['POST'])
    def submit_audio():
        if not runtime.VOICE_ENABLED:
            raise ValueError('Голосовой ввод выключен')
        upload = request.files.get('audio')
        if upload is None:
            raise ValueError('Выберите аудиофайл')
        ident = uuid.uuid4().hex
        folder = runtime.task_store().directory / 'audio'
        folder.mkdir(exist_ok=True, mode=0o700)
        target = folder / ident
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            size = 0
            with os.fdopen(descriptor, 'wb') as output:
                while True:
                    block = upload.stream.read(65536)
                    if not block:
                        break
                    size += len(block)
                    if size > 20 * 1024 * 1024:
                        raise ValueError('Аудио превышает 20 МБ')
                    output.write(block)
            if not size:
                raise ValueError('Пустой аудиофайл')
            task = runtime.task_store().submit_chat('[Голосовое сообщение]', 'local_owner',
                conversation_id=request.form.get('conversation_id') or None,
                model_target='qwen', allow_tools=False, audio_upload=ident, project_id=request.form.get('project_id') or None)
            with runtime.lock:
                runtime.active_tasks[task['task_id']] = task
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return jsonify(success=True, task_id=task['task_id'], conversation_id=task['conversation_id'])

    @runtime.app.route('/api/voices', methods=['GET', 'POST'])
    def voices():
        from assistant_app.speech.clone import list_voices, select_voice, settings
        model, directory = settings()
        if request.method == 'POST':
            select_voice(directory, body().get('preset_id'))
        return jsonify(success=True, **list_voices(model, directory))

    @runtime.app.route('/api/voices/<preset_id>/sample')
    def voice_sample(preset_id):
        from flask import send_file
        from assistant_app.speech.clone import sample_path, settings, voice_presets
        _, directory = settings()
        preset = voice_presets(directory).get(preset_id)
        if not preset:
            return jsonify(success=False, error='Голосовой профиль не найден.'), 404
        response = send_file(sample_path(directory, preset), mimetype='audio/wav')
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @runtime.app.route('/api/task/<task_id>/speech', methods=['GET', 'POST'])
    def task_speech(task_id):
        from flask import send_file
        if request.method == 'GET':
            task = runtime.task_store().get(task_id)
            if not task or not task.get('speech_file') or task.get('source') != 'desktop':
                raise KeyError(task_id)
            # Never trust an arbitrary stored path from a restored task payload.
            filename = task_id + '.mp3'
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id) or task['speech_file'] != filename:
                raise ValueError('Некорректный аудиофайл')
            response = send_file(runtime.task_store().directory / 'speech' / filename, mimetype='audio/mpeg')
            response.headers['Cache-Control'] = 'no-store'
            return response
        source = runtime.task_store().get(task_id)
        if not source or source.get('source') != 'desktop' or source.get('status') != 'completed' or source.get('kind') == 'speech':
            raise ValueError('Выберите завершенный ответ локального чата')
        tid = 'speech_' + uuid.uuid4().hex
        task, _ = runtime.task_service().submit(tid, 'Озвучивание ответа', 'desktop:local_owner', 'desktop',
            user_id='local_owner', kind='speech', speech_source=task_id)
        return jsonify(success=True, task_id=task['task_id'])

    @runtime.app.route('/api/conversations')
    def list_conversations():
        return jsonify(success=True, conversations=runtime.task_store().conversations('local_owner'))

    @runtime.app.route('/api/projects', methods=['GET', 'POST'])
    def projects():
        service = runtime.project_context()
        if request.method == 'GET':
            return jsonify(success=True, projects=service.list())
        data = body()
        return jsonify(success=True, id=service.save(data.get('title'), data.get('notes', ''), data.get('id'), data.get('revision')))

    @runtime.app.route('/api/identities', methods=['GET', 'POST'])
    def identities():
        service = runtime.identity_service()
        if request.method == 'GET':
            return jsonify(success=True, links=service.links())
        data = body()
        if data.get('action') == 'issue':
            return jsonify(success=True, **service.issue(data.get('channel')))
        service.update(data.get('id'), data.get('action'), data.get('conversation_id'))
        if data.get('action') == 'confirm':
            runtime.import_linked_max_memory(data.get('id'))
        return jsonify(success=True)

    @runtime.app.route('/api/automations', methods=['GET', 'POST'])
    def automations():
        service = runtime.automation_service()
        if request.method == 'GET':
            return jsonify(success=True, automations=service.list())
        data = body()
        if data.get('action') == 'create':
            return jsonify(success=True, id=service.create(data.get('config')))
        service.toggle(data.get('id'), data.get('enabled'), data.get('revision'))
        return jsonify(success=True)

    @runtime.app.route('/api/conversations/<conversation_id>', methods=['GET', 'DELETE'])
    def conversation_messages(conversation_id):
        if request.method == 'DELETE':
            from assistant_app.operations.data_admin import delete_conversation
            count = delete_conversation(runtime.task_store(), conversation_id)
            runtime.refresh_tasks()
            return jsonify(success=True, deleted_runs=count)
        return jsonify(success=True, messages=runtime.task_store().messages(conversation_id, 'local_owner'))

    @runtime.app.route('/api/export')
    def export_personal_data():
        from assistant_app.operations.data_admin import export_data
        response = jsonify(export_data(runtime))
        response.headers['Content-Disposition'] = 'attachment; filename="assistant-personal-data.json"'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @runtime.app.route('/api/runs')
    def list_runs():
        with runtime.lock:
            tasks = sorted(runtime.active_tasks.values(), key=lambda task: task.get('timestamp', ''), reverse=True)
            return jsonify(success=True, runs=[{key: task.get(key) for key in ('task_id', 'run_id', 'status', 'source', 'timestamp', 'attempts', 'delivered', 'delivery_uncertain')} for task in tasks[:200]])

    @runtime.app.route('/api/worker', methods=['POST'])
    def manage_worker():
        if runtime.WORKER_MODE != 'process':
            return (jsonify(success=False, error='Управление доступно для отдельного процесса worker'), 409)
        action = body().get('action')
        if action not in ('start', 'stop', 'restart'):
            raise ValueError('Неизвестное действие')
        with runtime.lock:
            if action in ('stop', 'restart') and runtime.worker_manager is not None:
                runtime.worker_manager.stop()
            if action in ('start', 'restart'):
                runtime.start_worker()
        return jsonify(success=True, worker=runtime.worker_status())

    @runtime.app.route('/api/task/<task_id>/cancel', methods=['POST'])
    def cancel_task(task_id):
        with runtime.lock:
            task = runtime.get_task_record(task_id)
            if task['status'] not in ('queued', 'processing'):
                return (jsonify(success=False, error='Задача уже завершена'), 409)
            if task.get('kind') == 'project_action' and task['status'] == 'processing':
                return (jsonify(success=False, error='Запущенная проверка или развертывание завершается отдельно; дождитесь результата.'), 409)
            runtime.task_store().set_runtime('cancel_' + task['run_id'], dict(requested=True))
            if task['status'] == 'queued':
                task['status'] = 'cancelled'
                runtime.persist(task)
        return jsonify(success=True, message='Отмена запрошена. Уже выполненные действия не откатываются.')

    @runtime.app.route('/api/task/<task_id>/delivery', methods=['POST'])
    def resolve_delivery(task_id):
        action = body().get('action')
        if action not in ('retry', 'confirm_delivered'):
            raise ValueError('Выберите retry или confirm_delivered')
        with runtime.lock:
            task = runtime.get_task_record(task_id)
            if not task.get('delivery_uncertain') or task.get('delivery_inflight'):
                return (jsonify(success=False, error='Нет неопределенной доставки для разрешения'), 409)
            task.update(delivery_uncertain=False, delivered=action == 'confirm_delivered')
            task.pop('delivery_retry_at', None)
            runtime.persist(task)
        return jsonify(success=True)

    @runtime.app.route('/api/result', methods=['POST'])
    def publish_result():
        data = body()
        text = required_text(data.get('result'))
        with runtime.lock:
            task = runtime.get_task_record(data.get('task_id'))
            if task['status'] not in ('queued', 'completed'):
                return (jsonify(success=False, error='Задача не ожидает результата'), 409)
            if task['status'] == 'completed' and task['result'] != text:
                return (jsonify(success=False, error='Результат уже сохранён'), 409)
            if task.get('delivery_uncertain'):
                return (jsonify(success=False, error='Исход предыдущей доставки неизвестен; требуется проверка.'), 409)
            if not task.get('delivered'):
                runtime.complete_task(task, text, deliver=True)
            return jsonify(success=True, result=task['result'], delivered=task.get('delivered', False))

    @runtime.app.route('/api/result')
    @runtime.app.route('/api/get_result/<task_id>')
    @runtime.app.route('/get_response', methods=['POST'])
    def get_response(task_id=None):
        data = body() if request.method == 'POST' else request.args
        tid = task_id or data.get('task_id')
        with runtime.lock:
            task = runtime.get_task_record(tid)
            if task['status'] == 'queued' and task.get('source') == 'web':
                messages = runtime.bot_client.get_last_messages(task['chat_id'], limit=100)
                if messages is None:
                    return (jsonify(success=False, error=runtime.bot_client.last_error), 502)
                reply = runtime.bot_client.find_reply(messages.get('messages', []), task['bot_message_id'])
                if reply is not None:
                    runtime.complete_task(task, runtime.message_text(reply))
            return jsonify(success=True, task_id=tid, status=task['status'], result=task.get('result'), error=task.get('error'), preview=task.get('preview') if task['status'] == 'processing' else None, sources=task.get('sources', []))

    @runtime.app.route('/task_status/<task_id>')
    def get_task_status(task_id):
        with runtime.lock:
            task = runtime.get_task_record(task_id)
            return jsonify(success=True, status=task['status'], timestamp=task['timestamp'], run_id=task.get('run_id'), events=task.get('events', []), error=task.get('error'), routing=task.get('routing'), model_calls=task.get('model_calls', []), tool_executions=task.get('tool_executions', []), context_summary_version=task.get('context_summary_version'), project_id=task.get('project_id'), delivery_receipts=task.get('delivery_receipts', []), delivery_uncertain=task.get('delivery_uncertain', False), cancellation_requested=bool((runtime.task_store().runtime('cancel_' + task['run_id']) or {}).get('requested')))

    @runtime.app.route('/get_task')
    def get_task():
        with runtime.lock:
            task = runtime.get_task_record(request.args.get('task_id'))
            return jsonify(success=True, content=task['message'])
    return {'login': login, 'panel_session': panel_session, 'logout': logout, 'health': health, 'task_conflict': task_conflict, 'permission_denied': permission_denied, 'invalid_input': invalid_input, 'missing_task': missing_task, 'http_error': http_error, 'index': index, 'status': status, 'start_bot': start_bot, 'send_task': send_task, 'list_tasks': list_tasks, 'manage_memory': manage_memory, 'project_files': project_files, 'project_file': project_file, 'create_project_draft': create_project_draft, 'project_draft': project_draft, 'project_action': project_action, 'manage_settings': manage_settings, 'knowledge_documents': knowledge_documents, 'knowledge_document': knowledge_document, 'knowledge_search': knowledge_search, 'submit_chat': submit_chat, 'list_conversations': list_conversations, 'conversation_messages': conversation_messages, 'list_runs': list_runs, 'manage_worker': manage_worker, 'cancel_task': cancel_task, 'resolve_delivery': resolve_delivery, 'publish_result': publish_result, 'get_response': get_response, 'get_task_status': get_task_status, 'get_task': get_task}
