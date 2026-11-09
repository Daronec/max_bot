"""Versioned non-secret runtime settings, shared by API and worker."""
import json
import math

from task_service import TaskConflict


class AssistantSettings:
    def __init__(self, store, defaults):
        self.store = store
        self.defaults = dict(defaults)

    def get(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='assistant_settings'").fetchone()
            saved = json.loads(row[0]) if row else dict(version=0, values={})
            return dict(version=saved['version'], values={**self.defaults, **saved['values']})

    def update(self, changes, expected_version):
        if not isinstance(changes, dict) or not changes or set(changes) - set(self.defaults):
            raise ValueError('Неизвестные или пустые настройки')
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError('Укажите версию настроек')
        for key, value in changes.items():
            if key in ('local_model', 'remote_model'):
                if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 for char in value):
                    raise ValueError('Укажите идентификатор модели')
            elif key in ('remote_enabled', 'file_tools_enabled', 'commands_enabled', 'web_enabled'):
                if not isinstance(value, bool):
                    raise ValueError(key + ' должен быть логическим значением')
            elif key == 'max_attempts':
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
                    raise ValueError('Количество попыток: от 1 до 10')
            elif key == 'run_timeout':
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 10 <= value <= 3600:
                    raise ValueError('Лимит времени: от 10 до 3600 секунд')
        with self.store.connect() as db, self.store.transaction(db):
            row = db.execute("SELECT value FROM metadata WHERE key='assistant_settings'").fetchone()
            current = json.loads(row[0]) if row else dict(version=0, values={})
            if current['version'] != expected_version:
                raise TaskConflict('Settings changed; reload before saving')
            previous = json.dumps(current, ensure_ascii=False)
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', ('assistant_settings_version:' + str(current['version']), previous))
            current = dict(version=current['version'] + 1, values={**current['values'], **changes})
            db.execute("INSERT INTO metadata VALUES('assistant_settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (json.dumps(current, ensure_ascii=False),))
        return self.get()
