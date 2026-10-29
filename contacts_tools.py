"""Read-only contact search. Full address book export is deliberately not a query."""
import json
import re
import subprocess
import sys
from config import BASE_DIR


def contact_query(text):
    command=re.match(r'^/contacts(?:\s+(.*))?$',text.strip(),re.I)
    if command:return (command[1] or '').strip()
    if re.search(r'созда|добав|измени|удали|экспорт|все контакты',text,re.I):return None
    match=re.match(r'^(?:найди|покажи|поищи|открой)\s+(?:мне\s+)?контакт(?:ы)?\s+(.+?)[?.!]?$',text.strip(),re.I)
    if match:return match[1].strip(' «»"')
    match=re.match(r'^(?:какой\s+)?(?:телефон|номер телефона|email|почта)\s+(?:контакта\s+|у\s+)?(.+?)[?.!]?$',text.strip(),re.I)
    return match[1].strip(' «»"') if match else None


def search_contacts(query):
    if not isinstance(query,str) or not query.strip() or len(query)>200 or '\x00' in query:
        return 'Укажите имя: /contacts Иван Иванов или «Найди контакт Иван Иванов».'
    try:
        result=subprocess.run([sys.executable,str(BASE_DIR/'contacts_bridge.py'),query.strip()],capture_output=True,text=True,
                              stdin=subprocess.DEVNULL,timeout=100,check=True)
        data=json.loads(result.stdout)
        if data.get('error'):return data['error']
        contacts=data['contacts']
        if not contacts:return 'Контакты по этому имени не найдены. Попробуйте имя в именительном падеже, например «Иван».'
        lines=[]
        for contact in contacts:
            lines.append(contact['name'])
            lines.extend('Телефон: '+value for value in contact['phones'])
            lines.extend('Email: '+value for value in contact['emails'])
            if not contact['phones'] and not contact['emails']:lines.append('Телефон и email не указаны.')
            lines.append('')
        if data.get('truncated'):lines.append('Показаны первые 20 контактов. Уточните имя.')
        return '\n'.join(lines).strip()
    except (OSError,subprocess.SubprocessError,ValueError,KeyError,TypeError):
        return 'Не удалось прочитать Контакты. Проверьте разрешение macOS и helpers/contacts.'


def create_contact(given_name: str, family_name: str = '', phone: str = '', email: str = '') -> dict:
    """Create a macOS contact only on explicit user request. Copy names, phone and email from the user; never invent fields. Duplicate phone/email prevents creation. Returns actual saved identifier."""
    import shlex
    from command_tools import run_command
    values={'given_name':given_name,'family_name':family_name,'phone':phone,'email':email}
    limits={'given_name':100,'family_name':100,'phone':80,'email':254}
    if any(not isinstance(v,str) or '\x00' in v or len(v)>limits[k] for k,v in values.items()) or not given_name.strip():
        return {'error':'Укажите имя контакта и корректные поля.'}
    if phone and len(re.sub(r'\D','',phone))<3:
        return {'error':'Номер телефона слишком короткий.'}
    if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
        return {'error':'Некорректный email.'}
    # Existing side-effect journal records the attempt before the helper may save data.
    result=run_command(shlex.join([sys.executable,str(BASE_DIR/'contacts_bridge.py'),'--create',json.dumps(values,ensure_ascii=False)]),timeout=110)
    if result.get('error') or result.get('exit_code')!=0:
        return {'error':'Создание контакта не подтверждено. Проверьте разрешение macOS и адресную книгу перед повтором.'}
    try:
        data=json.loads(result.get('stdout',''))
        if not isinstance(data,dict) or not (data.get('error') or data.get('duplicate') or (data.get('created') and data.get('id'))):raise ValueError()
        return data
    except (ValueError,TypeError):
        return {'error':'Сохранение контакта не подтверждено. Проверьте адресную книгу перед повтором.'}


CREATE_SCHEMA={'type':'function','function':{'name':'create_contact','description':create_contact.__doc__,
    'parameters':{'type':'object','properties':{key:{'type':'string'} for key in ('given_name','family_name','phone','email')},'required':['given_name']}}}
