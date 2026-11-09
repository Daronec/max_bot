"""Local panel authentication; secrets never enter model context or API responses."""
from collections import defaultdict, deque
import hmac
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, session

from assistant_app.paths import PROJECT_ROOT


def create_panel_app(name):
    app = Flask(name, static_folder=str(PROJECT_ROOT / 'static'), static_url_path='/static')
    app.config.update(MAX_CONTENT_LENGTH=22 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict')
    return app


def is_public_panel_path(path):
    return path in ('/', '/api/login', '/healthz') or path.startswith('/static/')


def authorize_panel(auth, allowed_hosts, *, protect_vault=False, vault=None):
    """Validate local origin, authorization and CSRF before running a panel route."""
    if (urlsplit('http://' + request.host).hostname or '').lower() not in allowed_hosts:
        return jsonify(success=False, error='Недопустимый адрес панели'), 403
    changes_state = request.method not in ('GET', 'HEAD', 'OPTIONS')
    if changes_state:
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return jsonify(success=False, error='Недопустимый источник запроса'), 403
    path = request.path
    if is_public_panel_path(path):
        return None
    bearer = request.headers.get('Authorization', '')
    browser_only = protect_vault and path.startswith('/api/vault')
    if not browser_only and bearer.startswith('Bearer ') and auth.matches(bearer[7:]):
        return None
    if not session.get('authenticated'):
        return jsonify(success=False, error='Требуется вход в панель'), 401
    if changes_state:
        csrf = request.headers.get('X-CSRF-Token', '')
        if not csrf or not hmac.compare_digest(csrf, session.get('csrf', '')):
            return jsonify(success=False, error='Недопустимый CSRF-токен'), 403
    if vault and not vault.configured() and path not in ('/api/session', '/api/logout', '/api/vault/setup'):
        return jsonify(success=False, error='Сначала создайте PIN хранилища.'), 403
    return None


def login_with_token(auth, token):
    outcome = auth.login(request.remote_addr or '', token)
    if outcome != 'ok':
        if outcome == 'limited':
            return jsonify(success=False, error='Слишком много попыток'), 429
        return jsonify(success=False, error='Неверный ключ'), 401
    session.clear()
    session.update(authenticated=True, csrf=secrets.token_urlsafe(32))
    return jsonify(success=True, csrf=session['csrf'])


class PanelAuth:
    def __init__(self, directory, token=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token = token or self._secret('panel.token')
        self.session_key = self._secret('panel.session')
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def _secret(self, name):
        path = self.directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = path.read_text().strip()
            if not value:
                raise RuntimeError('Panel secret is empty')
            return value
        value = secrets.token_urlsafe(32)
        with os.fdopen(descriptor, 'w') as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        return value

    def matches(self, value):
        return isinstance(value, str) and hmac.compare_digest(self.token, value)

    def login(self, address, value):
        with self._lock:
            attempts = self._attempts[address]
            now = time.monotonic()
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return 'limited'
            if not self.matches(value):
                attempts.append(now)
                return 'invalid'
            attempts.clear()
            return 'ok'
