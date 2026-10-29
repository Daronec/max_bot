"""Daily mood check-in with persisted questions and idempotent answer association."""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import threading

from bot_client import message_id
from config import BASE_DIR, MOOD_ENABLED, MOOD_TIME, MOOD_CHAT_ID, FS_ALLOWED_CHAT_IDS

QUESTION = 'Как у тебя настроение?'


class MoodCheckin:
    def __init__(self, client, state=None):
        self.client=client
        self.state=Path(state) if state else BASE_DIR/'mood'/'state.sqlite3'
        self.active=False
        self.error=None
        self.thread=None
        self._lock=threading.RLock()

    def db(self):
        self.state.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        db=sqlite3.connect(self.state,timeout=5)
        db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS questions(day TEXT PRIMARY KEY,chat TEXT,mid TEXT,
                sent_at REAL,answer_mid TEXT);''')
        return db

    def status(self):
        return {'enabled':MOOD_ENABLED,'active':self.active,'time':MOOD_TIME,'error':self.error}

    def tick(self,current=None):
        if str(MOOD_CHAT_ID) not in FS_ALLOWED_CHAT_IDS:
            raise ValueError('Чат настроения не разрешён')
        current=current or datetime.now().astimezone()
        day=current.date().isoformat()
        scheduled=datetime.strptime(MOOD_TIME,'%H:%M').time()
        with self._lock, closing(self.db()) as db:
            if not db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
                db.execute("INSERT INTO meta VALUES('initialized','1')")
                if current.time()>=scheduled:
                    db.execute('INSERT OR IGNORE INTO questions(day,chat) VALUES(?,?)',(day,str(MOOD_CHAT_ID)))
                db.commit()
            if current.time()<scheduled: return
            if db.execute('SELECT 1 FROM questions WHERE day=?',(day,)).fetchone(): return
            response=self.client.send_message(MOOD_CHAT_ID,QUESTION)
            mid=message_id((response or {}).get('message',{}))
            if not mid: raise RuntimeError('MAX не подтвердил вопрос о настроении')
            db.execute('INSERT INTO questions(day,chat,mid,sent_at) VALUES(?,?,?,?)',
                       (day,str(MOOD_CHAT_ID),mid,current.timestamp()))
            db.commit()
        self.error=None

    def match_answer(self, chat, mid, reply_mid=None, is_command=False, current=None, message_time=None):
        if not MOOD_ENABLED or str(chat)!=str(MOOD_CHAT_ID) or str(chat) not in FS_ALLOWED_CHAT_IDS:
            return None
        current=current or datetime.now().astimezone()
        with self._lock, closing(self.db()) as db:
            previous=db.execute('SELECT mid FROM questions WHERE chat=? AND answer_mid=?',(str(chat),mid)).fetchone()
            if previous: return previous[0]
            rows=db.execute('SELECT day,mid,sent_at FROM questions WHERE chat=? AND mid IS NOT NULL AND answer_mid IS NULL AND sent_at>=? ORDER BY sent_at DESC',
                            (str(chat),(current-timedelta(days=1)).timestamp())).fetchall()
            for day,question_mid,sent_at in rows:
                if message_time is not None and message_time < sent_at: continue
                if reply_mid and str(reply_mid)!=question_mid: continue
                if is_command and not reply_mid: continue
                db.execute('UPDATE questions SET answer_mid=? WHERE day=?',(mid,day))
                db.commit()
                return question_mid
        return None

    def run(self,stop):
        self.active=True
        try:
            while not stop.is_set():
                try: self.tick()
                except Exception: self.error='Не удалось отправить вопрос о настроении; повторю позже.'
                stop.wait(30)
        finally: self.active=False

    def start(self,stop):
        if not MOOD_ENABLED:return
        with self._lock:
            if self.thread and self.thread.is_alive():return
            self.thread=threading.Thread(target=self.run,args=(stop,),daemon=True,name='mood-checkin')
            self.thread.start()
