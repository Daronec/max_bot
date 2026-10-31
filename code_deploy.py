"""Standalone deployment runner copied before changing any live source files.

It waits for the MAX reply to be delivered, stops the old process, replaces only
reviewed files and checks the new server. Failed startup restores the previous tree.
This module deliberately imports no application code or credentials.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name or not path.parts or '\\' in name:
        raise ValueError('Invalid deployment path')
    public = {'.env.example', '.gitignore', 'requirements.txt', 'requirements-tts.txt', 'README.md', 'START.md'}
    if any(part.startswith('.') for part in path.parts) and name not in public:
        raise ValueError('Private deployment path')
    allowed = (len(path.parts)==1 and (path.suffix in {'.py','.md','.sh','.command'} or name in public)
               or path.parts[0]=='tests' and path.suffix=='.py'
               or path.parts[0]=='helpers' and path.suffix in {'.swift','.plist'}
               or path.parts[0]=='static' and path.suffix in {'.html','.css','.js','.json'})
    if not allowed:raise ValueError('Not a source file')
    target = root / name
    if not target.resolve().is_relative_to(root.resolve()):raise ValueError('Path escape')
    for part in (target, *target.parents):
        if part == root:break
        if part.is_symlink():raise ValueError('Symlink deployment path')
    if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):raise ValueError('Not a regular source file')
    return target


def write_file(path, data, mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.deploy-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'wb') as output:
            output.write(data);output.flush();os.fsync(output.fileno())
        os.chmod(temporary,mode)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def save_state(folder, state):
    write_file(folder/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode())


def current_hash(path):
    return digest(path.read_bytes()) if path.exists() else None


def source_inventory(root):
    result = {}
    for directory, folders, names in os.walk(root):
        relative = Path(directory).relative_to(root)
        folders[:] = [name for name in folders if not name.startswith('.')
                      and (relative.parts or name in ('tests','static','helpers'))]
        for name in names:
            name = (Path(directory)/name).relative_to(root).as_posix()
            try:path = safe_path(root,name)
            except ValueError:continue
            data = path.read_bytes()
            if len(data)>256000 or b'\x00' in data:continue
            result[name] = digest(data)
    return result


def verify_candidate(folder, state):
    deployment = state['deployment']
    if not deployment['changes'] or len(deployment['changes'])>20:raise ValueError('Invalid change manifest')
    for name, expected in deployment['changes'].items():
        if current_hash(safe_path(folder/'tree',name)) != expected:raise ValueError('Reviewed change was modified')
    for name, expected in state['base'].items():
        if current_hash(safe_path(folder/'base',name)) != expected:raise ValueError('Original snapshot was modified')
        if name not in deployment['changes'] and current_hash(safe_path(folder/'tree',name)) != expected:
            raise ValueError('Unreviewed source modification')
    candidate = {**state['base'],**deployment['changes']}
    if source_inventory(folder/'tree') != candidate:raise ValueError('Unreviewed files in candidate')
    expected = digest(json.dumps(candidate,sort_keys=True).encode())
    tests = state.get('tests') or {}
    if not tests.get('passed') or tests.get('fingerprint') != expected:raise ValueError('Tests do not match reviewed files')


def get_status(host, port):
    with urllib.request.urlopen(f'http://{host}:{port}/api/status',timeout=3) as response:
        return json.load(response)


def stop_bot(root):
    pid = int((root/'logs/server.pid').read_text())
    result = subprocess.run(['/bin/ps','-p',str(pid),'-o','args='],capture_output=True,text=True)
    if result.returncode:return
    expected = f'{root}/.venv/bin/python -u {root}/run.py'
    if result.stdout.strip() != expected:raise RuntimeError('Unexpected bot process')
    os.kill(pid,signal.SIGTERM)
    for _ in range(60):
        check = subprocess.run(['/bin/ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
        if check.returncode or check.stdout.strip().startswith('Z'):return
        time.sleep(.25)
    raise RuntimeError('Bot did not stop')


def launch_bot(root):
    environment = dict(os.environ)
    environment.pop('PYTHONPYCACHEPREFIX',None)
    subprocess.run([str(root/'.venv/bin/python'),str(root/'launch_server.py')],cwd=root,
                   stdin=subprocess.DEVNULL,check=True,timeout=30,env=environment)


def invalidate_bytecode(root, names):
    # Python's timestamp cache uses whole seconds and file size. A quick rollback
    # to equally sized source could otherwise keep executing the newer bytecode.
    for name in names:
        if not name.endswith('.py'):continue
        source = safe_path(root,name)
        cache = source.parent/'__pycache__'
        if cache.is_dir() and not cache.is_symlink():
            for file in cache.glob(source.stem+'.*.pyc'):
                if file.is_file() and not file.is_symlink():file.unlink()


def healthy(host, port, bot_id):
    for _ in range(30):
        try:
            status = get_status(host,port)
            if (status.get('status')=='ok' and status.get('bot_id')==bot_id and status.get('worker_active')
                    and status.get('monitoring_active') and not status.get('worker_error') and not status.get('monitoring_error')):
                return
        except (OSError,ValueError):pass
        time.sleep(1)
    raise RuntimeError('New bot failed health check')


def deploy(root, folder, state):
    mode = state['deployment']['mode']
    verify_candidate(folder,state)
    expected = state['deployment']['changes'] if mode=='rollback' else {
        name:state['base'].get(name) for name in state['deployment']['changes']}
    expected_sources = {**state['base'],**state['deployment']['changes']} if mode=='rollback' else state['base']
    if source_inventory(root) != expected_sources:raise ValueError('Live project changed after review')
    for name,value in expected.items():
        if current_hash(safe_path(root,name)) != value:raise ValueError('Live source changed: '+name)
    # Host and port are captured by the running server, not guessed from .env.
    host,port = state['deployment']['host'],state['deployment']['port']
    old_status = get_status(host,port)
    bot_id = old_status['bot_id']
    original = {}
    recovery = folder/'recovery';recovery.mkdir(exist_ok=True,mode=0o700)
    for name in expected:
        path = safe_path(root,name)
        data = path.read_bytes() if path.exists() else None
        permissions = path.stat().st_mode & 0o777 if path.exists() else 0o600
        original[name] = (data,permissions)
        if data is not None:write_file(safe_path(recovery,name),data,permissions)
    state['recovery_manifest'] = {name:None if data is None else digest(data) for name,(data,_) in original.items()}
    save_state(folder,state)
    stopped,changed = False,False
    try:
        stop_bot(root);stopped = True
        if source_inventory(root) != expected_sources:raise ValueError('Live project changed before deployment')
        # Check again after stopping: a message or manual edit could have raced approval.
        for name,value in expected.items():
            if current_hash(safe_path(root,name)) != value:raise ValueError('Live source changed before deployment')
        for name in expected:
            path = safe_path(root,name)
            data = safe_path(folder/'tree',name).read_bytes() if mode=='apply' else (
                safe_path(folder/'base',name).read_bytes() if name in state['base'] else None)
            changed = True
            if data is None:
                if path.exists():path.unlink()
            else:write_file(path,data,original[name][1])
        invalidate_bytecode(root,expected)
        launch_bot(root);healthy(host,port,bot_id)
        state.update(phase='applied' if mode=='apply' else 'rolled_back',
                     deployment_result='Проверенные изменения применены. Бот перезапущен и отвечает.' if mode=='apply' else 'Прежние исходники восстановлены. Бот перезапущен и отвечает.')
    except Exception as error:
        print('Deployment failure:', repr(error), flush=True)
        if changed:
            try:
                stop_bot(root)
                for name,(data,permissions) in original.items():
                    path = safe_path(root,name)
                    if data is None:
                        if path.exists():path.unlink()
                    else:write_file(path,data,permissions)
                invalidate_bytecode(root,original)
                launch_bot(root);healthy(host,port,bot_id)
                state.update(phase='rolled_back' if mode=='apply' else 'applied',
                             deployment_result='Обновление не прошло проверку запуска ('+type(error).__name__+'). Восстановлена версия, работавшая до попытки; бот отвечает.')
            except Exception as recovery_error:
                print('Recovery failure:', repr(recovery_error), flush=True)
                state.update(phase='deployment_failed',deployment_result='Не удалось подтвердить автоматическое восстановление: '+type(recovery_error).__name__+'. Нужна проверка deployment.log на компьютере.')
        else:
            if stopped:launch_bot(root)
            state.update(phase='deployment_failed',deployment_result='Исходники не менялись: '+type(error).__name__+'.')
    save_state(folder,state)


def main(root, ident, token):
    root = Path(root).resolve()
    if not re.fullmatch(r'[a-f0-9]{12}',ident):raise ValueError('Invalid job ID')
    folder = root/'.code_changes'/ident
    if folder.is_symlink() or folder.parent.is_symlink():raise ValueError('Invalid job directory')
    with (folder.parent/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((folder/'state.json').read_text())
        if state['phase']!='deploying' or state['deployment']['token']!=token:return
        try:
            task_id = state['deployment']['task_id']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',task_id):raise ValueError('Invalid task ID')
            deadline = time.monotonic()+120
            while True:
                task_path = root/'tasks'/(task_id+'.json')
                task = json.loads(task_path.read_text()) if task_path.is_file() else {}
                if task.get('status')=='completed' and task.get('delivered'):
                    break
                if time.monotonic()>deadline:raise RuntimeError('Approval reply was not delivered')
                time.sleep(.5)
            deploy(root,folder,state)
        except Exception as error:
            state.update(phase='deployment_failed',deployment_result='Применение не выполнено: '+type(error).__name__+'. Подробности в deployment.log; применение не подтверждено.')
            save_state(folder,state)


if __name__=='__main__':
    main(*sys.argv[1:])
