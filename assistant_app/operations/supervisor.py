"""Independent local control plane: authenticated panel and managed assistant API."""
import fcntl
import logging
import os
import signal
import threading
import tempfile

from assistant_app.api.auth import PanelAuth
from assistant_app.api.control import create_app
from .process_manager import ProcessManager


def main():
    logging.basicConfig(level=logging.INFO)
    external = dict(os.environ)
    from assistant_app.config import BASE_DIR, PORT, BOT_ID, PANEL_ACCESS_TOKEN, PANEL_ALLOWED_HOSTS, SUPERVISOR_PORT, TASKS_PATH, LOCAL_LM_BASE_URL
    from .maintenance import run as run_maintenance
    from assistant_app.models.gateway import _local_target, _ready, recover_local_server
    from assistant_app.models.registry import ModelRegistry
    from assistant_app.api.secret_vault import SecretVault
    from assistant_app.core.task_store import SQLiteTaskStore
    logs = BASE_DIR / 'logs'
    logs.mkdir(exist_ok=True)
    with (logs / '.supervisor.lock').open('a') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = PanelAuth(BASE_DIR / '.secrets', PANEL_ACCESS_TOKEN)
        manager = ProcessManager([str(BASE_DIR / '.venv/bin/python'), '-u', str(BASE_DIR / 'run.py')],
            BASE_DIR, logs / 'server.log', env=dict(external, HOST='127.0.0.1'), pid_path=logs / 'server.pid', stop_timeout=15)
        api_url = f'http://127.0.0.1:{PORT}'
        store = SQLiteTaskStore(TASKS_PATH)
        registry = ModelRegistry(store)
        vault = SecretVault(BASE_DIR, external,
                            lambda: (model.get('key_env') for model in registry.get()['models']))
        app = create_app(manager, auth, api_url, BOT_ID, PANEL_ALLOWED_HOSTS, log_root=BASE_DIR,
                         vault=vault, maintenance_store=store)
        maintenance_stop = threading.Event()
        def recover():
            target = _local_target(LOCAL_LM_BASE_URL)
            if target is not None and _ready(target):return 'already_healthy'
            return recover_local_server(LOCAL_LM_BASE_URL)
        def shutdown(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, shutdown)
        pid_path = logs / 'supervisor.pid'
        with tempfile.NamedTemporaryFile(mode='w', dir=logs, prefix='.supervisor-pid-', delete=False) as handle:
            handle.write(str(os.getpid()))
        os.replace(handle.name, pid_path)
        try:
            manager.start()
            threading.Thread(target=run_maintenance,args=(store,manager,app.extensions['control_lock'],api_url,
                recover,maintenance_stop),name='daily-maintenance',daemon=True).start()
            app.run(host='127.0.0.1', port=SUPERVISOR_PORT, use_reloader=False)
        finally:
            maintenance_stop.set()
            try:
                manager.stop()
            finally:
                pid_path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
