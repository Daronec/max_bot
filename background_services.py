"""One coordinator for legacy jobs, with durable delivery through the common outbox."""
import hashlib
import threading
import time

from task_service import record_lifecycle, timestamp


class BackgroundSettings:
    def __init__(self,store):self.store=store

    def get(self):
        from config import SUMMARY_ENABLED,MOOD_ENABLED,NOTIFICATIONS_ENABLED
        defaults=dict(summary=SUMMARY_ENABLED,mood=MOOD_ENABLED,notifications=NOTIFICATIONS_ENABLED)
        with self.store.connect() as db:
            row=db.execute("SELECT value FROM metadata WHERE key='background_settings'").fetchone()
        import json
        saved=json.loads(row[0]) if row else dict(version=0,enabled={})
        return dict(version=saved['version'],enabled={**defaults,**saved['enabled']})

    def update(self,name,enabled,version):
        import json
        from task_service import TaskConflict
        if name not in ('summary','mood','notifications') or not isinstance(enabled,bool) or not isinstance(version,int) or isinstance(version,bool):raise ValueError('Некорректные настройки фонового сервиса')
        with self.store.connect() as db,self.store.transaction(db):
            row=db.execute("SELECT value FROM metadata WHERE key='background_settings'").fetchone()
            current=json.loads(row[0]) if row else dict(version=0,enabled={})
            if current['version']!=version:raise TaskConflict('Background settings changed')
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('background_settings_version:'+str(version),json.dumps(current)))
            current=dict(version=version+1,enabled={**current['enabled'],name:enabled})
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('background_settings',json.dumps(current)))
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
