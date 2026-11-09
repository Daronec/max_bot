"""Durable single-owner schedules; task creation and advancing are one transaction."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant_app.core.task_service import record_lifecycle


class AutomationService:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS automations (
                id TEXT PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL,
                next_due REAL NOT NULL, revision INTEGER NOT NULL)''')

    @staticmethod
    def validate(config):
        if not isinstance(config, dict) or set(config) - {'title', 'prompt', 'kind', 'interval', 'time', 'timezone', 'missed', 'project_id', 'channel_link'}:
            raise ValueError('Некорректное расписание')
        for key, limit in (('title', 120), ('prompt', 32000)):
            if not isinstance(config.get(key), str) or not config[key].strip() or len(config[key]) > limit:
                raise ValueError('Укажите название и текст задачи')
        if config.get('missed', 'skip') not in ('skip', 'once'):
            raise ValueError('Политика пропущенных запусков: skip или once')
        if config.get('kind') == 'interval':
            value = config.get('interval')
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 60 <= value <= 31536000:
                raise ValueError('Интервал должен быть от минуты до года')
        elif config.get('kind') == 'daily':
            try:
                ZoneInfo(config.get('timezone', 'Europe/Moscow'))
                value = datetime.strptime(config.get('time', ''), '%H:%M')
                if value.strftime('%H:%M') != config['time']:
                    raise ValueError()
            except (ValueError, TypeError, ZoneInfoNotFoundError):
                raise ValueError('Укажите время HH:MM и часовой пояс IANA') from None
        else:
            raise ValueError('Неизвестный тип расписания')
        return dict(config)

    @staticmethod
    def following(config, after):
        if config['kind'] == 'interval':
            return after + config['interval']
        zone = ZoneInfo(config.get('timezone', 'Europe/Moscow'))
        local = datetime.fromtimestamp(after, zone)
        hour, minute = map(int, config['time'].split(':'))
        day = local.date()
        # Roundtrip rejects nonexistent spring times; fall overlap runs only once.
        for _ in range(370):
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            stamp = candidate.timestamp()
            back = datetime.fromtimestamp(stamp, zone)
            if stamp > after and (back.hour, back.minute) == (hour, minute):
                return stamp
            day += timedelta(days=1)
        raise ValueError('Не удалось вычислить следующий запуск')

    def list(self):
        with self.store.connect() as db:
            return [dict(id=r[0], config=json.loads(r[1]), enabled=bool(r[2]), next_due=r[3], revision=r[4])
                    for r in db.execute('SELECT * FROM automations ORDER BY rowid DESC')]

    def create(self, config, now=None):
        config = self.validate(config)
        from assistant_app.projects.project_context import ProjectContext
        ProjectContext(self.store).scope(config.get('project_id'))
        if config.get('channel_link'):
            from assistant_app.channels.identities import IdentityService
            link = next((link for link in IdentityService(self.store).links() if link['id'] == config['channel_link']), None)
            if not link or link['state'] != 'active' or not link['conversation_id']:
                raise ValueError('Выберите подтвержденный канал с общим диалогом')
            config['conversation_id'] = link['conversation_id']
            bound = ProjectContext(self.store).conversation_project(link['conversation_id'])
            if config.get('project_id') and config['project_id'] != bound:
                raise ValueError('Проект расписания должен совпадать с проектом общего диалога')
            config['project_id'] = bound
        ident = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('INSERT INTO automations VALUES(?,?,1,?,1)',
                       (ident, json.dumps(config, ensure_ascii=False), self.following(config, time.time() if now is None else now)))
        return ident

    def toggle(self, ident, enabled, revision):
        if not isinstance(enabled, bool) or isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError('Укажите enabled и revision')
        from assistant_app.core.task_service import TaskConflict
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute('SELECT config,revision FROM automations WHERE id=?', (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            if row[1] != revision:
                raise TaskConflict('Automation changed')
            due = self.following(json.loads(row[0]), time.time())
            db.execute('UPDATE automations SET enabled=?,revision=revision+1,next_due=? WHERE id=?', (int(enabled), due, ident))

    def tick(self, now=None):
        now = time.time() if now is None else now
        created = []
        with self.store.connect() as db, self.store.transaction(db):
            due = db.execute('SELECT id,config,next_due FROM automations WHERE enabled=1 AND next_due<=? ORDER BY next_due LIMIT 100', (now,)).fetchall()
            pending = [json.loads(r[0]) for r in db.execute("SELECT payload FROM tasks WHERE status IN ('queued','processing')")]
            for ident, raw, scheduled in due:
                config = json.loads(raw)
                next_due = self.following(config, now)
                db.execute('UPDATE automations SET next_due=? WHERE id=?', (next_due, ident))
                if any(t.get('automation_id') == ident for t in pending):
                    continue
                # A one-minute grace handles polling jitter. A missed run can be caught up once.
                if now - scheduled > 60 and config.get('missed', 'skip') == 'skip':
                    continue
                tid = 'auto_' + hashlib.sha256(f'{ident}:{scheduled}'.encode()).hexdigest()[:32]
                if db.execute('SELECT 1 FROM tasks WHERE task_id=?', (tid,)).fetchone():
                    continue
                stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
                conversation = config.get('conversation_id') or uuid.uuid4().hex
                delivery = {}
                if config.get('channel_link'):
                    link = db.execute("SELECT channel,sender,chat,conversation FROM channel_links WHERE id=? AND state='active'", (config['channel_link'],)).fetchone()
                    if not link or link[3] != conversation:
                        db.execute('UPDATE automations SET enabled=0,revision=revision+1 WHERE id=?', (ident,))
                        continue
                    if any(t.get('conversation_id') == conversation for t in pending):
                        continue
                    delivery = dict(channel_link=config['channel_link'], sender_id=link[1], chat_id=link[2], delivery_channel=link[0])
                else:
                    db.execute('INSERT INTO conversations(id,owner,title,created_at,local_only) VALUES(?,?,?,?,1)',
                               (conversation, 'local_owner', config['title'], stamp))
                    if config.get('project_id'):
                        db.execute('INSERT INTO conversation_projects VALUES(?,?)', (conversation, config['project_id']))
                task = dict(task_id=tid, message=config['prompt'], source='desktop', chat_id='desktop:local_owner',
                            user_id='local_owner', conversation_id=conversation, automation_id=ident,
                            scheduled_at=scheduled, status='queued', timestamp=stamp, result=None,
                            allow_tools=False, model_target='qwen', use_knowledge=True)
                task.update(delivery)
                if config.get('project_id'):
                    task['project_id'] = config['project_id']
                record_lifecycle(task)
                self.store.write(db, task)
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (tid, conversation, 'user', task['message'], stamp))
                created.append(tid)
                pending.append(task)
        return created
