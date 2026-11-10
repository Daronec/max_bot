"""HTTP control panel and loopback proxy for the assistant API."""
import threading
import time
from urllib.parse import urlsplit

import requests
from flask import Response, jsonify, request, session

from .auth import authorize_panel, create_panel_app, login_with_token


def create_app(manager, auth, api_url, bot_id='', allowed_hosts=('localhost', '127.0.0.1', '::1'), log_root=None, vault=None, maintenance_store=None):
    parsed = urlsplit(api_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('Supervisor can only manage a local API')
    app = create_panel_app(__name__)
    app.secret_key = auth.session_key

    @app.before_request
    def authorize():
        return authorize_panel(auth, allowed_hosts, protect_vault=True, vault=vault)

    @app.route('/')
    def index():
        return app.send_static_file('index.html')

    @app.route('/api/login', methods=['POST'])
    def login():
        data = request.get_json(silent=True)
        token = data.get('token') if isinstance(data, dict) else None
        return login_with_token(auth, token)

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
            return jsonify(success=True,unlocked=unlocked,entries=vault.entries(),
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
        from assistant_app.operations.diagnostics import log_tail
        try:
            return jsonify(success=True, **log_tail(log_root, request.args.get('name', 'supervisor.log')))
        except ValueError as error:
            return jsonify(success=False, error=str(error)), 400

    control_lock = threading.Lock()
    app.extensions['control_lock'] = control_lock

    @app.route('/api/maintenance')
    def maintenance():
        from assistant_app.operations.maintenance import SCHEDULE, STATE
        report = maintenance_store.runtime(STATE) if maintenance_store else None
        return jsonify(success=True, schedule=SCHEDULE, last=report)

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
