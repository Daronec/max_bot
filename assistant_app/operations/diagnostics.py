"""Bounded owner-only diagnostic log tails with configured credentials redacted."""
import os
from pathlib import Path
import re

NAMES=('server.log','worker.log','supervisor.log','launchd.log')


def log_tail(root,name):
    if name not in NAMES:raise ValueError('Неизвестный журнал')
    root=Path(root).resolve();path=root/'logs'/name
    if path.is_symlink() or (root/'logs').is_symlink():raise ValueError('Ссылки на журналы запрещены')
    if not path.is_file():return dict(name=name,text='Журнал пока не создан',truncated=False)
    with path.open('rb') as stream:
        stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-65536));data=stream.read(65536)
    text=data.decode('utf-8',errors='replace')
    if size>65536:text=text.split('\n',1)[-1]
    text='\n'.join(text.splitlines()[-150:])
    from dotenv import dotenv_values
    from assistant_app.api.secret_vault import vault_values
    sources = (dotenv_values(root/'.env',interpolate=False), vault_values(root), os.environ)
    credentials = {value for source in sources for key,value in source.items()
                   if value and len(value)>=6 and re.search('TOKEN|PASSWORD|SECRET|API_KEY|PRIVATE_KEY',key,re.I)}
    for value in sorted(credentials,key=len,reverse=True):
        text=text.replace(value,'[REDACTED]')
    text=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+',r'\1[REDACTED]',text)
    text=re.sub(r'(?i)((?:token|access_token|api_key|signature)=)[^&\s]+',r'\1[REDACTED]',text)
    return dict(name=name,text=text,truncated=size>65536)
