"""Daily local diagnostics and bounded recovery, independent of the API process."""
from datetime import datetime
import json
import logging
import time
from zoneinfo import ZoneInfo

import requests

TIMEZONE = 'Europe/Moscow'
ZONE = ZoneInfo(TIMEZONE)
HOUR = 4
SCHEDULE = f'{HOUR:02d}:00 {TIMEZONE}'
LOGGER = logging.getLogger(__name__)
STATE = 'daily_maintenance'
OFFLINE = ('нет соединения с сервером модели', 'нет сервера lm studio',
           'локальный сервер модели ещё недоступен', 'не найден lms для автозапуска')
REFUSALS = ('не могу выполнить', 'не удалось выполнить', 'не смог выполнить',
            'не смогла выполнить', 'выполнить не удалось')


def findings(store, now):
    """The dialogue is only evidence. It cannot supply commands, URLs or policy."""
    found = []
    with store.connect() as db:
        rows = db.execute("""
            SELECT payload FROM tasks WHERE status IN ('completed','failed','queued')
            AND julianday(COALESCE(json_extract(payload,'$.completed_at'),
                json_extract(payload,'$.events[#-1].timestamp'),json_extract(payload,'$.timestamp')))
                BETWEEN julianday(?, 'unixepoch') AND julianday(?, 'unixepoch')
            ORDER BY rowid DESC
        """, (now-86400, now))
        for row in rows:
            task = json.loads(row[0])
            completed = task['status'] == 'completed'
            error = str(task.get('last_processing_error') or task.get('error') or '').casefold()
            answer = str(task.get('result') or '').casefold()[:1600]
            target = (task.get('routing') or {}).get('target')
            if not completed and target in (None, 'qwen', 'local') and any(pattern in error for pattern in OFFLINE):
                found.append(dict(task_id=task['task_id'], kind='local_model_offline'))
            elif task.get('delivery_uncertain') or (not completed and (error or task['status']=='failed')) or any(pattern in answer for pattern in REFUSALS):
                found.append(dict(task_id=task['task_id'], kind='review'))
    return found


def tick(store, manager, control_lock, api_url, recover, *, now=None, clock=time.monotonic, pause=time.sleep, stop=None):
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, ZONE)
    day = local.date().isoformat()
    if local.hour < HOUR or (store.runtime(STATE) or {}).get('day') == day:
        return
    with control_lock:
        if (store.runtime(STATE) or {}).get('day') == day:
            return
        if not manager.status()['running']:
            return  # Respect a manual stop.
        with store.connect() as db:
            if db.execute("SELECT 1 FROM tasks WHERE status IN ('processing','sending') OR json_extract(payload,'$.delivery_inflight')=1 LIMIT 1").fetchone():
                return
        issues = findings(store, now)
        result = dict(day=day, issues=issues[:100], total=len(issues), action='review' if issues else 'none')
        store.set_runtime(STATE,{**result,'action':'started'})
        if any(issue['kind']=='local_model_offline' for issue in issues):
            try:
                repaired = recover()
            except Exception:
                repaired = False
            if repaired == 'already_healthy':
                result['action']='already_healthy'
            elif repaired and not (stop and stop.is_set()):
                store.set_runtime('maintenance_pause',{'until':time.time()+45})
                try:
                    with store.connect() as db:
                        busy = db.execute("SELECT 1 FROM tasks WHERE status IN ('processing','sending') OR json_extract(payload,'$.delivery_inflight')=1 LIMIT 1").fetchone()
                    if busy:
                        result['action']='model_recovered_busy'
                    else:
                        result['action']=restart_and_probe(manager,api_url,clock,pause,stop)
                finally:
                    store.set_runtime('maintenance_pause',{'until':0})
            else:
                result['action']='recovery_unavailable'
        store.set_runtime(STATE,result)
        return result


def restart_and_probe(manager,api_url,clock,pause,stop):
    try:
        if stop and stop.is_set():return 'interrupted'
        manager.stop()
        if stop and stop.is_set():return 'interrupted'
        manager.start()
        deadline = clock()+15
        while clock()<deadline:
            if stop and stop.is_set():return 'interrupted'
            try:
                probe = requests.get(api_url+'/healthz', timeout=2, allow_redirects=False)
                if probe.ok and probe.json().get('ready') is True:return 'restarted_healthy'
            except (requests.RequestException,ValueError):pass
            pause(.5)
        return 'restart_unverified'
    except (OSError,RuntimeError):
        return 'restart_failed'


def run(store, manager, control_lock, api_url, recover, stop):
    while not stop.wait(30):
        try:
            tick(store,manager,control_lock,api_url,recover,stop=stop)
        except Exception:
            LOGGER.exception('Daily maintenance failed')
