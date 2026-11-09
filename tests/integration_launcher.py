"""Explicit launcher/deployment-control check in a disposable project copy.

No .env, real messages, models or production processes are used. Requires local
socket access. Run: .venv/bin/python tests/integration_launcher.py
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_for(check, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except requests.RequestException:
            pass
        time.sleep(.1)
    raise AssertionError('Local component did not reach the expected state')


def main():
    original = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(original))
    from assistant_app.operations import code_deploy
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for source in original.glob('*.py'):
            shutil.copy2(source, root / source.name)
        shutil.copytree(original / 'assistant_app', root / 'assistant_app', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(original / 'static', root / 'static')
        (root / '.venv').symlink_to(original / '.venv', target_is_directory=True)
        api_port, panel_port = free_port(), free_port()
        while panel_port == api_port:
            panel_port = free_port()
        environment = dict(os.environ, MAX_BOT_TOKEN='', MAX_BOT_ID='launcher-smoke',
            MAX_CHAT_ID='', DEEPSEEK_API_KEY='', LM_API_KEY='', LOCAL_LM_API_KEY='', PANEL_ACCESS_TOKEN='',
            PANEL_AUTH_ENABLED='true', PANEL_ALLOWED_HOSTS='localhost,127.0.0.1,::1', DEEPSEEK_ENABLED='false',
            HOST='127.0.0.1', PORT=str(api_port), SUPERVISOR_PORT=str(panel_port), WORKER_MODE='process',
            ASSISTANT_TASKS_DIR=str(root / 'tasks'), TELEGRAM_ENABLED='false', TELEGRAM_BOT_TOKEN='',
            NOTIFICATIONS_ENABLED='false', SUMMARY_ENABLED='false', MOOD_ENABLED='false',
            MEMORY_ENABLED='false', CODE_CHANGES_ENABLED='false', FS_ENABLED='false',
            COMMANDS_ENABLED='false', WEB_ENABLED='false', VOICE_ENABLED='false',
            HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        pid = None
        try:
            launch = subprocess.run([str(root / '.venv/bin/python'), str(root / 'launch_server.py')],
                cwd=root, env=environment, capture_output=True, text=True, timeout=25)
            if (root / 'logs/supervisor.pid').is_file():
                pid = int((root / 'logs/supervisor.pid').read_text())
            if launch.returncode:
                raise AssertionError(launch.stdout + launch.stderr)
            token = (root / '.secrets/panel.token').read_text().strip()
            headers = {'Authorization':'Bearer ' + token}
            def status():
                response = requests.get(f'http://127.0.0.1:{panel_port}/api/status', headers=headers, timeout=3)
                response.raise_for_status()
                return response.json()
            wait_for(lambda: status()['worker_active'])
            panel_url = f'http://127.0.0.1:{panel_port}'
            with requests.Session() as browser:
                assert 'Секреты проекта' in browser.get(panel_url + '/', timeout=3).text
                assert browser.get(panel_url + '/static/index.html', timeout=3).ok
                assert browser.get(panel_url + '/api/runs', timeout=3).status_code == 401
                login = browser.post(panel_url + '/api/login', json={'token': token}, timeout=3)
                login.raise_for_status()
                csrf = {'X-CSRF-Token': login.json()['csrf']}
                assert browser.get(panel_url + '/api/session', timeout=3).json()['pin_setup_required']
                assert browser.get(panel_url + '/api/runs', timeout=3).status_code == 403
                pin = {'pin': '123456', 'confirmation': '123456'}
                assert browser.post(panel_url + '/api/vault/setup', json=pin, timeout=3).status_code == 403
                assert browser.post(panel_url + '/api/vault/setup', json=pin, headers=csrf, timeout=3).ok
                assert not browser.get(panel_url + '/api/session', timeout=3).json()['pin_setup_required']
                vault = browser.get(panel_url + '/api/vault', timeout=3)
                assert vault.ok and vault.headers['Cache-Control'] == 'no-store'
                assert requests.get(panel_url + '/api/vault', headers=headers, timeout=3).status_code == 401
                for path in ('/api/runs', '/api/conversations', '/api/identities', '/api/maintenance'):
                    assert browser.get(panel_url + path, timeout=3).json()['success']
                logs = browser.get(panel_url + '/api/logs?name=supervisor.log', timeout=3)
                assert logs.ok and logs.json()['name'] == 'supervisor.log'
                before_restart = status()['supervisor']['server']['pid']
                assert browser.post(panel_url + '/api/server', json={'action': 'restart'}, headers=csrf, timeout=20).ok
                wait_for(lambda: status()['worker_active'] and status()['supervisor']['server']['pid'] != before_restart)
                missing = browser.get(panel_url + '/task_status/missing', timeout=3)
                assert missing.status_code == 404 and missing.json()['success'] is False
                assert browser.post(panel_url + '/api/logout', json={}, headers=csrf, timeout=3).ok
                assert browser.get(panel_url + '/api/session', timeout=3).status_code == 401
            with patch.dict(os.environ, SUPERVISOR_PORT=str(panel_port), PANEL_ACCESS_TOKEN=''):
                code_deploy.stop_bot(root)
                wait_for(lambda: not status()['server_active'])
                assert status()['supervisor']['active']
                code_deploy.launch_bot(root)
                wait_for(lambda: status()['worker_active'])
            def maintenance():
                response = requests.get(panel_url + '/api/maintenance', headers=headers, timeout=3)
                response.raise_for_status()
                return response.json()

            assert maintenance()['schedule'] == '04:00 Europe/Moscow'
            now = datetime.now(ZoneInfo('Europe/Moscow'))
            if now.hour >= 4:
                def completed_maintenance():
                    report = maintenance()['last']
                    return report if report and report['action'] != 'started' else None

                report = wait_for(completed_maintenance, timeout=45)
                assert report['day'] == now.date().isoformat() and report['action'] == 'none'
            else:
                assert maintenance()['last'] is None
            assert (root / 'logs/server.pid').is_file()
            assert (root / 'tasks/assistant.sqlite3').is_file()
            print(json.dumps(dict(result='passed', checks=['detached_launcher', 'panel_and_static',
                'login_csrf_pin_logout', 'proxied_and_legacy_routes', 'api_and_worker', 'panel_restart', 'diagnostics',
                'daily_maintenance' if now.hour >= 4 else 'maintenance_waiting_for_schedule',
                'deployment_stop', 'independent_panel', 'deployment_start'])))
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
                    if state.returncode or state.stdout.strip().startswith('Z'):
                        break
                    time.sleep(.1)
                else:
                    os.kill(pid, signal.SIGKILL)


if __name__ == '__main__':
    main()
