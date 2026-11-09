"""Owner-only project editing in reviewable copies using the existing deploy flow."""
import threading
import uuid

from assistant_app.projects.project_changes import ProjectChanges, inventory, safe_file, sha


class PanelProjectChanges(ProjectChanges):
    def owner(self, task):
        if not self.enabled or task.get('source') != 'desktop' or task.get('user_id') != 'local_owner':
            raise PermissionError('Управление проектом доступно владельцу локальной панели')
        return sha(b'panel:local_owner')


class ProjectService:
    def __init__(self, root, enabled=True):
        self.manager = PanelProjectChanges(root=root, enabled=enabled)
        self.identity = dict(source='desktop', user_id='local_owner')
        self.lock = threading.RLock()

    def load(self, ident):
        return self.manager.load(ident, self.manager.owner(self.identity))

    def files(self, ident=None):
        self.manager.owner(self.identity)
        root = self.manager.job_dir(self.load(ident)['id']) / 'tree' if ident else self.manager.root
        return sorted(inventory(root))

    def read(self, name, ident=None):
        self.manager.owner(self.identity)
        root = self.manager.job_dir(self.load(ident)['id']) / 'tree' if ident else self.manager.root
        if name not in inventory(root):
            raise ValueError('Исходный файл не найден')
        data = safe_file(root, name).read_bytes()
        text = data.decode('utf-8')
        if self.manager.redact(text) != text:
            raise PermissionError('Файл содержит приватную конфигурацию')
        return dict(path=name, content=text, sha256=sha(data))

    def create(self):
        state, _ = self.manager.create(dict(self.identity, task_id='panel_' + uuid.uuid4().hex), 'Изменение из локальной панели')
        return dict(id=state['id'], phase=state['phase'], files=self.files(state['id']))

    def save_file(self, ident, name, content, expected_sha256):
        with self.lock:
            state = self.load(ident)
            if state['phase'] not in ('editing', 'ready', 'failed'):
                raise ValueError('Изменение сейчас недоступно для редактирования')
            if not isinstance(content, str) or len(content.encode()) > 256000 or '\0' in content:
                raise ValueError('Требуется текст до 256 КБ')
            if self.manager.redact(content) != content:
                raise ValueError('В файле найдены значения приватной конфигурации')
            root = self.manager.job_dir(ident) / 'tree'
            path = safe_file(root, name)
            current = sha(path.read_bytes()) if path.exists() else ''
            if current != expected_sha256:
                raise ValueError('Файл изменился; откройте его заново')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Atomic replacement; the live source tree remains untouched.
            temporary = path.with_name('.panel-' + uuid.uuid4().hex)
            try:
                temporary.write_text(content, encoding='utf-8')
                temporary.chmod(0o600)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            state.update(phase='editing', tests=None)
            self.manager.save(state)
            return dict(sha256=sha(content.encode()))

    def status(self, ident):
        state = self.load(ident)
        diff = self.manager.diff(state)
        return dict(id=ident, phase=state['phase'], summary=self.manager.status(state),
                    diff=diff[:100000], diff_truncated=len(diff) > 100000, tests=state.get('tests'))

    def execute(self, ident, action, task):
        state = self.load(ident)
        if action == 'test':
            if state['phase'] not in ('editing', 'failed', 'ready'):
                raise ValueError('Тесты недоступны в текущем состоянии')
            state['phase'] = 'testing'
            self.manager.save(state)
            try:
                result = self.manager.run_tests(state)
                state['phase'] = 'editing' if result['passed'] else 'failed'
                self.manager.save(state)
                if result['passed']:
                    self.manager.review(state, 'Изменения из панели; тесты пройдены')
                return ('Тесты пройдены.' if result['passed'] else 'Тесты не пройдены.') + '\n' + result['output']
            except Exception:
                state['phase'] = 'failed'
                self.manager.save(state)
                raise
        if action in ('apply', 'rollback'):
            return self.manager.deploy(state, task, rollback=action == 'rollback')
        raise ValueError('Неизвестное действие с проектом')
