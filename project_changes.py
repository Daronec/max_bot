"""Owner-scoped, reviewable changes to the bot's own source tree."""
from contextlib import contextmanager
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import threading
import uuid

from config import BASE_DIR, CODE_CHANGES_ENABLED, CODE_ALLOWED_CHAT_IDS, CODE_ALLOWED_SENDER_IDS, CODE_TEST_TIMEOUT, HOST, PORT

_LOCK = threading.RLock()
JOB_ID = re.compile(r'^[a-f0-9]{12}$')
PUBLIC_ROOT_FILES = {'.env.example', '.gitignore', 'requirements.txt', 'README.md', 'START.md'}
PRIVATE_PARTS = {'.env', '.git', '.venv', '.code_changes', 'memory', 'tasks', 'logs',
                 'uploads', 'certs', '.secrets', '.models', '.bot_file_backups', '__pycache__'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_path(name):
    if not isinstance(name, str) or '\\' in name or '\x00' in name:
        raise ValueError('Нужен относительный путь исходного файла.')
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or '..' in path.parts or not path.parts:
        raise ValueError('Недопустимый путь.')
    if any(p in PRIVATE_PARTS or (p.startswith('.') and name not in PUBLIC_ROOT_FILES) for p in path.parts):
        raise ValueError('Личные данные и служебные каталоги недоступны.')
    allowed = (len(path.parts) == 1 and (path.suffix in {'.py', '.md', '.sh', '.command'} or name in PUBLIC_ROOT_FILES)
               or path.parts[0] == 'tests' and path.suffix == '.py'
               or path.parts[0] == 'helpers' and path.suffix in {'.swift', '.plist'}
               or path.parts[0] == 'static' and path.suffix in {'.html', '.css', '.js', '.json'})
    if not allowed:
        raise ValueError('Этот тип файла не входит в исходники проекта.')
    return name


def safe_file(root, name):
    source_path(name)
    root = Path(root).resolve()
    target = root / name
    if not target.resolve().is_relative_to(root):
        raise ValueError('Путь выходит за пределы копии проекта.')
    for part in (target, *target.parents):
        if part == root:break
        if part.is_symlink():raise ValueError('Символические ссылки запрещены.')
    if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
        raise ValueError('Нужен обычный исходный файл без жёстких ссылок.')
    return target


def inventory(root):
    result = {}
    for directory, folders, names in os.walk(root):
        relative = Path(directory).relative_to(root)
        folders[:] = [name for name in folders if name not in PRIVATE_PARTS and not name.startswith('.')
                      and (relative.parts or name in ('tests', 'helpers', 'static'))]
        for filename in names:
            file = Path(directory) / filename
            name = file.relative_to(root).as_posix()
            try:
                target = safe_file(root, name)
            except ValueError:
                continue
            data = target.read_bytes()
            if len(data) > 256000 or b'\x00' in data:continue
            data.decode('utf-8')
            result[name] = sha(data)
    return result


def atomic_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp, path)


