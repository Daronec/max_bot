"""SQLite persistence for personal facts and processed events."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading

from assistant_app.config import MEMORY_DB_PATH


class MemoryStore:
    def __init__(self, path=MEMORY_DB_PATH):
        self.path = Path(path)
        self.lock = threading.RLock()

    @contextmanager
    def connect(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            descriptor=os.open(self.path,os.O_CREAT | os.O_APPEND | os.O_WRONLY,0o600)
            os.close(descriptor)
            self.path.chmod(0o600)
            connection=sqlite3.connect(self.path,timeout=5)
            connection.row_factory=sqlite3.Row
            try:
                connection.execute('PRAGMA journal_mode=DELETE')
                connection.executescript('''
                CREATE TABLE IF NOT EXISTS facts(
                    scope TEXT NOT NULL, slot TEXT NOT NULL, category TEXT NOT NULL,
                    evidence TEXT NOT NULL, source_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope,slot));
                CREATE TABLE IF NOT EXISTS events(
                    scope TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(scope,event_id));
                ''')
                with connection:yield connection
            finally:connection.close()

    def processed(self, scope, event_id):
        with self.connect() as db:
            return db.execute('SELECT 1 FROM events WHERE scope=? AND event_id=?',(scope,event_id)).fetchone() is not None

    def save(self, scope, event_id, facts):
        stamp=datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            if db.execute('SELECT 1 FROM events WHERE scope=? AND event_id=?',(scope,event_id)).fetchone():return 0
            for fact in facts:
                db.execute('''INSERT INTO facts VALUES(?,?,?,?,?,?)
                    ON CONFLICT(scope,slot) DO UPDATE SET category=excluded.category,
                    evidence=excluded.evidence,source_id=excluded.source_id,updated_at=excluded.updated_at''',
                    (scope,fact.slot,fact.category,fact.evidence,event_id,stamp))
            db.execute('INSERT INTO events VALUES(?,?)',(scope,event_id))
        return len(facts)

    def list(self, scope, limit=300):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT slot,category,evidence,source_id,updated_at FROM facts WHERE scope=? ORDER BY updated_at DESC LIMIT ?',(scope, limit))]

    def forget(self, scope, query):
        all_facts=self.list(scope,limit=-1)
        categories={'семью':'family','семья':'family','профессию':'profession','работу':'work',
                    'предпочтения':'preferences','здоровье':'health','финансы':'finance'}
        query=query.casefold().strip()
        selected=[f for f in all_facts if query in {'всё','все','всё обо мне','все обо мне'} or
                  f['category']==categories.get(query) or query in f['evidence'].casefold() or query==f['slot']]
        with self.connect() as db:
            for fact in selected:db.execute('DELETE FROM facts WHERE scope=? AND slot=?',(scope,fact['slot']))
        return len(selected)
