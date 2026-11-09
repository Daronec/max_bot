#!/usr/bin/env python3
"""Start the bot detached from Terminal, or report an existing local server."""
import fcntl
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

from assistant_app.config import BASE_DIR, BOT_ID, HOST, PORT, SUPERVISOR_PORT
from assistant_app.operations.code_deploy import supervisor_action


def running(host=HOST):
    connection = http.client.HTTPConnection(host, PORT, timeout=2)
    try:
        connection.request('GET', '/healthz')
        response = connection.getresponse()
        data = json.loads(response.read(65536))
        return response.status == 200 and data.get('bot_id') == BOT_ID and data.get('status') == 'ok'
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def main():
    logs = BASE_DIR / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / '.launcher.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Запуск уже выполняется. Подождите несколько секунд.')
            return 0
        managed = supervisor_action(BASE_DIR, 'start', SUPERVISOR_PORT)
        if managed:
            for _ in range(30):
                if running('127.0.0.1'):
                    print(f'Панель ассистента: http://127.0.0.1:{SUPERVISOR_PORT}')
                    return 0
                time.sleep(.5)
            print('Supervisor доступен, API не запустился. Проверьте logs/server.log.')
            return 1
        if running():
            print(f'Сервер бота уже работает: http://{HOST}:{PORT}')
            return 0
        try:
            with socket.create_connection((HOST, PORT), timeout=2):
                print(f'Порт {PORT} занят, но сервер бота не отвечает. Проверьте logs/server.log.')
                return 1
        except OSError:
            pass
        interpreter = BASE_DIR / '.venv/bin/python'
        if not interpreter.is_file():
            print('Не найдено окружение .venv. Сначала установите зависимости проекта.')
            return 1
        with (logs / 'supervisor.log').open('ab') as output:
            process = subprocess.Popen([str(interpreter), '-u', str(BASE_DIR / 'supervisor.py')],
                cwd=BASE_DIR, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                start_new_session=True)
        for _ in range(30):
            if process.poll() is not None:
                print('Сервер завершился при запуске. Откройте журнал: ' + str(logs / 'server.log'))
                return 1
            if running('127.0.0.1'):
                print(f'Панель ассистента: http://127.0.0.1:{SUPERVISOR_PORT}')
                print('Окно Terminal можно закрыть — бот продолжит работать.')
                print('Журнал: ' + str(logs / 'server.log'))
                return 0
            time.sleep(0.5)
        print('Процесс запущен, но сервер пока не ответил. Проверьте: ' + str(logs / 'server.log'))
        return 1


if __name__ == '__main__':
    sys.exit(main())