class ProjectChanges:
    def __init__(self, root=BASE_DIR, enabled=CODE_CHANGES_ENABLED, allowed_chats=CODE_ALLOWED_CHAT_IDS,
                 test_timeout=CODE_TEST_TIMEOUT, allowed_senders=CODE_ALLOWED_SENDER_IDS):
        self.root = Path(root).resolve()
        self.directory = self.root / '.code_changes'
        self.enabled, self.allowed_chats = enabled, allowed_chats
        self.allowed_senders = allowed_senders
        self.test_timeout = test_timeout

    def owner(self, task):
        if (not self.enabled or task.get('source') != 'max' or task.get('memory_excluded')
                or str(task.get('chat_id')) not in self.allowed_chats or task.get('sender_id') is None):
            raise PermissionError('Изменение max_bot доступно только исходному сообщению известного отправителя в разрешённом чате.')
        if str(task['sender_id']) not in self.allowed_senders:
            raise PermissionError('Отправитель не разрешён для изменения max_bot. Доступ настраивается через CODE_ALLOWED_SENDER_IDS.')
        return sha(f"{task['chat_id']}:{task['sender_id']}".encode())

    def job_dir(self, ident):
        if not isinstance(ident, str) or not JOB_ID.fullmatch(ident):raise ValueError('Неверный ID изменения.')
        if self.directory.is_symlink():raise ValueError('Каталог изменений не должен быть ссылкой.')
        folder = self.directory / ident
        if folder.is_symlink():raise ValueError('Каталог изменения не должен быть ссылкой.')
        return folder

    def load(self, ident, owner):
        folder = self.job_dir(ident)
        if not (folder / 'state.json').is_file():raise ValueError('Изменение не найдено.')
        state = json.loads((folder / 'state.json').read_text())
        if state['owner'] != owner:raise PermissionError('Это изменение принадлежит другому отправителю.')
        return state

    def save(self, state):
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.job_dir(state['id']) / 'state.json', state)

    def create(self, task, request):
        owner = self.owner(task)
        ident = sha((owner + ':' + str(task['task_id'])).encode())[:12]
        folder = self.job_dir(ident)
        if folder.exists():return self.load(ident, owner), False
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        folder.mkdir(mode=0o700)
        # Only public source files are copied: no credentials, database or live tasks.
        files = inventory(self.root)
        if not files or 'run.py' not in files:raise ValueError('Исходники max_bot не найдены.')
        for area in ('base', 'tree'):
            destination = folder / area
            destination.mkdir(mode=0o700)
            for name in files:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(safe_file(self.root, name), target)
                target.chmod(0o600 if area == 'tree' else 0o400)
        state = {'id': ident, 'owner': owner, 'phase': 'editing', 'request': request,
                 'base': files, 'events': [str(task['task_id'])], 'summary': '', 'tests': None}
        self.save(state)
        return state, True

    def changes(self, state):
        folder = self.job_dir(state['id'])
        current = inventory(folder / 'tree')
        if any(name not in current for name in state['base']):
            raise ValueError('Удаление исходных файлов в этом процессе не поддерживается.')
        changes = {name: digest for name, digest in current.items() if state['base'].get(name) != digest}
        if len(changes) > 20:raise ValueError('Одно изменение может затрагивать не более 20 файлов.')
        return changes

    def fingerprint(self, state):
        return sha(json.dumps(inventory(self.job_dir(state['id']) / 'tree'), sort_keys=True).encode())

    def diff(self, state):
        folder = self.job_dir(state['id'])
        parts = []
        for name in self.changes(state):
            original = (folder / 'base' / name).read_text().splitlines(True) if name in state['base'] else []
            modified = safe_file(folder / 'tree', name).read_text().splitlines(True)
            parts.extend(difflib.unified_diff(original, modified, fromfile='a/' + name, tofile='b/' + name))
        return self.redact(''.join(parts))

    def secrets(self):
        from dotenv import dotenv_values
        return [item.strip() for key, value in dotenv_values(self.root / '.env').items()
                if value and any(term in key for term in ('TOKEN', 'API_KEY', 'CHAT_ID', 'SENDER_ID', 'BOT_ID'))
                for item in str(value).split(',') if len(item.strip()) >= 6]

    def redact(self, text):
        for value in self.secrets():text = text.replace(value, '[PRIVATE]')
        return text

    def verify(self, state):
        changes = self.changes(state)
        if not changes:raise ValueError('Нет изменений исходников.')
        folder = self.job_dir(state['id'])
        if inventory(folder / 'base') != state['base']:raise ValueError('Исходный снимок изменился.')
        for name in changes:
            text = safe_file(folder / 'tree', name).read_text()
            if any(secret in text for secret in self.secrets()):
                raise ValueError('В исходниках найдены значения из приватной конфигурации. Необходимо убрать их.')
            if name.endswith('.py'):compile(text, name, 'exec')
        return changes

    def run_tests(self, state):
        folder = self.job_dir(state['id'])
        self.verify(state)
        stamp = self.fingerprint(state)
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'TZ') if key in os.environ}
        environment.update({'MAX_BOT_TOKEN': 'test-token', 'MAX_BOT_ID': '@test_bot',
            'DEEPSEEK_API_KEY': 'test-key', 'LM_API_KEY': '', 'LOCAL_LM_API_KEY': '',
            'HF_HUB_OFFLINE': '1', 'RUN_INTENT_MODEL_TESTS': '0',
            'PYTHONPATH': str(folder / 'tree'), 'PYTHONPYCACHEPREFIX': str(folder / 'cache')})
        interpreter = self.root / '.venv/bin/python'
        if not interpreter.is_file():raise ValueError('Не найден Python проекта в .venv.')
        logs, passed = [], True
        for suite in (folder / 'base/tests', folder / 'tree/tests'):
            process = subprocess.Popen([str(interpreter), '-m', 'unittest', 'discover', '-s', str(suite), '-v'],
                cwd=folder / 'tree', env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                output, _ = process.communicate(timeout=self.test_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
                passed = False
                output += b'\nTest timeout\n'
            logs.append(output.decode('utf-8', errors='replace'))
            passed = passed and process.returncode == 0
            if not passed:break
        output = self.redact('\n'.join(logs))
        (folder / 'tests.log').write_text(output)
        if self.fingerprint(state) != stamp or inventory(folder / 'base') != state['base']:
            passed = False
            output += '\nSource changed during tests; rerun required.'
        state['tests'] = {'passed': passed, 'fingerprint': stamp}
        self.save(state)
        return {'passed': passed, 'output': output[-6500:]}

    def review(self, state, summary):
        self.verify(state)
        if not state.get('tests') or not state['tests']['passed'] or state['tests']['fingerprint'] != self.fingerprint(state):
            raise ValueError('Перед завершением нужны успешные тесты актуальной версии.')
        state.update(phase='ready', summary=self.redact(summary[:2000]))
        self.save(state)
        return self.status(state) + '\n\n' + self.diff(state)[:3500] + f"\n\nПрименить: /code apply {state['id']}\nПолный diff: /code diff {state['id']}"

    def status(self, state):
        names = ', '.join(self.changes(state)) or 'нет'
        tests = state.get('tests')
        labels = {'editing':'подготовка', 'ready':'готово к применению', 'failed':'подготовка не завершена',
                  'cancelled':'отменено', 'deploying':'применение или откат', 'applied':'применено, бот работает',
                  'rolled_back':'восстановлена прежняя версия', 'deployment_failed':'ошибка применения; смотрите причину'}
        return (f"Изменение {state['id']}: {labels.get(state['phase'], state['phase'])}.\n"
                f"{state.get('summary', '')}\nФайлы: {names}.\nТесты: " +
                ('пройдены' if tests and tests['passed'] else 'не пройдены или ещё не запускались') +
                ('\n' + state['deployment_result'] if state.get('deployment_result') else ''))

    def deploy(self, state, task, rollback=False):
        if state['phase'] in ('deploying', 'applied') and not rollback:return self.status(state)
        if rollback:
            if state['phase'] != 'applied':raise ValueError('Откат доступен только применённому изменению.')
        else:
            if state['phase'] != 'ready':raise ValueError('Изменение ещё не готово к применению.')
            self.verify(state)
            if not state.get('tests') or not state['tests']['passed'] or state['tests']['fingerprint'] != self.fingerprint(state):
                raise ValueError('После последней правки нужны повторные тесты.')
        changes = self.changes(state)
        expected_sources = {**state['base'], **changes} if rollback else state['base']
        if inventory(self.root) != expected_sources:
            raise ValueError('Исходники рабочего проекта изменились после подготовки. Создайте новую правку.')
        expected = changes if rollback else {name: state['base'].get(name) for name in changes}
        for name, digest in expected.items():
            path = safe_file(self.root, name)
            actual = sha(path.read_bytes()) if path.exists() else None
            if actual != digest:raise ValueError('Рабочие файлы изменились. Подготовьте новую правку: ' + name)
        for folder in self.directory.iterdir():
            other = folder / 'state.json'
            if other.is_file() and folder.name != state['id'] and json.loads(other.read_text()).get('phase') == 'deploying':
                raise ValueError('Другое обновление уже применяется.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(task['task_id'])):raise ValueError('Неверный ID сообщения.')
        folder = self.job_dir(state['id'])
        runner = folder / 'deploy_runner.py'
        shutil.copy2(self.root / 'code_deploy.py', runner)
        token = uuid.uuid4().hex
        previous = state['phase']
        state.update(phase='deploying', deployment={'token':token, 'task_id':str(task['task_id']),
                     'mode':'rollback' if rollback else 'apply', 'changes':changes, 'previous_phase':previous,
                     'host':'127.0.0.1' if HOST in ('0.0.0.0','::') else HOST, 'port':PORT})
        self.save(state)
        try:
            with (folder / 'deployment.log').open('ab') as log:
                subprocess.Popen([str(self.root / '.venv/bin/python'), str(runner), str(self.root), state['id'], token],
                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        except OSError:
            state['phase'] = previous
            self.save(state)
            raise ValueError('Не удалось запустить применение. Рабочий код не менялся.') from None
        return f"Изменение {state['id']}: {'откат' if rollback else 'применение'} начнётся после доставки этого сообщения. Бот перезапустится. Результат: /code status {state['id']}"


HELP = '''Изменения max_bot через сообщения:
/code <что улучшить> — подготовить правку в отдельной копии и проверить тестами.
/code status ID — состояние.
/code diff ID [страница] — изменения (по 3500 символов).
/code tests ID [страница] — вывод тестов.
/code revise ID <уточнение> — доработать подготовленную правку.
/code apply ID — применить проверенную правку и перезапустить бот.
/code rollback ID — вернуть версию до этого изменения.
/code cancel ID — отменить подготовку.
Можно написать: «Исправь в проекте max_bot …». Голосовые запросы работают после распознавания.
Секреты и личная память в копию не попадают. GitHub автоматически не публикуется.'''


def is_project_request(text):
    text = text.strip()
    return bool(re.match(r'^/code(?:\s|$)', text, re.I) or
                re.search(r'\bmax[_ -]?bot\b|макс[_ -]?бот|код\s+(?:самого\s+)?(?:бота|агафьи)|проект\s+(?:бота|агафьи)', text, re.I)
                and re.search(r'исправ|поправ|улучш|усовершен|оптимизир|почини|передел|добав|измен|реализ|доработ|внес|проверь|посмотр|почему|не\s+(?:работ|смог)', text, re.I))


project_changes = ProjectChanges()


def deployment_pending():
    # Keep polling MAX, but do not start another task between approval delivery
    # and restart. Delivery retries must remain available to the worker.
    if not project_changes.enabled or not project_changes.directory.is_dir():return False
    for path in project_changes.directory.glob('*/state.json'):
        try:
            if json.loads(path.read_text()).get('phase') == 'deploying':return True
        except (OSError,ValueError):continue
    return False


def handle_project_request(task, text, manager=None, notify=None):
    manager = manager or project_changes
    if not is_project_request(text):return None
    try:
        owner = manager.owner(task)
        command = re.sub(r'^/code\s*', '', text.strip(), flags=re.I)
        if not command or command.lower() in ('help', 'помощь'):return HELP
        match = re.match(r'^(status|diff|tests|apply|rollback|cancel|revise)\s+([a-f0-9]{12})(?:\s+(.*))?$', command, re.I | re.S)
        with _LOCK:
            if match:
                action, ident, tail = match.groups();action = action.lower()
                state = manager.load(ident, owner)
                if action == 'status':return manager.status(state)
                if action in ('diff', 'tests'):
                    page = int(tail or '1')
                    if not 1 <= page <= 10000:raise ValueError('Неверная страница.')
                    logfile = manager.job_dir(ident) / 'tests.log'
                    diff = manager.diff(state) if action == 'diff' else (manager.redact(logfile.read_text()) if logfile.exists() else 'Тесты ещё не запускались.')
                    start = (page - 1) * 3500
                    return f"{action} {ident}, страница {page}/{max(1,(len(diff)+3499)//3500)}:\n" + (diff[start:start+3500] or 'Нет изменений на этой странице.')
                if action in ('apply', 'rollback'):return manager.deploy(state, task, rollback=action == 'rollback')
                if action == 'cancel':
                    if state['phase'] in ('applied', 'deploying'):raise ValueError('Для применённой версии используйте rollback; применение уже запущено отменить нельзя.')
                    state['phase'] = 'cancelled';manager.save(state);return manager.status(state)
                if not tail:raise ValueError('Укажите уточнение после ID.')
                if state['phase'] not in ('ready', 'failed', 'editing'):raise ValueError('Доработать можно только неприменённую правку.')
                if str(task['task_id']) in state['events']:return manager.status(state)
                state['events'].append(str(task['task_id']))
                state.update(phase='editing', request=state['request'] + '\nУточнение: ' + tail, tests=None)
                manager.save(state)
            else:
                if re.match(r'^(status|diff|tests|apply|rollback|cancel|revise)\b', command, re.I):return HELP
                state, created = manager.create(task, command)
                if not created and state['phase'] != 'editing':return manager.status(state)
            if notify is not None:
                notify(f"Готовлю изменение {state['id']} в отдельной копии max_bot. Проверю тестами и пришлю diff. Рабочий бот пока не меняется.")
            from project_agent import prepare_change
            try:
                return prepare_change(manager, state)
            except Exception as error:
                state.update(phase='failed', summary='Подготовка остановлена: ' + manager.redact(str(error) or type(error).__name__)[:700] + '. Рабочий бот не менялся.')
                manager.save(state)
                return manager.status(state) + f"\nУточнить задачу: /code revise {state['id']} <текст>"
    except (ValueError, PermissionError, OSError) as error:
        return 'Изменение проекта не выполнено: ' + manager.redact(str(error))
