"""Recover a stopped, explicitly configured local LM Studio server on connection refusal."""
from pathlib import Path
from contextvars import ContextVar
from functools import wraps
import logging
from contextlib import contextmanager
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from config import LM_AUTO_START, LMS_CLI, LMS_START_TIMEOUT, LM_BASE_URL
from config import (LM_PROVIDER, LM_FALLBACK_ENABLED, LOCAL_LM_BASE_URL, LOCAL_LM_MODEL,
                    LOCAL_LM_API_KEY, DEEPSEEK_REQUEST_TIMEOUT, LM_FALLBACK_COOLDOWN)

_lock = threading.Lock()
_last_attempt = None


class ModelServerUnavailable(RuntimeError):
    pass


def _local_target(url):
    target = urlsplit(url)
    configured = urlsplit(LM_BASE_URL)
    if target.hostname in ('127.0.0.1', 'localhost') and target.port == urlsplit(LOCAL_LM_BASE_URL).port:
        configured = urlsplit(LOCAL_LM_BASE_URL)
    if (target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost')
            or target.hostname != configured.hostname or target.port != configured.port):
        return None
    return target


def _ready(target):
    root = urlunsplit((target.scheme, target.netloc, '/v1/models', '', ''))
    try:
        # Authentication errors also prove that the listener is running; let the actual request report them.
        return requests.get(root, timeout=2).status_code in (200, 401, 403)
    except requests.RequestException:
        return False


def recover_local_server(url):
    global _last_attempt
    target = _local_target(url)
    if not LM_AUTO_START or target is None:
        return False
    with _lock:
        if _ready(target):
            return True
        if _last_attempt is not None and time.monotonic() - _last_attempt < 30:
            raise ModelServerUnavailable('Локальный сервер модели ещё недоступен; задание сохранено, повторю подключение позже.')
        _last_attempt = time.monotonic()
        cli = Path(LMS_CLI).expanduser()
        if not cli.is_file():
            raise ModelServerUnavailable('Не найден lms для автозапуска. Откройте LM Studio/Bionic и включите локальный сервер.')
        try:
            result = subprocess.run([str(cli), 'server', 'start', '--port', str(target.port or 1234),
                                     '--bind', '127.0.0.1'], timeout=LMS_START_TIMEOUT,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    start_new_session=True)
            success = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            success = False
        deadline = time.monotonic() + (5 if success else 1)
        while True:
            if _ready(target):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        raise ModelServerUnavailable('На порту ' + str(target.port or 1234) +
                                     ' нет сервера LM Studio, автозапуск не удался. '
                                     'Откройте LM Studio/Bionic и включите сервер. Задание сохранено.')


def _request(method, url, **kwargs):
    try:
        return getattr(requests, method)(url, **kwargs)
    except requests.ConnectionError as error:
        refused = 'connection refused' in str(error).lower()
        if refused and recover_local_server(url):
            return getattr(requests, method)(url, **kwargs)
        raise


logger = logging.getLogger(__name__)
_selected_backend = ContextVar('selected_backend', default=None)


def current_backend():
    return _selected_backend.get()


@contextmanager
def intent_session(decision):
    from config import (DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, DEEPSEEK_API_KEY,
                        LOCAL_LM_BASE_URL, LOCAL_LM_MODEL, LOCAL_LM_API_KEY)
    remote = decision.target == 'deepseek'
    backend = {'provider': 'deepseek' if remote else 'lmstudio',
               'base_url': DEEPSEEK_BASE_URL if remote else LOCAL_LM_BASE_URL,
               'model': DEEPSEEK_MODEL if remote else LOCAL_LM_MODEL,
               'api_key': DEEPSEEK_API_KEY if remote else LOCAL_LM_API_KEY}
    token = _selected_backend.set(backend)
    local_token = _local_session.set(False)
    with _route_lock:
        _route.update(last_provider=backend['provider'], fallback_reason=decision.reason)
    try:
        yield
    finally:
        _local_session.reset(local_token)
        _selected_backend.reset(token)


_local_session = ContextVar('local_model_session', default=False)
_retry_deepseek_at = 0.0
_route_lock = threading.Lock()
_route = {'last_provider': None, 'fallback_reason': None}


