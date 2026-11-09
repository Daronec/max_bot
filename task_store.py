"""Transactional task storage and verified, non-destructive JSON migration."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from task_service import TaskConflict, record_lifecycle


def validate_task(task):
    tid = task.get('task_id')
    if not isinstance(tid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', tid):
        raise ValueError('Invalid task ID')
    if not isinstance(task.get('message'), str) or not isinstance(task.get('status'), str):
        raise ValueError('Invalid task record')
    if task['status'] not in ('sending', 'queued', 'processing', 'completed', 'failed', 'cancelled'):
        raise ValueError('Unknown task status')
    if task.get('result') is not None and not isinstance(task['result'], str):
        raise ValueError('Invalid task result')
    if 'timestamp' in task and not isinstance(task['timestamp'], str):
        raise ValueError('Invalid task timestamp')
    if not isinstance(task.get('events', []), list) or any(
            not isinstance(event, dict) or not isinstance(event.get('status'), str)
            for event in task.get('events', [])):
        raise ValueError('Invalid lifecycle events')


class SQLiteTaskStore:
    SCHEMA_VERSION = 4

    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.resolve() in (Path('/'), Path.home().resolve(), Path(tempfile.gettempdir()).resolve()):
            raise ValueError('Use a dedicated private task directory')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / 'assistant.sqlite3'
        # Create with owner-only permissions before sqlite opens the file.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            with self.transaction(db):
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version > self.SCHEMA_VERSION:
                    raise RuntimeError('Task database requires a newer application')
                db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, payload TEXT NOT NULL)''')
                db.execute('CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status)')
                db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute('''CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                    created_at TEXT NOT NULL)''')
                columns = {row[1] for row in db.execute('PRAGMA table_info(conversations)')}
                if 'local_only' not in columns:
                    db.execute('ALTER TABLE conversations ADD COLUMN local_only INTEGER NOT NULL DEFAULT 0')
                db.execute('''CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)''')
                db.execute('CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id)')
                db.execute('''CREATE TABLE IF NOT EXISTS assistant_projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL)''')
                db.execute('CREATE TABLE IF NOT EXISTS conversation_projects (conversation TEXT PRIMARY KEY, project TEXT NOT NULL)')
                db.execute('PRAGMA user_version=4')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    @staticmethod
    @contextmanager
    def transaction(db):
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @staticmethod
    def decode(row):
        if row is None:
            return None
        record = json.loads(row[1])
        record['_revision'] = row[0]
        return record

    def get(self, task_id):
        with self.connect() as db:
            return self.decode(db.execute('SELECT revision,payload FROM tasks WHERE task_id=?',
                                          (task_id,)).fetchone())

    def export(self, task):
        """Atomic owner-only compatibility exports; never used as canonical reads."""
        validate_task(task)
        files = {task['task_id'] + '.json': json.dumps(task, ensure_ascii=False, indent=2),
                 task['task_id'] + '.txt': task['message']}
        if task.get('result') is not None:
            files[task['task_id'] + '_result.md'] = task['result']
        for name, content in files.items():
            descriptor, temporary = tempfile.mkstemp(prefix='.export-', suffix='.tmp', dir=self.directory)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    output.write(content)
                os.replace(temporary, self.directory / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def all(self):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute('SELECT revision,payload FROM tasks ORDER BY rowid')]

    def set_runtime(self, name, value):
        with self.connect() as db:
            db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('runtime:' + name, json.dumps(value)))

    def runtime(self, name):
        with self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', ('runtime:' + name,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def write(db, task):
        validate_task(task)
        expected = task.get('_revision', 0)
        revision = expected + 1
        payload = dict(task)
        payload.pop('_revision', None)
        encoded = json.dumps(payload, ensure_ascii=False)
        if expected:
            changed = db.execute('UPDATE tasks SET status=?,revision=?,payload=? WHERE task_id=? AND revision=?',
                                 (task['status'], revision, encoded, task['task_id'], expected)).rowcount
            if not changed:
                raise TaskConflict('Task changed; reload before retrying')
        else:
            try:
                db.execute('INSERT INTO tasks VALUES(?,?,?,?)', (task['task_id'], task['status'], revision, encoded))
            except sqlite3.IntegrityError as error:
                raise TaskConflict('Task already exists') from error
        return revision

    def save(self, task):
        with self.connect() as db, self.transaction(db):
            if task['status']=='sending' and not task.get('_revision') and self._maintenance_paused(db):
                raise TaskConflict('Сервер перезапускается. Повторите запрос после запуска.')
            revision = self.write(db, task)
            if task.get('transcription'):
                db.execute("UPDATE messages SET content=? WHERE id=? AND role='user'", (task['transcription'], task['task_id']))
            if task.get('conversation_id') and task['status'] == 'completed':
                db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)',
                           ('answer:' + task['task_id'], task['conversation_id'], 'assistant',
                            task['result'], task['completed_at']))
        task['_revision'] = revision

    def conversations(self, owner):
        with self.connect() as db:
            return [dict(id=row[0], title=row[1], created_at=row[2], project_id=row[3]) for row in db.execute(
                'SELECT c.id,c.title,c.created_at,p.project FROM conversations c LEFT JOIN conversation_projects p ON p.conversation=c.id WHERE c.owner=? ORDER BY c.created_at DESC', (owner,))]

    def conversation_privacy(self, conversation_id, owner, mark_private=False):
        with self.connect() as db, self.transaction(db):
            row = db.execute('SELECT local_only FROM conversations WHERE id=? AND owner=?', (conversation_id, owner)).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if mark_private:
                db.execute('UPDATE conversations SET local_only=1 WHERE id=? AND owner=?', (conversation_id, owner))
            return bool(row[0] or mark_private)

    def messages(self, conversation_id, owner, limit=40):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM conversations WHERE id=? AND owner=?', (conversation_id, owner)).fetchone():
                raise KeyError(conversation_id)
            rows = db.execute('SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT ?',
                              (conversation_id, limit)).fetchall()
            return [dict(role=row[0], content=row[1], created_at=row[2]) for row in reversed(rows)]

    def submit_chat(self, message, owner, conversation_id=None, allow_tools=False, model_target='auto', use_knowledge=True, audio_upload=None, project_id=None):
        import uuid
        if not isinstance(message, str) or not message.strip():
            raise ValueError('Требуется непустой текст')
        if model_target not in ('auto', 'qwen', 'deepseek') and not (isinstance(model_target, str) and re.fullmatch(r'model:[A-Za-z0-9_-]{1,60}', model_target)):
            raise ValueError('Неизвестная модель')
        if conversation_id is not None and (not isinstance(conversation_id, str) or not re.fullmatch(r'[a-f0-9]{32}', conversation_id)):
            raise ValueError('Некорректный идентификатор диалога')
        stamp = datetime.now(timezone.utc).isoformat()
        with self.connect() as db, self.transaction(db):
            if project_id and (owner != 'local_owner' or not db.execute('SELECT 1 FROM assistant_projects WHERE id=?', (project_id,)).fetchone()):
                raise KeyError(project_id)
            if conversation_id:
                if not db.execute('SELECT 1 FROM conversations WHERE id=? AND owner=?', (conversation_id, owner)).fetchone():
                    raise KeyError(conversation_id)
                previous_project = db.execute('SELECT project FROM conversation_projects WHERE conversation=?', (conversation_id,)).fetchone()
                bound_project = previous_project[0] if previous_project else None
                if project_id and project_id != bound_project:
                    raise ValueError('Для другого проекта создайте новый диалог')
                project_id = bound_project
            else:
                conversation_id = uuid.uuid4().hex
                db.execute('INSERT INTO conversations(id,owner,title,created_at) VALUES(?,?,?,?)', (conversation_id, owner, message[:80], stamp))
                if project_id:
                    db.execute('INSERT INTO conversation_projects VALUES(?,?)', (conversation_id, project_id))
            # Serialize turns so each response sees the preceding completed answer.
            pending = db.execute("SELECT payload FROM tasks WHERE status IN ('queued','processing')").fetchall()
            if any(json.loads(row[0]).get('conversation_id') == conversation_id for row in pending):
                raise TaskConflict('A turn is still running in this conversation')
            tid = 'chat_' + uuid.uuid4().hex
            task = dict(task_id=tid, message=message, chat_id='desktop:' + owner, source='desktop',
                        user_id=owner, conversation_id=conversation_id, status='queued',
                        result=None, timestamp=stamp, allow_tools=bool(allow_tools), model_target=model_target,
                        use_knowledge=bool(use_knowledge))
            if project_id:
                task['project_id'] = project_id
            if audio_upload:
                if not re.fullmatch(r'[a-f0-9]{32}', audio_upload):
                    raise ValueError('Некорректное аудиовложение')
                task['audio_upload'] = audio_upload
            record_lifecycle(task)
            revision = self.write(db, task)
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (tid, conversation_id, 'user', message, stamp))
        task['_revision'] = revision
        return task

    def claim(self, task_id, now):
        """Compare and transition a due queued task in one database transaction."""
        with self.connect() as db, self.transaction(db):
            if self._maintenance_paused(db):
                return None
            task = self.decode(db.execute('SELECT revision,payload FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            if task is None or task['status'] != 'queued' or task.get('retry_at', 0) > now:
                return None
            task.update(status='processing', attempts=task.get('attempts', 0) + 1)
            record_lifecycle(task)
            revision = self.write(db, task)
        task['_revision'] = revision
        return task

    def claim_delivery(self, task_id):
        with self.connect() as db, self.transaction(db):
            if self._maintenance_paused(db):
                return None
            task = self.decode(db.execute('SELECT revision,payload FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            if (task is None or task['status'] != 'completed' or task.get('delivered', True)
                    or task.get('delivery_inflight') or task.get('delivery_uncertain')):
                return None
            task['delivery_inflight'] = True
            task['delivery_owner_pid'] = os.getpid()
            revision = self.write(db, task)
        task['_revision'] = revision
        return task

    @staticmethod
    def _maintenance_paused(db):
        import time
        row = db.execute("SELECT value FROM metadata WHERE key='runtime:maintenance_pause'").fetchone()
        return bool(row and json.loads(row[0]).get('until',0)>time.time())

    def migrate_json(self):
        """Snapshot every JSON source before importing; commit all valid records once.

        Invalid files remain untouched and are listed in the migration report.
        A database row always wins over a legacy export with the same identifier.
        """
        with self.connect() as db, self.transaction(db):
            existing = db.execute("SELECT value FROM metadata WHERE key='json_migration_v1'").fetchone()
            if existing:
                return json.loads(existing[0])
            sources = [(path, path.read_bytes()) for path in sorted(self.directory.glob('*.json'))]
            digest = hashlib.sha256()
            for path, data in sources:
                digest.update(path.name.encode() + b'\0' + data + b'\0')
            backup = self.directory / 'legacy-backups' / digest.hexdigest()
            if sources:
                backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            report = dict(imported=0, existing=0, rejected=[], backup=str(backup) if sources else None,
                          timestamp=datetime.now(timezone.utc).isoformat(), sha256=digest.hexdigest())
            for path, data in sources:
                destination = backup / path.name
                # A retry after an interrupted migration must verify its existing snapshot.
                try:
                    with destination.open('xb') as output:
                        os.chmod(destination, 0o600)
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                except FileExistsError:
                    pass
                if destination.read_bytes() != data:
                    raise OSError('Legacy backup verification failed')
                try:
                    task = json.loads(data)
                    if not isinstance(task, dict):
                        raise ValueError('Expected object')
                    validate_task(task)
                    if task['task_id'] != path.stem:
                        raise ValueError('Filename does not match task ID')
                except (ValueError, KeyError, TypeError, UnicodeError):
                    report['rejected'].append(path.name)
                    continue
                if db.execute('SELECT 1 FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone():
                    report['existing'] += 1
                    continue
                task.pop('_revision', None)
                task.setdefault('source', 'max' if task['task_id'].startswith('max_') else 'web')
                record_lifecycle(task)
                self.write(db, task)
                stored = self.decode(db.execute('SELECT revision,payload FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone())
                stored.pop('_revision')
                if stored != task:
                    raise RuntimeError('Legacy import verification failed')
                report['imported'] += 1
            db.execute('INSERT INTO metadata VALUES(?,?)', ('json_migration_v1', json.dumps(report)))
        return report

    def backup(self, destination):
        """Consistent snapshot including committed WAL contents."""
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError('Backup must have a different path')
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
            finally:
                target.close()
