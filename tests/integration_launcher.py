"""Explicit launcher/deployment-control check in a disposable project copy.

No .env, real messages, models or production processes are used. Requires local
socket access. Run: .venv/bin/python tests/integration_launcher.py
"""
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
    import code_deploy
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for source in original.glob('*.py'):
            shutil.copy2(source, root / source.name)
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
            with patch.dict(os.environ, SUPERVISOR_PORT=str(panel_port), PANEL_ACCESS_TOKEN=''):
                code_deploy.stop_bot(root)
                wait_for(lambda: not status()['server_active'])
                assert status()['supervisor']['active']
                code_deploy.launch_bot(root)
                wait_for(lambda: status()['worker_active'])
            assert (root / 'logs/server.pid').is_file()
            assert (root / 'tasks/assistant.sqlite3').is_file()
            print(json.dumps(dict(result='passed', checks=['detached_launcher', 'authenticated_panel',
                'api_and_worker', 'deployment_stop', 'independent_panel', 'deployment_start'])))
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
