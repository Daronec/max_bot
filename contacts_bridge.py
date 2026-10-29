"""Use LaunchServices so privacy access belongs to the Contacts app, not its parent terminal."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from config import BASE_DIR


def invoke(arguments):
    directory=BASE_DIR/'contacts_requests'
    directory.mkdir(exist_ok=True,mode=0o700)
    with (directory/'.lock').open('a') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        path=directory/(uuid.uuid4().hex+'.json')
        response=Path(str(path)+'.result.json')
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as output:json.dump({'args':arguments},output,ensure_ascii=False)
            subprocess.run(['/usr/bin/open','-W','-n',str(BASE_DIR/'helpers/MAX Bot Contacts.app'),'--args','--request',str(path)],
                           stdin=subprocess.DEVNULL,capture_output=True,timeout=90,check=True)
            return json.loads(response.read_text())
        finally:
            path.unlink(missing_ok=True)
            response.unlink(missing_ok=True)


if __name__=='__main__':
    try:print(json.dumps(invoke(sys.argv[1:]),ensure_ascii=False))
    except Exception:print(json.dumps({'error':'Не удалось запустить MAX Bot Contacts. Откройте приложение на Mac и разрешите доступ к Контактам.'},ensure_ascii=False))