def model_session(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _local_session.set(False)
        try:
            return function(*args, **kwargs)
        finally:
            _local_session.reset(token)
    return wrapped


def routing_status():
    from intent_router import deepseek_availability, DEEPSEEK_ENABLED
    with _route_lock:
        return {**_route, 'fallback_enabled': LM_FALLBACK_ENABLED,
                'deepseek_enabled': DEEPSEEK_ENABLED,
                'deepseek_available': DEEPSEEK_ENABLED and deepseek_availability.available(),
                'deepseek_retry_in_seconds': deepseek_availability.retry_in_seconds(),
                'local_model': LOCAL_LM_MODEL}


def _fallback(kwargs):
    # Preserve conversation and tool receipts, never restart the original task.
    local = dict(kwargs)
    payload = dict(local.get('json') or {})
    payload['model'] = LOCAL_LM_MODEL
    payload.pop('thinking', None)
    payload['reasoning_effort'] = 'none'
    payload['messages'] = [{k:v for k,v in message.items() if k != 'reasoning_content'}
                           for message in payload.get('messages', [])]
    local['json'] = payload
    headers = {k:v for k,v in local.get('headers', {}).items() if k.lower() != 'authorization'}
    if LOCAL_LM_API_KEY: headers['Authorization'] = 'Bearer ' + LOCAL_LM_API_KEY
    local['headers'] = headers
    # The local model can need longer than the remote failover deadline.
    from config import LM_TIMEOUT
    local['timeout'] = (10, LM_TIMEOUT)
    _local_session.set(True)
    with _route_lock: _route['last_provider'] = 'lmstudio'
    return _request('post', LOCAL_LM_BASE_URL + '/chat/completions', **local)


def model_post(url, **kwargs):
    global _retry_deepseek_at
    backend = current_backend()
    if backend is not None:
        # Actual API failure handling stays next to the request. Continue the same
        # conversation on Qwen, preserving tool receipts and avoiding replay.
        from intent_router import deepseek_availability
        if backend['provider'] != 'deepseek':
            return _request('post', url, **kwargs)
        if _local_session.get() or not deepseek_availability.available():
            return _fallback(kwargs)
        primary = dict(kwargs)
        primary['timeout'] = (10, DEEPSEEK_REQUEST_TIMEOUT)
        try:
            response = _request('post', url, **primary)
            response.raise_for_status()
            if isinstance(response.status_code, int) and response.status_code >= 400:
                raise requests.HTTPError(response=response)
        except requests.RequestException as error:
            deepseek_availability.mark_unavailable()
            with _route_lock:
                _route.update(fallback_reason='deepseek_error', last_provider='lmstudio')
            logger.warning('intent_route target=qwen reason=deepseek_error error=%s', type(error).__name__)
            return _fallback(kwargs)
        deepseek_availability.mark_available()
        return response
    remote = LM_PROVIDER == 'deepseek' and urlsplit(url).hostname == 'api.deepseek.com'
    if not remote or not LM_FALLBACK_ENABLED:
        return _request('post', url, **kwargs)
    with _route_lock:
        cooling = time.monotonic() < _retry_deepseek_at
    if _local_session.get() or cooling:
        return _fallback(kwargs)
    primary = dict(kwargs)
    primary['timeout'] = (10, DEEPSEEK_REQUEST_TIMEOUT)
    reason = None
    try:
        response = _request('post', url, **primary)
        status = response.status_code
        if isinstance(status, int) and (status in (401, 402, 403, 408, 429) or status >= 500):
            reason = 'HTTP ' + str(status)
        else:
            with _route_lock:
                _route.update(last_provider='deepseek', fallback_reason=None)
            return response
    except (requests.ConnectionError, requests.Timeout):
        reason = 'timeout_or_connection_error'
    with _route_lock:
        _retry_deepseek_at = time.monotonic() + LM_FALLBACK_COOLDOWN
        _route.update(fallback_reason=reason)
    return _fallback(kwargs)



def model_get(url, **kwargs):
    return _request('get', url, **kwargs)
