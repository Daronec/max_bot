"""Explicit channel pairing. Codes prove possession; the panel grants access separately."""
import hashlib
import json
import secrets
import time


class IdentityService:
    def __init__(self, store):
        self.store = store
        with store.connect() as db, store.transaction(db):
            db.execute('''CREATE TABLE IF NOT EXISTS channel_links (
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL,
                chat TEXT NOT NULL, state TEXT NOT NULL, conversation TEXT,
                created REAL NOT NULL, UNIQUE(channel,sender,chat))''')
            db.execute('''CREATE TABLE IF NOT EXISTS pairing_codes (
                digest TEXT PRIMARY KEY, channel TEXT NOT NULL, expires REAL NOT NULL)''')
            if 'private_verified' not in {row[1] for row in db.execute('PRAGMA table_info(channel_links)')}:
                db.execute('ALTER TABLE channel_links ADD COLUMN private_verified INTEGER NOT NULL DEFAULT 0')
                db.execute("UPDATE channel_links SET state='revoked',conversation=NULL WHERE channel='max'")

    def issue(self, channel):
        if channel not in ('telegram', 'max'):
            raise ValueError('Неизвестный канал')
        code = secrets.token_urlsafe(24)
        with self.store.connect() as db, self.store.transaction(db):
            db.execute('DELETE FROM pairing_codes WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO pairing_codes VALUES(?,?,?)',
                       (hashlib.sha256(code.encode()).hexdigest(), channel, time.time() + 600))
        return dict(code=code, expires_in=600, command='/link ' + code)

    def links(self):
        with self.store.connect() as db:
            return [dict(id=r[0], channel=r[1], sender=r[2], chat=r[3], state=r[4], conversation_id=r[5],
                         shared_memory=r[4]=='active' and bool(r[7]))
                    for r in db.execute('SELECT * FROM channel_links ORDER BY created DESC')]

    def update(self, ident, action, conversation=None):
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute('SELECT state FROM channel_links WHERE id=?', (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            if action == 'confirm':
                if row[0] != 'pending':
                    raise ValueError('Подтвердить можно только ожидающую привязку')
                db.execute("UPDATE channel_links SET state='active' WHERE id=?", (ident,))
            elif action == 'revoke':
                db.execute("UPDATE channel_links SET state='revoked',conversation=NULL WHERE id=?", (ident,))
            elif action == 'conversation':
                if row[0] != 'active':
                    raise ValueError('Привязка не подтверждена')
                if conversation is not None and not db.execute(
                        "SELECT 1 FROM conversations WHERE id=? AND owner='local_owner'", (conversation,)).fetchone():
                    raise KeyError(conversation)
                db.execute('UPDATE channel_links SET conversation=? WHERE id=?', (conversation, ident))
            else:
                raise ValueError('Неизвестное действие')

    def command(self, task):
        channel, sender, chat = task.get('source'), task.get('sender_id'), str(task.get('chat_id'))
        if channel not in ('telegram', 'max') or not sender or task.get('memory_excluded'):
            return None
        text = task['message'].strip()
        if text == '/unlink':
            with self.store.connect() as db:
                db.execute("UPDATE channel_links SET state='revoked',conversation=NULL WHERE channel=? AND sender=? AND chat=?",
                           (channel, str(sender), chat))
            return 'Привязка отозвана. История панели и общая личная память больше недоступны в этом чате.'
        if not text.startswith('/link '):
            return None
        digest = hashlib.sha256(text[6:].strip().encode()).hexdigest()
        ident = secrets.token_hex(16)
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute('SELECT channel,expires FROM pairing_codes WHERE digest=?', (digest,)).fetchone()
            if not row or row[0] != channel or row[1] < time.time():
                return 'Код недействителен или истек. Создайте новый код в панели.'
            if channel == 'max' and task.get('chat_type') != 'dialog':
                return 'Привязка MAX доступна только в личном диалоге с ботом.'
            db.execute('DELETE FROM pairing_codes WHERE digest=?', (digest,))
            db.execute('''INSERT INTO channel_links(id,channel,sender,chat,state,conversation,created,private_verified) VALUES(?,?,?,?,?,?,?,1)
                ON CONFLICT(channel,sender,chat) DO UPDATE SET id=excluded.id,state='pending',conversation=NULL,created=excluded.created,private_verified=1''',
                       (ident, channel, str(sender), chat, 'pending', None, time.time()))
        return 'Запрос привязки получен. Подтвердите отправителя и чат в панели, чтобы открыть общую личную память. Диалог можно выбрать отдельно.'

    def memory_link(self, task):
        """Return the current private owner grant, independent of a history binding."""
        channel = task.get('source')
        expected_type = {'max': 'dialog', 'telegram': 'private'}.get(channel)
        if (not expected_type or task.get('chat_type') != expected_type
                or not task.get('sender_id') or task.get('memory_excluded')):
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM channel_links WHERE channel=? AND sender=? AND chat=? AND state='active' AND private_verified=1",
                             (channel, str(task['sender_id']), str(task['chat_id']))).fetchone()
        return row[0] if row else None

    def attach(self, task):
        """Bind one durable turn, only after current access and ownership checks."""
        if task.get('source') not in ('telegram', 'max'):
            return False
        if task['source'] == 'max' and task.get('chat_type') != 'dialog':
            if task.get('channel_link'):
                raise PermissionError('Связанный диалог MAX должен быть личным')
            return False
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute("SELECT id,conversation FROM channel_links WHERE channel=? AND sender=? AND chat=? AND state='active'",
                             (task['source'], str(task.get('sender_id')), str(task['chat_id']))).fetchone()
            if not row or not row[1]:
                if task.get('channel_link'):
                    raise PermissionError('Привязка или доступ к диалогу отозваны')
                return False
            if task.get('channel_link') and task['channel_link'] != row[0]:
                raise PermissionError('Привязка была заменена; прежний запуск остановлен')
            if task.get('conversation_id') and task['conversation_id'] != row[1]:
                raise PermissionError('Диалог привязки изменен; прежний запуск остановлен')
            if not db.execute("SELECT 1 FROM conversations WHERE id=? AND owner='local_owner'", (row[1],)).fetchone():
                raise PermissionError('Диалог недоступен')
            # One worker serializes external turns. Desktop admission sees the same task metadata.
            others = db.execute("SELECT task_id,payload FROM tasks WHERE status IN ('queued','processing') AND task_id<>?", (task['task_id'],)).fetchall()
            if any(json.loads(r[1]).get('conversation_id') == row[1] for r in others):
                raise ValueError('В диалоге уже выполняется другой запрос')
            task.update(channel_link=row[0], conversation_id=row[1], user_id='local_owner', use_knowledge=True)
            revision = self.store.write(db, task)
            db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)',
                       (task['task_id'], row[1], 'user', task['message'], task['timestamp']))
            # Shared history is always private, including future desktop turns.
            db.execute('UPDATE conversations SET local_only=1 WHERE id=?', (row[1],))
        task['_revision'] = revision
        return True

    def delivery_allowed(self, task):
        if task.get('memory_link') and self.memory_link(task) != task['memory_link']:
            return False
        if not task.get('channel_link'):
            return True
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM channel_links WHERE id=? AND state='active' AND conversation=? AND sender=? AND chat=? AND (channel!='max' OR private_verified=1)",
                                   (task['channel_link'], task['conversation_id'], str(task.get('sender_id')), str(task['chat_id']))).fetchone())
