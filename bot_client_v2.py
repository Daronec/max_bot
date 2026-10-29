"""CLI compatibility wrapper around the shared MAX API implementation."""
import fnmatch
import time
from datetime import datetime
from pathlib import Path

from bot_client import MAXBotClient, message_text
from config import BASE_DIR, BOT_ID, BOT_TOKEN


class MAXBotClientV2(MAXBotClient):
    def __init__(self, token, bot_id=BOT_ID, chat_id=None):
        super().__init__(token, chat_id)
        self.bot_id = bot_id
        self.active = True
        self.last_message_id = None
        self.task_messages = {}

    def send_task(self, task_id, message):
        from server import valid_task_id
        task_id = valid_task_id(task_id)
        mid = super().send_task(message, f'{task_id}.txt')
        if mid is None:
            return None
        self.last_message_id = mid
        self.task_messages[task_id] = mid
        directory = BASE_DIR / 'tasks'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{task_id}.txt').write_text(message, encoding='utf-8')
        return dict(success=True, task_id=task_id, our_message_id=mid)

    def get_bot_answer(self, task_id, after_id=None):
        mid = after_id if after_id is not None else self.task_messages.get(task_id)
        if mid is None:
            return None
        return self.get_result(mid, result_file=None, chat_id=self.chat_id)

    def save_message_file(self, task_id, messages):
        from server import valid_task_id
        valid_task_id(task_id)
        if isinstance(messages, dict):
            messages = [messages]
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        directory = BASE_DIR / 'tasks'
        logs = BASE_DIR / 'logs'
        directory.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        text = '\n\n'.join(message_text(m) for m in messages)
        (directory / f'{task_id}_result_{stamp}.txt').write_text(text, encoding='utf-8')
        (logs / f'{task_id}_{stamp}.log').write_text(text, encoding='utf-8')

    def execute_task(self, task_id, timeout=120):
        from server import valid_task_id
        valid_task_id(task_id)
        path = BASE_DIR / 'tasks' / f'{task_id}.txt'
        if not path.is_file():
            return None
        sent = self.send_task(task_id, path.read_text(encoding='utf-8'))
        if not sent:
            return None
        deadline = time.monotonic() + timeout
        while self.active and time.monotonic() < deadline:
            answer = self.get_bot_answer(task_id, sent['our_message_id'])
            if answer:
                self.save_message_file(task_id, [{'text': answer}])
                return answer
            time.sleep(2)
        return None

    def start_monitoring(self, task_id_pattern='task_*'):
        # Monitor existing tasks without repeatedly resending them.
        try:
            while self.active:
                for tid in list(self.task_messages):
                    if not fnmatch.fnmatch(tid, task_id_pattern):
                        continue
                    answer = self.get_bot_answer(tid)
                    if answer:
                        self.save_message_file(tid, [{'text': answer}])
                        del self.task_messages[tid]
                        print(answer)
                time.sleep(2)
        except KeyboardInterrupt:
            self.active = False


def main():
    client = MAXBotClientV2(BOT_TOKEN)
    while True:
        choice = input('1 — отправить задачу; 2 — мониторинг отправленных задач; 4 — выход: ')
        try:
            if choice == '1':
                result = client.send_task(input('ID задачи: '), input('Текст: '))
                print(result or client.last_error)
            elif choice == '2':
                client.start_monitoring()
            elif choice == '4':
                return
        except ValueError as error:
            print(error)


if __name__ == '__main__':
    main()
