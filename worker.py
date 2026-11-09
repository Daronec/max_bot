"""Standalone worker. One OS lock protects recovery and execution per task store."""
if __name__ == '__main__':
    from runtime import use_project_python
    use_project_python()

import argparse
import fcntl
import logging
import os
from pathlib import Path
import signal
import threading
import time


def run_worker(directory, parent_pid=None):
    # Import after argument parsing: tests can inspect CLI help without configuration.
    import server
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    server.TASKS_DIR = str(directory)
    with (directory / '.worker.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A worker already owns this task store')
        server.stop_event.clear()
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: server.stop_event.set())
        server.load_tasks(recover=True, recover_submissions=False, active_delivery_pid=parent_pid)
        server.worker_active = True
        def heartbeat():
            while not server.stop_event.is_set():
                if parent_pid is not None and os.getppid() != parent_pid:
                    # A blocked model call must not keep an orphan worker holding
                    # the queue lock after its API process has disappeared.
                    os._exit(1)
                try:
                    server.task_store().set_runtime('worker', dict(
                        pid=os.getpid(), heartbeat=time.time(), active=True, error=server.worker_error,
                        model_routing=server.routing_status()))
                except Exception:
                    logging.exception('Worker heartbeat failed')
                    server.stop_event.set()
                    break
                server.stop_event.wait(1)
        monitor = threading.Thread(target=heartbeat, daemon=True)
        monitor.start()
        try:
            server.task_worker()
        finally:
            server.stop_event.set()
            monitor.join(timeout=3)
            server.task_store().set_runtime('worker', dict(
                pid=os.getpid(), heartbeat=time.time(), active=False, error=server.worker_error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks-dir', required=True)
    parser.add_argument('--parent-pid', type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_worker(args.tasks_dir, args.parent_pid)


if __name__ == '__main__':
    main()
