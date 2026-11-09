"""Independent local control plane: authenticated panel and managed assistant API."""
if __name__ == '__main__':
    from runtime import use_project_python
    use_project_python()

import fcntl
import hmac
import logging
import os
from pathlib import Path
import secrets
import signal
import threading
import tempfile
import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, session
import requests

from panel_auth import PanelAuth
from process_manager import ProcessManager


def create_app(manager, auth, api_url, bot_id='', allowed_hosts=('localhost', '127.0.0.1', '::1'), log_root=None, vault=None, maintenance_store=None):
    parsed = urlsplit(api_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Supervisor can only manage a local API')
    app = Flask(__name__)
    app.secret_key = auth.session_key
    app.config.update(MAX_CONTENT_LENGTH=22 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict')

    @app.before_request
    def authorize():
        if (urlsplit('http://' + request.host).hostname or '').lower() not in allowed_hosts:
            return jsonify(success=False, error='Недопустимый адрес панели'), 403
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(success=False, error='Недопустимый источник запроса'), 403
        if request.path in ('/', '/api/login', '/healthz') or request.path.startswith('/static/'):
            return
        bearer = request.headers.get('Authorization', '')
        token_auth = not request.path.startswith('/api/vault') and bearer.startswith('Bearer ') and auth.matches(bearer[7:])
        if not token_auth and not session.get('authenticated'):
            return jsonify(success=False, error='Требуется вход в панель'), 401
        if not token_auth and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            csrf = request.headers.get('X-CSRF-Token', '')
            if not csrf or not hmac.compare_digest(csrf, session.get('csrf', '')):
                return jsonify(success=False, error='Недопустимый CSRF-токен'), 403
        if vault and not token_auth and not vault.configured() and request.path not in ('/api/session','/api/logout','/api/vault/setup'):
            return jsonify(success=False, error='Сначала создайте PIN хранилища.'), 403

    @app.route('/')
    def index():
        return app.send_static_file('index.html')

    @app.route('/api/login', methods=['POST'])
    def login():
        data = request.get_json(silent=True)
        outcome = auth.login(request.remote_addr or '', data.get('token') if isinstance(data, dict) else None)
        if outcome != 'ok':
            return jsonify(success=False, error='Слишком много попыток' if outcome == 'limited' else 'Неверный ключ'), 429 if outcome == 'limited' else 401
        session.clear()
        session.update(authenticated=True, csrf=secrets.token_urlsafe(32))
        return jsonify(success=True, csrf=session['csrf'])

    @app.route('/api/session')
    def current_session():
        return jsonify(success=True, csrf=session.get('csrf'), authentication_enabled=True,
                       pin_setup_required=bool(vault and not vault.configured()))

    @app.route('/api/logout', methods=['POST'])
    def logout():
        session.clear()
        return jsonify(success=True)

    @app.route('/healthz')
    def health():
        return jsonify(status='ok', service='assistant-supervisor', bot_id=bot_id)

    @app.after_request
    def protect_secret_responses(response):
        if request.path.startswith('/api/vault'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/api/vault/setup', methods=['POST'])
    @app.route('/api/vault/unlock', methods=['POST'])
    @app.route('/api/vault/lock', methods=['POST'])
    def vault_session():
        if vault is None:return jsonify(success=False, error='Хранилище недоступно.'), 404
        data = request.get_json(silent=True) or {}
        if not isinstance(data,dict):return jsonify(success=False, error='Некорректный запрос.'), 400
        action = request.path.rsplit('/',1)[1]
        if action == 'lock':
            session.pop('vault_until',None)
            return jsonify(success=True)
        if action == 'setup':
            try:vault.setup(data.get('pin'),data.get('confirmation'))
            except ValueError as error:return jsonify(success=False,error=str(error)),400
        else:
            valid = vault.unlock(data.get('pin'))
            if valid is None:return jsonify(success=False,error='Слишком много попыток. Подождите 5 минут.'),429
            if not valid:return jsonify(success=False,error='Неверный PIN.'),403
        session['vault_until'] = time.time()+300
        return jsonify(success=True)

    @app.route('/api/vault', methods=['GET','POST'])
    def secrets_vault():
        if vault is None:return jsonify(success=False,error='Хранилище недоступно.'),404
        if request.method == 'GET':
            unlocked = session.get('vault_until',0)>time.time()
            return jsonify(success=True,unlocked=unlocked,entries=vault.entries() if unlocked else [],
                           expires_in=max(0,session.get('vault_until',0)-time.time()))
        if session.get('vault_until',0)<=time.time():return jsonify(success=False,error='Разблокируйте хранилище PIN-кодом.'),403
        data = request.get_json(silent=True) or {}
        try:
            if not isinstance(data,dict):raise ValueError('Некорректный запрос.')
            if data.get('action')=='reveal':return jsonify(success=True,value=vault.reveal(data.get('name')))
            if data.get('action')!='save':raise ValueError('Неизвестное действие.')
            vault.save(data.get('name'),data.get('value'))
            return jsonify(success=True)
        except ValueError as error:return jsonify(success=False,error=str(error)),400

    @app.route('/api/logs')
    def logs():
        if log_root is None:
            return jsonify(success=False, error='Журналы не настроены'), 404
        from diagnostics import log_tail
        try:
            return jsonify(success=True, **log_tail(log_root, request.args.get('name', 'supervisor.log')))
        except ValueError as error:
            return jsonify(success=False, error=str(error)), 400

    control_lock = threading.Lock()
    app.extensions['control_lock'] = control_lock

    @app.route('/api/maintenance')
    def maintenance():
        from daily_maintenance import STATE
        report = maintenance_store.runtime(STATE) if maintenance_store else None
        return jsonify(success=True, schedule='04:00 Europe/Moscow', last=report)

    @app.route('/api/server', methods=['POST'])
    def control():
        data = request.get_json(silent=True)
        action = data.get('action') if isinstance(data, dict) else None
        if action not in ('start', 'stop', 'restart'):
            return jsonify(success=False, error='Неизвестное действие'), 400
        with control_lock:
            if action in ('stop', 'restart'):
                manager.stop()
            if action in ('start', 'restart'):
                manager.start()
        return jsonify(success=True, server=manager.status())

    def forward(path):
        # Never forward browser cookies, credentials or arbitrary destination headers.
        response = requests.request(request.method, api_url.rstrip('/') + '/' + path,
            params=request.args, data=request.get_data(),
            headers={'Authorization':'Bearer ' + auth.token,
                     'Content-Type':request.headers.get('Content-Type', 'application/json')},
            timeout=(3, 120), allow_redirects=False)
        return response

    @app.route('/api/status')
    def status():
        process = manager.status()
        try:
            response = forward('api/status')
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get('bot_id') != bot_id:
                raise ValueError('Unexpected local API')
            active = True
        except (requests.RequestException, ValueError):
            active = False
            data = dict(status='ok', bot_id=bot_id, worker_active=False, worker_error=None,
                        worker=dict(mode='process'), monitoring_active=False, model_routing={},
                        telegram=dict(active=False, configured=False))
        data.update(supervisor=dict(active=True, server=process), server_active=active)
        return jsonify(data)

    @app.route('/<path:path>', methods=['GET', 'POST', 'DELETE'])
    def proxy(path):
        if not (path.startswith('api/') or path.startswith('task_status/')
                or path in ('start', 'get_task', 'get_response', 'send_task')):
            return jsonify(success=False, error='Маршрут не найден'), 404
        try:
            response = forward(path)
            result = Response(response.content, status=response.status_code,
                              content_type=response.headers.get('Content-Type', 'application/json'))
            if 'Cache-Control' in response.headers:
                result.headers['Cache-Control'] = response.headers['Cache-Control']
            return result
        except requests.RequestException:
            return jsonify(success=False, error='Сервер ассистента недоступен. Запустите его в разделе инфраструктуры.'), 503

    return app


def main():
    external = dict(os.environ)
    from config import BASE_DIR, PORT, BOT_ID, PANEL_ACCESS_TOKEN, PANEL_ALLOWED_HOSTS, SUPERVISOR_PORT, TASKS_PATH, LOCAL_LM_BASE_URL
    from daily_maintenance import run as run_maintenance
    from lm_connection import _local_target, _ready, recover_local_server
    from model_registry import ModelRegistry
    from secret_vault import SecretVault
    from task_store import SQLiteTaskStore
    logs = BASE_DIR / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / '.supervisor.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = PanelAuth(BASE_DIR / '.secrets', PANEL_ACCESS_TOKEN)
        manager = ProcessManager([str(BASE_DIR / '.venv/bin/python'), '-u', str(BASE_DIR / 'run.py')],
            BASE_DIR, logs / 'server.log', env=dict(external, HOST='127.0.0.1'), pid_path=logs / 'server.pid', stop_timeout=15)
        api_url = f'http://127.0.0.1:{PORT}'
        store = SQLiteTaskStore(TASKS_PATH)
        registry = ModelRegistry(store)
        vault = SecretVault(BASE_DIR, external,
                            lambda: (model.get('key_env') for model in registry.get()['models']))
        app = create_app(manager, auth, api_url, BOT_ID, PANEL_ALLOWED_HOSTS, log_root=BASE_DIR,
                         vault=vault, maintenance_store=store)
        maintenance_stop = threading.Event()
        def recover():
            target = _local_target(LOCAL_LM_BASE_URL)
            if target is not None and _ready(target):return 'already_healthy'
            return recover_local_server(LOCAL_LM_BASE_URL)
        def shutdown(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, shutdown)
        pid_path = logs / 'supervisor.pid'
        with tempfile.NamedTemporaryFile(mode='w', dir=logs, prefix='.supervisor-pid-', delete=False) as handle:
            handle.write(str(os.getpid()))
        os.replace(handle.name, pid_path)
        try:
            manager.start()
            threading.Thread(target=run_maintenance,args=(store,manager,app.extensions['control_lock'],api_url,
                recover,maintenance_stop),name='daily-maintenance',daemon=True).start()
            app.run(host='127.0.0.1', port=SUPERVISOR_PORT, use_reloader=False)
        finally:
            maintenance_stop.set()
            try:
                manager.stop()
            finally:
                pid_path.unlink(missing_ok=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
