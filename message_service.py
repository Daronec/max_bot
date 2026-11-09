"""Application message dispatch, independent of HTTP and task storage."""
import re
from assistant_identity import strip_assistant_address, ADDRESS_REPLY


def execute_message(text, *, process_message, is_balance_query, get_balance,
                    contact_query, search_contacts, is_today_reminders_query,
                    today_reminders, build_summary, bot_client, permissions):
    text = strip_assistant_address(text)
    if not text:
        return ADDRESS_REPLY
    if is_balance_query(text):
        return get_balance() if permissions else 'Баланс API доступен только разрешённому чату.'
    query = contact_query(text)
    if query is not None:
        return search_contacts(query) if permissions else 'Контакты доступны только разрешённому чату.'
    if permissions and is_today_reminders_query(text):
        return today_reminders()
    if text.strip().lower() == '/voice':
        return 'Если голосовое не доходит из MAX, отправьте запись как аудиофайл M4A, MP3, OGG или WAV (до 5 минут, 20 МБ). Нативные голосовые MAX иногда передаёт ботам пустым событием без аудио.'
    if permissions and re.search(r'^/summary$|(?:пришли|покажи|составь|дай).*сводк|утренн[яю][яю] сводк', text, re.I):
        return build_summary(bot_client)
    return process_message(text, **permissions)
