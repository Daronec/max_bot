"""Telegram private-chat adapter using the common task service.

Only explicitly allowed sender IDs are accepted. No MAX permissions are reused.
"""
import hashlib
import threading
import re
import time

import requests


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token):
        self._token = token

    def call(self, method, payload):
        try:
            response = requests.post('https://api.telegram.org/bot' + self._token + '/' + method,
                                     json=payload, timeout=(5, 35), allow_redirects=False)
            if response.status_code != 200:
                raise TelegramError('Telegram API request failed')
            data = response.json()
            if not isinstance(data, dict):
                raise TelegramError('Invalid Telegram response')
            if data.get('ok') is not True:
                raise TelegramError('Telegram API rejected the request')
            return data['result']
        except (requests.RequestException, ValueError, KeyError, TypeError):
            # requests exception strings contain the URL and therefore the bot token.
            raise TelegramError('Telegram API unavailable or returned an invalid response') from None

    def updates(self, offset):
        result = self.call('getUpdates', dict(offset=offset, timeout=25, allowed_updates=['message']))
        if not isinstance(result, list):
            raise TelegramError('Invalid Telegram updates')
        return result

    def send(self, chat_id, text, reply_id=None):
        payload = dict(chat_id=chat_id, text=text)
        if reply_id is not None:
            payload['reply_parameters'] = dict(message_id=int(reply_id), allow_sending_without_reply=True)
        result = self.call('sendMessage', payload)
        if not isinstance(result, dict) or not isinstance(result.get('message_id'), int):
            raise TelegramError('Missing Telegram message receipt')
        return result

    def download_audio(self, file_id, destination):
        result = self.call('getFile', dict(file_id=file_id))
        path = result.get('file_path') if isinstance(result, dict) else None
        if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,500}', path) or '..' in path.split('/') or path.startswith('/'):
            raise TelegramError('Invalid Telegram file path')
        from config import VOICE_MAX_BYTES
        started = time.monotonic()
        try:
            with requests.get('https://api.telegram.org/file/bot' + self._token + '/' + path,
                              stream=True, timeout=(5, 20), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise TelegramError('Telegram audio download failed')
                total = 0
                with open(destination, 'wb') as output:
                    for block in response.iter_content(65536):
                        total += len(block)
                        if total > VOICE_MAX_BYTES or time.monotonic() - started > 60:
                            raise TelegramError('Telegram audio exceeds download limits')
                        output.write(block)
        except requests.RequestException:
            raise TelegramError('Telegram audio download unavailable') from None

    def send_audio(self, chat_id, path, caption='', reply_id=None):
        import json
        data = dict(chat_id=chat_id, caption=caption[:1000])
        if reply_id is not None:
            data['reply_parameters'] = json.dumps(dict(message_id=int(reply_id), allow_sending_without_reply=True))
        try:
            with open(path, 'rb') as audio:
                response = requests.post('https://api.telegram.org/bot' + self._token + '/sendVoice',
                    data=data, files={'voice':('reply.mp3',audio,'audio/mpeg')},
                    timeout=(5,60), allow_redirects=False)
            payload = response.json()
            if response.status_code != 200 or payload.get('ok') is not True or not isinstance(payload.get('result'),dict) or not isinstance(payload['result'].get('message_id'),int):
                raise TelegramError('Telegram audio receipt unavailable')
            return payload['result']
        except (requests.RequestException, ValueError, OSError):
            raise TelegramError('Telegram audio delivery unavailable') from None


class TelegramAdapter:
    def __init__(self, client, service, store, allowed_senders):
        self.client = client
        self.service = service
        self.store = store
        self.allowed_senders = frozenset(str(value) for value in allowed_senders)
        self.active = False
        self.error = None
        self.thread = None

    def handle(self, update):
        message = update.get('message') or {}
        sender = message.get('from') or {}
        chat = message.get('chat') or {}
        text, mid = message.get('text'), message.get('message_id')
        from config import VOICE_ENABLED, VOICE_MAX_BYTES, VOICE_MAX_SECONDS
        audio = message.get('voice') or message.get('audio') or {}
        voice = (VOICE_ENABLED and isinstance(audio, dict) and isinstance(audio.get('file_id'), str)
                 and isinstance(audio.get('duration'), (int, float)) and 0 < audio['duration'] <= VOICE_MAX_SECONDS
                 and isinstance(audio.get('file_size', 0), int) and 0 <= audio.get('file_size', 0) <= VOICE_MAX_BYTES)
        if voice and not isinstance(text, str):
            text = '[Голосовое сообщение]'
        if (sender.get('is_bot') or str(sender.get('id')) not in self.allowed_senders
                or chat.get('type') != 'private' or not isinstance(text, str)
                or not text.strip() or not isinstance(mid, int) or not isinstance(chat.get('id'), int)):
            return
        tid = 'tg_' + hashlib.sha256(f"{chat['id']}:{mid}".encode()).hexdigest()[:24]
        metadata = dict(telegram_audio=audio['file_id']) if voice else {}
        if any(message.get(key) for key in ('forward_origin', 'forward_date', 'forward_from', 'forward_from_chat')):
            metadata['memory_excluded'] = True
        self.service().submit(tid, text, chat['id'], 'telegram', bot_message_id=mid,
                              sender_id=str(sender['id']), user_id='telegram:' + str(sender['id']),
                              chat_type='private', **metadata)

    def poll(self, stop_event):
        self.active = True
        try:
            while not stop_event.is_set():
                try:
                    offset = (self.store().runtime('telegram_offset') or {}).get('offset', 0)
                    updates = self.client.updates(offset)
                    if not isinstance(updates, list):
                        raise TelegramError('Invalid Telegram updates')
                    for update in updates:
                        if stop_event.is_set():
                            break
                        ident = update.get('update_id')
                        if not isinstance(ident, int):
                            raise TelegramError('Missing Telegram update ID')
                        self.handle(update)
                        # A replay after a crash is deduplicated by deterministic task ID.
                        self.store().set_runtime('telegram_offset', dict(offset=ident + 1))
                    self.error = None
                except Exception:
                    self.error = 'Не удалось получить или сохранить сообщения Telegram'
                    stop_event.wait(5)
        finally:
            self.active = False

    def start(self, stop_event):
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.poll, args=(stop_event,), daemon=True)
        self.thread.start()

    def status(self):
        return dict(active=self.active, error=self.error, allowed_senders=len(self.allowed_senders))
