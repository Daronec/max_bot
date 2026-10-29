"""Local daily agenda, overdue reminders and selected notifications; no LLM inference."""
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import threading

from bot_client import message_id
from config import (BASE_DIR, SUMMARY_ENABLED, SUMMARY_TIME, SUMMARY_CHAT_ID,
                    SUMMARY_IMPORTANT_WORDS, FS_ALLOWED_CHAT_IDS)
from notification_forwarder import NotificationForwarder, notification_text, APPLE_EPOCH


def read_agenda(mode=None):
    helper = BASE_DIR / 'helpers' / 'agenda'
    try:
        output = subprocess.run([str(helper)] + ([mode] if mode else []),stdin=subprocess.DEVNULL,capture_output=True,
                                text=True,timeout=150,check=True)
        data = json.loads(output.stdout)
        if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in ('events','reminders','errors')):
            raise ValueError()
        return data
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'events': [], 'reminders': [], 'errors': [
            'Календарь и Напоминания недоступны: проверьте разрешения macOS и helpers/agenda.']}


def build_summary(client=None, current=None):
    current = current or datetime.now().astimezone()
    agenda = read_agenda()
    lines = ['Утренняя сводка · ' + current.strftime('%d.%m.%Y')]
    def section(title, items, empty, limit=850):
        lines.append('\n' + title)
        used = 0
        for index, item in enumerate(items):
            item = item.replace('\r',' ').replace('\n',' ')[:260]
            if used + len(item) > limit:
                lines.append(f'… ещё {len(items)-index}')
                break
            lines.append('• ' + item)
            used += len(item) + 3
        if not items: lines.append(empty)
    events = []
    for event in agenda['events']:
        try:
            stamp = 'Весь день' if event.get('all_day') else datetime.fromisoformat(event['start'].replace('Z','+00:00')).astimezone().strftime('%H:%M')
            events.append(stamp + ' — ' + event['title'])
        except (KeyError,ValueError,TypeError):
            agenda['errors'].append('Не удалось разобрать одно событие календаря.')
    section('Календарь на сегодня',events,'Нет событий.' if not agenda['errors'] else 'Данные могут быть недоступны.')
    reminders = []
    for reminder in agenda['reminders']:
        try:
            due = datetime.fromisoformat(reminder['due'].replace('Z','+00:00')).astimezone().strftime('%d.%m %H:%M')
            reminders.append(due + ' — ' + reminder['title'])
        except (KeyError,ValueError,TypeError):
            agenda['errors'].append('Не удалось разобрать одно напоминание.')
    section('Просроченные напоминания',reminders,'Нет просроченных.' if not agenda['errors'] else 'Данные могут быть недоступны.')
    notes = []
    try:
        rows = NotificationForwarder(client)._rows()
        cutoff = (current-timedelta(hours=24)).timestamp()
        for app, uid, payload, delivered in reversed(rows):
            if not cutoff <= APPLE_EPOCH + delivered <= current.timestamp(): continue
            text = notification_text(app,payload,delivered)
            if SUMMARY_IMPORTANT_WORDS and not any(word in text.casefold() for word in SUMMARY_IMPORTANT_WORDS): continue
            notes.append(text)
    except (sqlite3.Error,OSError,ValueError,TypeError):
        agenda['errors'].append('Уведомления недоступны или формат записи изменился.')
    section('Уведомления за 24 часа',notes,'Нет уведомлений по выбранному фильтру.',limit=1150)
    if agenda['errors']:
        lines.append('\nНедоступные данные: ' + '; '.join(dict.fromkeys(agenda['errors'])))
    return '\n'.join(lines)[:3900]


class MorningSummary:
    def __init__(self,client,state=None):
        self.client=client
        self.state=Path(state) if state else BASE_DIR/'summaries'/'state.sqlite3'
        self.active=False
        self.error=None
        self.thread=None
        self.last_sent=None
        self._lock=threading.Lock()

    def status(self):
        return {'enabled':SUMMARY_ENABLED,'time':SUMMARY_TIME,'active':self.active,'error':self.error,'last_sent':self.last_sent}

    def tick(self,current=None):
        if str(SUMMARY_CHAT_ID) not in FS_ALLOWED_CHAT_IDS:
            raise ValueError('Получатель сводки не входит в список разрешённых чатов')
        current=current or datetime.now().astimezone()
        scheduled=datetime.strptime(SUMMARY_TIME,'%H:%M').time()
        day=current.date().isoformat()
        self.state.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with closing(sqlite3.connect(self.state)) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY,chat TEXT,text TEXT,sent INTEGER DEFAULT 0);')
            if not db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
                # First installation after the morning hour starts tomorrow, not immediately.
                db.execute("INSERT INTO meta VALUES('initialized','1')")
                if current.timetz().replace(tzinfo=None)>=scheduled:
                    db.execute('INSERT OR IGNORE INTO daily(day,chat,text,sent) VALUES(?,?,?,1)',(day,str(SUMMARY_CHAT_ID),'Initial baseline'))
                db.commit()
            if current.timetz().replace(tzinfo=None)<scheduled: return
            row=db.execute('SELECT chat,text,sent FROM daily WHERE day=?',(day,)).fetchone()
            if row and row[2]: return
            if row is None:
                text=build_summary(self.client,current)
                db.execute('INSERT INTO daily(day,chat,text) VALUES(?,?,?)',(day,str(SUMMARY_CHAT_ID),text))
                db.commit()
                row=(str(SUMMARY_CHAT_ID),text,0)
            if row[0]!=str(SUMMARY_CHAT_ID): raise ValueError('Получатель сохранённой сводки изменился')
            response=self.client.send_message(row[0],row[1])
            if not response or not message_id(response.get('message',{})):
                raise RuntimeError('MAX не подтвердил сводку; доставка будет повторена')
            db.execute('UPDATE daily SET sent=1 WHERE day=?',(day,))
            db.commit()
            self.last_sent=day
        self.error=None

    def run(self,stop):
        self.active=True
        try:
            while not stop.is_set():
                try: self.tick()
                except Exception:
                    self.error='Не удалось подготовить или доставить сводку; проверьте доступ к данным и MAX.'
                stop.wait(30)
        finally: self.active=False

    def start(self,stop):
        if not SUMMARY_ENABLED: return
        with self._lock:
            if self.thread and self.thread.is_alive(): return
            self.thread=threading.Thread(target=self.run,args=(stop,),daemon=True,name='morning-summary')
            self.thread.start()
