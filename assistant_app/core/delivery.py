"""Channel transport and durable delivery of completed responses."""
import logging

from assistant_app.channels.max_client import message_id
from assistant_app.channels.telegram import TelegramClient, TelegramError

logger = logging.getLogger(__name__)
CHUNK_SIZE = 4000


def send_reply(task, text, *, allowed, max_client, telegram_token, **options):
    if not allowed(task):
        return None
    speech_path = options.pop('speech_path', None)
    if task.get('delivery_channel', task.get('source')) == 'telegram':
        try:
            client = TelegramClient(telegram_token)
            reply_id = task.get('bot_message_id')
            if speech_path:
                response = client.send_audio(task['chat_id'], speech_path, text, reply_id)
            else:
                response = client.send(task['chat_id'], text, reply_id)
            return {'message': {'id': str(response['message_id'])}} if response.get('message_id') else None
        except TelegramError:
            return None
    if speech_path:
        attachment = max_client.upload_audio(speech_path)
        if attachment is None:
            return None
        options['attachments'] = [attachment]
    return max_client.send_message(
        task['chat_id'], text,
        reply_to_message_id=task.get('reply_to_message_id', task.get('bot_message_id')),
        **options,
    )


def deliver_result(task, *, store, lock, persist, refresh_tasks, send, record_mood_receipt):
    """Claim delivery and resume at the first chunk without a saved receipt."""
    with lock:
        if not task.get('_revision'):
            persist(task)
        claimed = store.claim_delivery(task['task_id'])
        if claimed is None:
            refresh_tasks()
            return
        task.clear()
        task.update(claimed)
        result = task['result']
        chunks = [result[offset:offset + CHUNK_SIZE] for offset in range(0, len(result), CHUNK_SIZE)]
        automatic_voice = bool(task.get('auto_voice_reply') and task.get('speech_file'))
        if automatic_voice:
            chunks.append('Голосовой ответ')
        sent = task.get('sent_chunks', 0)
        for index in range(sent, len(chunks)):
            options = {}
            try:
                if task.get('speech_file') and index == (len(chunks) - 1 if automatic_voice else 0):
                    if task['speech_file'] != task['task_id'] + '.mp3':
                        raise ValueError('Некорректный аудиофайл доставки')
                    options['speech_path'] = str(store.directory / 'speech' / task['speech_file'])
                if index == 0 and task.get('result_attachments'):
                    options['attachments'] = task['result_attachments']
                response = send(task, chunks[index], **options)
            except Exception:
                task.update(delivered=False, delivery_inflight=False, delivery_uncertain=True)
                persist(task)
                logger.warning('Delivery interrupted; remote receipt is unknown')
                return
            receipt = message_id((response or {}).get('message', {}))
            if not receipt:
                # A missing receipt does not prove the remote side rejected the message.
                task.update(delivered=False, delivery_inflight=False, delivery_uncertain=True)
                persist(task)
                return
            task['sent_chunks'] = index + 1
            task.setdefault('delivery_receipts', []).append(receipt)
            persist(task)
        if task.get('background_purpose') == 'mood' and task.get('delivery_receipts'):
            record_mood_receipt(task['task_id'], task['delivery_receipts'][0])
        task['delivered'] = True
        task['delivery_inflight'] = False
        task.pop('delivery_retry_at', None)
        persist(task)
