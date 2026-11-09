"""Project credentials and the panel-only PIN. Never pass values to a model."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

from dotenv import dotenv_values

KNOWN = {'MAX_BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'DEEPSEEK_API_KEY', 'LM_API_KEY', 'LOCAL_LM_API_KEY', 'HEYGEN_API_KEY'}
NAME = re.compile(r'[A-Z][A-Z0-9_]{0,90}_(?:TOKEN|API_KEY|SECRET|PASSWORD|PRIVATE_KEY)\Z')


def managed(name):
    return isinstance(name, str) and name != 'PANEL_ACCESS_TOKEN' and bool(NAME.fullmatch(name))


def atomic(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.new-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _cipher(directory, create=False):
    from cryptography.fernet import Fernet
    key = directory/'vault.key'
    if create and not key.exists():
        atomic(key, Fernet.generate_key())
    return Fernet(key.read_bytes())


def vault_values(root):
    directory = Path(root)/'.secrets'
    encrypted = directory/'project.vault'
    if not encrypted.exists():
        return {}
    data = json.loads(_cipher(directory).decrypt(encrypted.read_bytes()))
    return {key:value for key,value in data.items() if managed(key) and isinstance(value,str)}


class SecretVault:
    def __init__(self, root, external=None, registered_keys=None):
        self.root = Path(root)
        self.directory = self.root/'.secrets'
        self.pin_path = self.directory/'vault.pin'
        self.external = {key:value for key,value in (external or {}).items() if managed(key)}
        self.registered_keys = registered_keys or (lambda: ())
        self.lock = threading.RLock()
        self.failures = []

    def configured(self):
        return self.pin_path.is_file()

    @staticmethod
    def digest(pin, salt):
        return hashlib.scrypt(pin.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64*1024*1024)

    def setup(self, pin, confirmation):
        if not isinstance(pin,str) or not re.fullmatch(r'[0-9]{6,12}',pin) or pin != confirmation:
            raise ValueError('Введите одинаковый PIN из 6–12 цифр.')
        with self.lock:
            if self.configured():
                raise ValueError('PIN уже настроен.')
            salt = secrets.token_bytes(16)
            atomic(self.pin_path, json.dumps(dict(salt=salt.hex(),digest=self.digest(pin,salt).hex())).encode())

    def unlock(self, pin):
        if not isinstance(pin,str) or not re.fullmatch(r'[0-9]{6,12}',pin):
            return False
        with self.lock:
            now = time.monotonic()
            self.failures = [t for t in self.failures if now-t < 300]
            if len(self.failures) >= 5:
                return None
            if not self.configured():
                return False
            data = json.loads(self.pin_path.read_text())
            if hmac.compare_digest(self.digest(pin,bytes.fromhex(data['salt'])).hex(),data['digest']):
                self.failures.clear()
                return True
            self.failures.append(now)
            return False

    def _project_values(self):
        env = {k:v for k,v in dotenv_values(self.root/'.env',interpolate=False).items() if managed(k)}
        values = {**env, **vault_values(self.root)}
        names = KNOWN | values.keys() | {key for key in self.registered_keys() if managed(key)}
        return {name:self.external.get(name,values.get(name)) for name in sorted(names)}

    def entries(self):
        return [dict(name=name, configured=bool(value), editable=name not in self.external)
                for name,value in self._project_values().items()]

    def reveal(self, name):
        if not managed(name):
            raise ValueError('Неизвестное имя секрета.')
        return self._project_values().get(name) or ''

    def save(self, name, value):
        if not managed(name) or name in self.external:
            raise ValueError('Это имя нельзя изменить через панель.')
        if not isinstance(value,str) or len(value)>8192 or '\x00' in value or '\n' in value or '\r' in value:
            raise ValueError('Введите однострочное значение до 8192 символов.')
        with self.lock:
            values = vault_values(self.root)
            values[name] = value
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            cipher = _cipher(self.directory, create=True)
            atomic(self.directory/'project.vault', cipher.encrypt(json.dumps(values).encode()))
