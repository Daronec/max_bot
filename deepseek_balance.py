"""Read real DeepSeek account balance; never ask a language model to estimate it."""
from decimal import Decimal, InvalidOperation
import re
import requests
from config import DEEPSEEK_API_KEY, LM_FALLBACK_ENABLED


def is_balance_query(text):
    text=text.strip()
    if text.lower()=='/balance':return True
    if re.search(r'баланс\s+белого|балансиров|пополни|переведи|оплати',text,re.I):return False
    return bool(re.search(r'\bбаланс\b',text,re.I) and re.search(r'deepseek|дипсик|api|апи|покажи|показать|проверь|какой|сколько|узна|^баланс[?.!]?$',text,re.I))


def get_balance():
    if not DEEPSEEK_API_KEY:return 'Ключ DeepSeek API не настроен.'
    try:
        response=requests.get('https://api.deepseek.com/user/balance',
            headers={'Authorization':'Bearer '+DEEPSEEK_API_KEY},timeout=(10,20),allow_redirects=False)
        if response.status_code in (401,403):return 'DeepSeek отклонил ключ API. Проверьте ключ в настройках бота.'
        if response.status_code!=200:return 'Не удалось получить баланс DeepSeek (HTTP '+str(response.status_code)+'). Повторите запрос позже.'
        data=response.json()
        if not isinstance(data.get('is_available'),bool):raise ValueError()
        rows=data.get('balance_infos')
        if not isinstance(rows,list) or not rows:raise ValueError()
        lines=['Баланс DeepSeek API']
        for row in rows:
            currency=row['currency']
            if currency not in ('USD','CNY'):raise ValueError()
            values=[]
            for key in ('total_balance','granted_balance','topped_up_balance'):
                value=Decimal(str(row[key]))
                if not value.is_finite():raise ValueError()
                values.append(format(value,'f'))
            lines.extend([f'Доступно: {values[0]} {currency}',f'Пополнения: {values[2]} {currency}',f'Бонусы: {values[1]} {currency}'])
        lines.append('Средств для API достаточно.' if data['is_available'] else 'Средств для API недостаточно.' + (' Для заданий включена резервная локальная модель.' if LM_FALLBACK_ENABLED else ''))
        return '\n'.join(lines)
    except (requests.RequestException,ValueError,KeyError,TypeError,InvalidOperation):
        return 'Не удалось прочитать баланс DeepSeek. Проверьте интернет и повторите запрос позже.'
