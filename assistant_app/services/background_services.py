"""One coordinator for legacy jobs, with durable delivery through the common outbox."""
import hashlib
import json
import threading
import time

from assistant_app.core.task_service import TaskConflict, record_lifecycle, timestamp


SERVICE_NAMES = ('summary', 'mood', 'notifications')


class BackgroundSettings:
    def __init__(self,store):self.store=store

    def get(self):
        from assistant_app.config import SUMMARY_ENABLED,MOOD_ENABLED,NOTIFICATIONS_ENABLED
        defaults=dict(summary=SUMMARY_ENABLED,mood=MOOD_ENABLED,notifications=NOTIFICATIONS_ENABLED)
        with self.store.connect() as db:
            row=db.execute("SELECT value FROM metadata WHERE key='background_settings'").fetchone()
        saved=json.loads(row[0]) if row else dict(version=0,enabled={})
        deleted = saved.get('deleted', [])
        enabled = {**defaults, **saved['enabled']}
        for name in deleted:
            enabled[name] = False
        return dict(version=saved['version'], enabled=enabled, deleted=deleted)

    def update(self,name,enabled,version):
        if not isinstance(enabled, bool):
            raise ValueError('Укажите, включён ли фоновый сервис.')
        return self._change(name, enabled, version)

    def delete(self, name, version):
        return self._change(name, None, version)

    def _change(self, name, enabled, version):
        if name not in SERVICE_NAMES or type(version) is not int:
            raise ValueError('Некорректные настройки фонового сервиса')
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute("SELECT value FROM metadata WHERE key='background_settings'").fetchone()
            current = json.loads(row[0]) if row else dict(version=0, enabled={})
            if current['version'] != version:
                raise TaskConflict('Background settings changed')
            deleted = current.get('deleted', [])
            if name in deleted:
                raise ValueError('Фоновый сервис уже удалён.')
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',
                       ('background_settings_version:' + str(version), json.dumps(current)))
            if enabled is None:
                deleted = [*deleted, name]
            current = dict(version=version + 1, enabled={**current['enabled'], name: bool(enabled)}, deleted=deleted)
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('background_settings', json.dumps(current)))
        return self.get()


class OutboxClient:
    durable_outbox=True

    def __init__(self,client,store):self.client=client;self.store=store

    def __getattr__(self,name):return getattr(self.client,name)

    def send_outbox(self,chat,text,*,key,purpose,day=None):
        tid='scheduled_'+hashlib.sha256(f'{purpose}:{chat}:{key}'.encode()).hexdigest()[:32]
        with self.store.connect() as db,self.store.transaction(db):
            if not db.execute('SELECT 1 FROM tasks WHERE task_id=?',(tid,)).fetchone():
                task=dict(task_id=tid,source='max',chat_id=chat,status='completed',timestamp=timestamp(),completed_at=timestamp(),
                          result=text,message='Фоновая доставка: '+purpose,delivered=False,
                          kind='scheduled_delivery',background_purpose=purpose,background_day=day)
                record_lifecycle(task);self.store.write(db,task)
        # Receipt means durably queued, not remotely delivered. UI exposes real delivery state.
        return {'message':{'id':'outbox:'+tid}}


class BackgroundCoordinator:
    def __init__(self,services,settings=None):
        self.services=services;self.thread=None;self.errors={};self.settings=settings

    def start(self,stop):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.run,args=(stop,),daemon=True,name='background-coordinator');self.thread.start()

    def run(self,stop):
        due={name:0 for name,_,_ in self.services}
        for _,service,_ in self.services:service.active=True
        try:
            while not stop.is_set():
                for name,service,interval in self.services:
                    if stop.is_set():break
                    enabled=self.settings.get()['enabled'].get(name,False) if self.settings else True
                    service.active=enabled
                    if not enabled:continue
                    if time.monotonic()<due[name]:continue
                    try:service.tick();service.error=None
                    except Exception:service.error='Фоновая задача не завершена; проверьте доступ к данным и очередь доставки.'
                    due[name]=time.monotonic()+interval
                stop.wait(1)
        finally:
            for _,service,_ in self.services:service.active=False
