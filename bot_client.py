"""MAX Bot API client. Message IDs are opaque strings, never numeric offsets."""
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from config import API_BASE_URL, BOT_TOKEN, CHAT_ID, MAX_CA_BUNDLE


def message_id(message):
    return str(message.get('body', {}).get('mid', message.get('id', '')))


def message_text(message):
    return message.get('body', {}).get('text') or message.get('text') or ''


class MAXBotClient:
    def __init__(self, token: str, chat_id=None):
        self.token = token
        self.chat_id = chat_id if chat_id is not None else CHAT_ID
        self.headers = {'Authorization': token, 'Content-Type': 'application/json'}
        self.last_id = None
        self.last_chat_id = None
        self.last_message = ''
        self.marker = None
        self.last_error = None
        self._lock = threading.Lock()
        self._next_request = 0.0
        self._next_send = {}

    @staticmethod
    def validate_chat_id(value):
        if isinstance(value, bool):
            raise ValueError('chat_id должен быть числовым ID чата MAX')
        try:
            return int(str(value))
        except (ValueError, TypeError):
            raise ValueError('Укажите числовой MAX_CHAT_ID или chat_id в запросе') from None

    def check_rate_limit(self, chat_id=None):
        with self._lock:
            now = time.monotonic()
            ready = max(now, self._next_request, self._next_send.get(chat_id, 0))
            if ready > now:
                time.sleep(ready - now)
            self._next_request = ready + 1 / 30
            if chat_id is not None:
                self._next_send[chat_id] = ready + 0.5

    def _request(self, method, path, params=None, data=None, timeout=35, send_chat=None):
        self.check_rate_limit(send_chat)
        try:
            response = requests.request(method, f'{API_BASE_URL}{path}', headers=self.headers,
                                        params=params, json=data, timeout=timeout, verify=MAX_CA_BUNDLE)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Некорректный ответ MAX API')
            self.last_error = None
            return payload
        except requests.exceptions.SSLError:
            self.last_error = 'Ошибка сертификата MAX API. Выполните python3 setup_max_tls.py и перезапустите сервер'
            return None
        except (requests.RequestException, ValueError):
            # Do not expose URLs or credentials from transport exceptions.
            self.last_error = 'Ошибка запроса к MAX API; проверьте настройки и доступ к чату'
            return None

    def send_message(self, chat_id, text, reply_to_message_id=None,
                     parse_mode=None, disable_notification=False, attachments=None):
        chat_id = self.validate_chat_id(chat_id)
        data = {'text': text, 'notify': not disable_notification}
        if attachments is not None:
            data['attachments'] = attachments
        if parse_mode:
            data['format'] = parse_mode.lower()
        if reply_to_message_id is not None:
            data['link'] = {'type': 'reply', 'mid': str(reply_to_message_id)}
        return self._request('POST', '/messages', params={'chat_id': chat_id},
                             data=data, send_chat=chat_id)

    def answer_callback(self, callback_id, notification=''):
        return self._request('POST', '/answers', params={'callback_id': str(callback_id)},
                             data={'notification': notification[:200]})

    def get_last_messages(self, chat_id, limit=50):
        return self._request('GET', '/messages', params={
            'chat_id': self.validate_chat_id(chat_id), 'count': max(1, min(100, limit))})

    def get_chat_info(self, chat_id):
        return self._request('GET', f'/chats/{self.validate_chat_id(chat_id)}')

    def get_updates(self, offset=None):
        marker = self.marker if offset is None else offset
        params = {'timeout': 25, 'types': 'message_created,message_callback,bot_started'}
        if marker is not None:
            params['marker'] = marker
        return self._request('GET', '/updates', params=params)

    def send_task(self, message, task_file, chat_id=None):
        target = self.validate_chat_id(chat_id if chat_id is not None else self.chat_id)
        response = self.send_message(target, f'📝 ЗАДАЧА ДЛЯ ВЫПОЛНЕНИЯ:\n{message}\n\n📁 Файл задачи: {task_file}')
        sent = (response or {}).get('message', {})
        mid = message_id(sent)
        if not mid:
            return None
        self.last_message = message
        self.last_id, self.last_chat_id = mid, target
        return mid

    @staticmethod
    def find_reply(messages, task_msg_id):
        for message in messages:
            linked = message.get('link') or {}
            original = linked.get('message') or {}
            linked_id = linked.get('mid') or message_id(original)
            if (message_id(message) != str(task_msg_id)
                    and linked.get('type') == 'reply' and str(linked_id) == str(task_msg_id)
                    and not message.get('sender', {}).get('is_bot', False)
                    and message_text(message).strip()):
                return message
        return None

    def get_result(self, task_msg_id, result_file='result.txt', chat_id=None) -> Optional[str]:
        target = chat_id if chat_id is not None else self.last_chat_id or self.chat_id
        data = self.get_last_messages(target, limit=100)
        reply = self.find_reply((data or {}).get('messages', []), task_msg_id)
        if reply is None:
            return None
        text = message_text(reply)
        if result_file is not None:
            path = Path(result_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'# РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ\n## Задача:\n{self.last_message}\n## Результат:\n{text}', encoding='utf-8')
        return text

    def _strip_task(self, task_content):
        return '\n'.join(line for line in task_content.splitlines()
                         if line.strip() and not line.startswith(('📝 ЗАДАЧА', '📁 Файл задачи:', '====')))

    def start_monitoring(self, chat_id=None, interval=1):
        target = self.validate_chat_id(chat_id if chat_id is not None else self.chat_id)
        try:
            while True:
                data = self.get_updates()
                if data is not None:
                    for update in data.get('updates', []):
                        message = update.get('message', {})
                        if message.get('recipient', {}).get('chat_id') == target:
                            print(message_text(message))
                    self.marker = data.get('marker', self.marker)
                time.sleep(interval)
        except KeyboardInterrupt:
            return


if __name__ == '__main__':
    MAXBotClient(BOT_TOKEN).start_monitoring()
