"""Read selected macOS notifications without modifying Apple's database or using inference.

Apple's database is private and version-dependent. Fail visibly on schema/access errors.
Delivery is at-least-once: a crash after MAX accepts a message but before local commit
can repeat that chunk. Normal polling/restarts and partial deliveries are deduplicated.
"""
import hashlib
import json
from pathlib import Path
import plistlib
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone

from bot_client import message_id
from config import (BASE_DIR, NOTIFICATIONS_DB, NOTIFICATIONS_ENABLED,
                    NOTIFICATIONS_CHAT_ID, NOTIFICATIONS_POLL_SECONDS, FS_ALLOWED_CHAT_IDS)

APPS = {
    'com.apple.mail': 'Почта',
    'ru.keepcoder.Telegram': 'Telegram',
    'org.telegram.desktop': 'Telegram',
    'com.tdesktop.Telegram': 'Telegram',
    'com.apple.iCal': 'Календарь',
    'com.apple.CalendarNotification.CalNCService': 'Календарь',
    'com.apple.reminders': 'Напоминания',
    'com.apple.reminders.RemindersNC': 'Напоминания',
}
APPLE_EPOCH = 978307200


def notification_text(app, payload, delivered):
    data = plistlib.loads(payload)
    request = data.get('req')
    if not isinstance(request, dict):
        raise ValueError('Unsupported notification payload')
    parts = []
    for key in ('titl', 'subt', 'body'):
        value = request.get(key, '')
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            value = '\n'.join(value)
        if not isinstance(value, str):
            raise ValueError('Unsupported notification text field')
        if value:
            parts.append(value)
    stamp = datetime.fromtimestamp(APPLE_EPOCH + delivered, timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')
    return f'{APPS[app]} · {stamp}\n' + ('\n'.join(parts) or '(Приложение не передало текст уведомления)')


class NotificationForwarder:
    def __init__(self, client, source=NOTIFICATIONS_DB, state=None, chat=NOTIFICATIONS_CHAT_ID):
        self.client = client
        self.source = Path(source)
        self.state = Path(state) if state else BASE_DIR / 'notifications' / 'state.sqlite3'
        self.chat = str(chat)
        self.active = False
        self.error = None
        self.thread = None
        self.last_scan = None
        self.sent = 0
        self.unsupported = 0
        self._lock = threading.Lock()

    def status(self):
        return {'enabled': NOTIFICATIONS_ENABLED, 'active': self.active, 'error': self.error,
                'last_scan': self.last_scan, 'sent_since_start': self.sent,
                'unsupported_records': self.unsupported}

    def _rows(self):
        # mode=ro preserves live WAL visibility; immutable=1 would miss live notifications.
        with closing(sqlite3.connect(self.source.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            marks = ','.join('?' for _ in APPS)
            return db.execute(f'''SELECT app.identifier, record.uuid, record.data, record.delivered_date
                FROM record JOIN app USING(app_id)
                WHERE app.identifier IN ({marks}) AND record.delivered_date > 0
                ORDER BY record.delivered_date, record.rec_id''', tuple(APPS)).fetchall()

    def tick(self):
        if self.chat not in FS_ALLOWED_CHAT_IDS:
            raise ValueError('Чат пересылки не входит в список разрешённых чатов')
        rows = self._rows()
        self.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.state)) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY, key TEXT UNIQUE,
                    chat TEXT NOT NULL, chunks TEXT NOT NULL, next_chunk INTEGER NOT NULL DEFAULT 0);''')
            initialized = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()
            for app, uid, payload, delivered in rows:
                # Track deliveries/updates by stable UUID, delivery timestamp and payload hash.
                uid_bytes = uid.encode() if isinstance(uid, str) else bytes(uid or b'')
                key = hashlib.sha256(app.encode() + uid_bytes + str(delivered).encode() + payload).hexdigest()
                if db.execute('SELECT 1 FROM seen WHERE key=?', (key,)).fetchone():
                    continue
                if initialized:
                    try:
                        text = notification_text(app, payload, delivered)
                    except (ValueError, TypeError, plistlib.InvalidFileException, OverflowError):
                        count = db.execute("SELECT value FROM meta WHERE key='unsupported'").fetchone()
                        db.execute("INSERT OR REPLACE INTO meta VALUES('unsupported',?)", (str(int(count[0]) + 1 if count else 1),))
                        db.execute('INSERT INTO seen(key) VALUES(?)', (key,))
                        continue
                    chunks = [text[i:i+3500] for i in range(0, len(text), 3500)]
                    db.execute('INSERT OR IGNORE INTO pending(key,chat,chunks) VALUES(?,?,?)',
                               (key, self.chat, json.dumps(chunks, ensure_ascii=False)))
                db.execute('INSERT INTO seen(key) VALUES(?)', (key,))
            db.execute("INSERT OR REPLACE INTO meta VALUES('initialized','1')")
            db.commit()
            count = db.execute("SELECT value FROM meta WHERE key='unsupported'").fetchone()
            self.unsupported = int(count[0]) if count else 0
            self.last_scan = datetime.now(timezone.utc).isoformat()
            pending = db.execute('SELECT id,chat,chunks,next_chunk FROM pending ORDER BY id LIMIT 20').fetchall()
            for ident, chat, encoded, offset in pending:
                if chat != self.chat:
                    raise ValueError('Изменился чат пересылки: проверьте сохранённую очередь')
                chunks = json.loads(encoded)
                for index in range(offset, len(chunks)):
                    response = self.client.send_message(chat, chunks[index])
                    if not response or not message_id(response.get('message', {})):
                        raise RuntimeError('MAX не подтвердил доставку уведомления; будет повторная попытка')
                    db.execute('UPDATE pending SET next_chunk=? WHERE id=?', (index+1, ident))
                    db.commit()
                db.execute('DELETE FROM pending WHERE id=?', (ident,))
                db.commit()
                self.sent += 1
        self.error = None

    def run(self, stop):
        self.active = True
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except sqlite3.Error:
                    self.error = 'Не удалось прочитать базу уведомлений или очередь. Проверьте доступ macOS и схему базы.'
                except (OSError, ValueError, TypeError, plistlib.InvalidFileException, OverflowError):
                    self.error = 'Не удалось прочитать уведомление: доступ запрещён или формат macOS не поддерживается.'
                except RuntimeError as error:
                    self.error = str(error)
                stop.wait(NOTIFICATIONS_POLL_SECONDS)
        finally:
            self.active = False

    def start(self, stop):
        if not NOTIFICATIONS_ENABLED:
            return
        with self._lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.run, args=(stop,), daemon=True, name='notifications')
            self.thread.start()
