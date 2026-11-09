"""Local panel authentication; secrets never enter model context or API responses."""
from collections import defaultdict, deque
import hmac
import os
from pathlib import Path
import secrets
import threading
import time


class PanelAuth:
    def __init__(self, directory, token=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token = token or self._secret('panel.token')
        self.session_key = self._secret('panel.session')
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def _secret(self, name):
        path = self.directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = path.read_text().strip()
            if not value:
                raise RuntimeError('Panel secret is empty')
            return value
        value = secrets.token_urlsafe(32)
        with os.fdopen(descriptor, 'w') as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        return value

    def matches(self, value):
        return isinstance(value, str) and hmac.compare_digest(self.token, value)

    def login(self, address, value):
        with self._lock:
            attempts = self._attempts[address]
            now = time.monotonic()
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 5:
                return 'limited'
            if not self.matches(value):
                attempts.append(now)
                return 'invalid'
            attempts.clear()
            return 'ok'
