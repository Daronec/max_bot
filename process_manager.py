"""Manage an owned subprocess, with bounded crash restarts and graceful shutdown."""
from collections import deque
import logging
import os
from pathlib import Path
import subprocess
import threading
import time


logger = logging.getLogger(__name__)


class ProcessManager:
    def __init__(self, command, cwd, log_path, env=None, pid_path=None, stop_timeout=8):
        self.command = list(command)
        self.cwd = Path(cwd)
        self.log_path = Path(log_path)
        self.env = env
        self.pid_path = Path(pid_path) if pid_path else None
        self.stop_timeout = stop_timeout
        self.process = None
        self.error = None
        self.restarts = 0
        self._guard = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def _spawn(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        self.log_path.chmod(0o600)
        with os.fdopen(descriptor, 'ab') as output:
            self.process = subprocess.Popen(self.command, cwd=self.cwd, env=self.env,
                                            stdin=subprocess.DEVNULL, stdout=output,
                                            stderr=output, start_new_session=True)
        if self.pid_path:
            try:
                temporary = self.pid_path.with_suffix('.tmp')
                temporary.write_text(str(self.process.pid))
                os.replace(temporary, self.pid_path)
            except OSError:
                self.process.terminate()
                self.process.wait(timeout=3)
                raise

    def start(self):
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self.error = None
            self._spawn()
            self._thread = threading.Thread(target=self._watch, daemon=True)
            self._thread.start()

    def _watch(self):
        failures = deque()
        while not self._stop.wait(.25):
            with self._guard:
                if self._stop.is_set():
                    return
                code = self.process.poll()
                if code is None:
                    continue
                current = time.monotonic()
                while failures and current - failures[0] > 60:
                    failures.popleft()
                failures.append(current)
                self.error = f'Process exited with code {code}'
                if len(failures) >= 3:
                    self.error = 'Repeated process failure; automatic restarts stopped'
                    return
            if self._stop.wait(min(2 ** len(failures), 8)):
                return
            with self._guard:
                if self._stop.is_set():
                    return
                try:
                    self._spawn()
                except OSError:
                    logger.exception('Could not restart managed process')
                    self.error = 'Could not restart process'
                    return
                self.restarts += 1
                self.error = None

    def status(self):
        with self._guard:
            running = self.process is not None and self.process.poll() is None
            return dict(running=running, pid=self.process.pid if running else None,
                        error=self.error, restarts=self.restarts)

    def stop(self):
        self._stop.set()
        with self._guard:
            process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
