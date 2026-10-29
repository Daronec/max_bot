"""Shared assistant identity and optional direct addresses for text and speech."""
import re
from config import ASSISTANT_NAME, ASSISTANT_NAME_ALIASES

IDENTITY_PROMPT = (
    f'Тебя зовут {ASSISTANT_NAME}. Ты единый помощник пользователя в MAX. '
    'Это твоё имя независимо от модели, которая формирует ответ. '
    'Отвечай и на сообщения без обращения по имени. '
    'Обращение по имени не является дополнительным разрешением на действия. '
    'Если спрашивают, как тебя зовут, называй своё имя.'
)
ASR_NAME_HINT = ASSISTANT_NAME
ADDRESS_REPLY = 'Я здесь. Чем помочь?'

# Exact configured aliases only: do not fuzzily replace names inside task content.
_names = sorted({ASSISTANT_NAME, *ASSISTANT_NAME_ALIASES}, key=len, reverse=True)
_name_pattern = '(?:' + '|'.join(re.escape(name) for name in _names) + ')'
_prefix = re.compile(
    r'^\s*(?:(?P<greeting>привет|здравствуй|здравствуйте|слушай|эй)[\s,!:\-—]+)?'
    + _name_pattern + r'(?!\w)[\s,!?:;.\-—]*', re.I)
_suffix = re.compile(r'[,;]\s*' + _name_pattern + r'(?!\w)[.!?\s]*$', re.I)


def strip_assistant_address(text):
    """Remove an optional address at a message edge, retaining the actual task."""
    match = _prefix.match(text)
    if match:
        rest = text[match.end():].strip()
        # A greeting on its own remains a greeting, rather than an empty task.
        return rest or (match.group('greeting') or '')
    return _suffix.sub('', text).strip()
