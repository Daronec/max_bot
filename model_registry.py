"""Versioned OpenAI-compatible model catalogue. Credentials stay in the environment."""
import json
import math
import os
import re
from urllib.parse import urlsplit

from task_service import TaskConflict


class ModelPolicyError(RuntimeError):
    pass


class ModelRegistry:
    def __init__(self, store):
        self.store = store

    def get(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='model_registry'").fetchone()
        return json.loads(row[0]) if row else dict(version=0, models=[])

    @staticmethod
    def validate(models):
        if not isinstance(models, list) or len(models) > 50:
            raise ValueError('Не более 50 моделей')
        seen = set()
        allowed = {'id','model','base_url','local','key_env','context_tokens','output_tokens','input_price','output_price','daily_budget','tools','enabled','intents','priority','timeout_seconds','cooldown_seconds'}
        result = []
        for model in models:
            if not isinstance(model, dict) or set(model) - allowed:
                raise ValueError('Некорректная модель')
            for key in ('id','model','base_url'):
                if not isinstance(model.get(key), str) or not model[key].strip() or len(model[key]) > 500 or any(ord(c) < 32 for c in model[key]):
                    raise ValueError('Укажите id, model и base_url')
            if not re.fullmatch('[a-zA-Z0-9_-]{1,60}', model['id']) or model['id'] in seen:
                raise ValueError('Идентификаторы моделей должны быть уникальны')
            seen.add(model['id'])
            url = urlsplit(model['base_url'])
            local = url.hostname in ('localhost', '127.0.0.1', '::1')
            if (url.username or url.password or url.query or url.fragment or not url.hostname
                    or url.scheme not in ('http','https') or (not local and url.scheme != 'https')):
                raise ValueError('Удаленная модель требует HTTPS; секреты в URL запрещены')
            if not isinstance(model.get('local'), bool) or model['local'] != local:
                raise ValueError('Локальной считается только модель на loopback-адресе')
            for key in ('enabled','tools'):
                if not isinstance(model.get(key, True), bool): raise ValueError('enabled/tools должны быть логическими')
            key_env = model.get('key_env', '')
            if not isinstance(key_env, str) or key_env and not re.fullmatch('[A-Z][A-Z0-9_]*_API_KEY', key_env):
                raise ValueError('Укажите имя переменной *_API_KEY, а не сам ключ')
            from intent_router import INTENT_DESCRIPTIONS
            intents=model.get('intents',[])
            if not isinstance(intents,list) or any(not isinstance(value,str) or value not in {*INTENT_DESCRIPTIONS,'default'} for value in intents):
                raise ValueError('Укажите известные намерения маршрутизатора или default')
            for key,low,high in (('priority',-100,100),('timeout_seconds',1,600),('cooldown_seconds',1,3600)):
                if key in model and (isinstance(model[key],bool) or not isinstance(model[key],int) or not low<=model[key]<=high):raise ValueError('Некорректный параметр '+key)
            for key, default, low, high in (('context_tokens', 32768, 1024, 2000000), ('output_tokens', 4096, 1, 65536)):
                value = model.get(key, default)
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ValueError('Некорректный токенный лимит')
            if model.get('output_tokens',4096) >= model.get('context_tokens',32768):
                raise ValueError('Лимит ответа должен быть меньше контекста')
            for key in ('input_price','output_price','daily_budget'):
                value = model.get(key, 0)
                if isinstance(value, bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value < 0 or value > 100000:
                    raise ValueError('Тарифы и бюджет должны быть конечными положительными числами')
            result.append(dict(model, base_url=model['base_url'].rstrip('/')))
        return result

    def update(self, models, version):
        models = self.validate(models)
        if not isinstance(version, int) or isinstance(version, bool): raise ValueError('Укажите версию')
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute("SELECT value FROM metadata WHERE key='model_registry'").fetchone()
            current = json.loads(row[0]) if row else dict(version=0,models=[])
            if current['version'] != version: raise TaskConflict('Model registry changed')
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('model_registry_version:'+str(version), json.dumps(current)))
            updated = dict(version=version+1, models=models)
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('model_registry',json.dumps(updated)))
        return updated

    def backend(self, ident, private=False, remote_enabled=True):
        model = next((m for m in self.get()['models'] if m['id']==ident), None)
        if not model or not model.get('enabled', True): raise ModelPolicyError('Модель отсутствует или отключена')
        import time
        if (self.store.runtime('model_cooldown:'+ident) or {}).get('until',0)>time.time():raise ModelPolicyError('Выбранная модель временно недоступна после ошибки')
        if (private or not remote_enabled) and not model['local']:
            raise ModelPolicyError('Политика приватности запрещает удаленную модель')
        key = os.environ.get(model.get('key_env',''), '')
        if model.get('key_env') and not key: raise ModelPolicyError('Не настроен ключ выбранного провайдера')
        return dict(model, provider='registered', api_key=key)

    def route(self,label,private=False,remote_enabled=True,needs_tools=False):
        candidates=sorted(self.get()['models'],key=lambda model:model.get('priority',0),reverse=True)
        for model in candidates:
            if (label or 'default') not in model.get('intents',[]) or needs_tools and not model.get('tools',True):continue
            try:return self.backend(model['id'],private,remote_enabled)
            except ModelPolicyError:continue
        return None

    def unavailable(self,backend):
        import time
        self.store.set_runtime('model_cooldown:'+backend['id'],dict(until=time.time()+backend.get('cooldown_seconds',60)))

    def status(self):
        from datetime import datetime, timezone
        import time
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        result=[]
        for model in self.get()['models']:
            with self.store.connect() as db:
                spent=db.execute('SELECT value FROM metadata WHERE key=?',('model_spend:'+model['id']+':'+day,)).fetchone()
            result.append(dict(id=model['id'],local=model['local'],enabled=model.get('enabled',True),
                credential_configured=not model.get('key_env') or bool(os.environ.get(model['key_env'])),
                cooldown_seconds=max(0,round((self.store.runtime('model_cooldown:'+model['id']) or {}).get('until',0)-time.time())),
                reserved_cost_today=float(spent[0]) if spent else 0,daily_budget=model.get('daily_budget',0),cost_basis='configured_utf8_upper_bound'))
        return result

    def reserve(self, backend, payload):
        """Conservative UTF-8 byte upper bound, charged before HTTP, including failures.

        This is a configured estimate, not a provider invoice. No automatic refunds
        after uncertain network failures. Units are the owner's configured currency.
        """
        from datetime import datetime, timezone
        input_tokens = len(json.dumps({'messages':payload.get('messages',[]), 'tools':payload.get('tools',[])},ensure_ascii=False).encode('utf-8'))
        output_tokens = min(payload.get('max_tokens',4096), backend.get('output_tokens',4096))
        if input_tokens + output_tokens > backend.get('context_tokens',32768):
            raise ModelPolicyError('Запрос превышает контекст выбранной модели; сократите историю или документы')
        if payload.get('tools') and not backend.get('tools',True):
            raise ModelPolicyError('Выбранная модель не поддерживает инструменты')
        estimate = (input_tokens*backend.get('input_price',0)+output_tokens*backend.get('output_price',0))/1000000
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        key = 'model_spend:'+backend['id']+':'+day
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            spent = float(row[0]) if row else 0
            budget = backend.get('daily_budget',0)
            if budget and spent+estimate > budget:
                raise ModelPolicyError('Достигнут суточный расчетный бюджет модели')
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,str(spent+estimate)))
        payload['max_tokens'] = output_tokens
        return dict(estimated_cost=estimate, reserved_input_tokens=input_tokens, reserved_output_tokens=output_tokens, cost_basis='configured_utf8_upper_bound', budget_day=day)
