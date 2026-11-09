"""Offline control-plane releases. Prepare/tests first; apply only with all processes stopped."""
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from project_changes import inventory,safe_file,sha,atomic_json


@contextmanager
def stopped(root,tasks):
    with ExitStack() as stack:
        for directory,name in ((Path(root)/'logs','.supervisor.lock'),(tasks,'.api.lock'),(tasks,'.worker.lock')):
            Path(directory).mkdir(exist_ok=True,parents=True,mode=0o700)
            guard=stack.enter_context((Path(directory)/name).open('a'))
            try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Stop supervisor, API and worker before changing the control plane') from None
        yield


def prepare(root,source,test=True):
    root,source=Path(root).resolve(),Path(source).resolve()
    files=inventory(source)
    if not {'supervisor.py','server.py','http_api.py','process_manager.py','panel_auth.py'}<=files.keys():raise ValueError('A complete source tree is required')
    (root/'.supervisor_releases').mkdir(exist_ok=True,mode=0o700)
    ident=uuid.uuid4().hex[:12];folder=root/'.supervisor_releases'/ident;tree=folder/'tree';tree.mkdir(parents=True,mode=0o700)
    for name,expected in files.items():
        src=safe_file(source,name);data=src.read_bytes()
        if sha(data)!=expected:raise ValueError('Source changed during preparation')
        if name.endswith('.py'):compile(data,name,'exec')
        target=safe_file(tree,name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    if test:
        env={key:value for key,value in os.environ.items() if key in ('PATH','HOME','USER','TMPDIR','LANG','LC_ALL','LC_CTYPE')}
        env.update(PYTHONPATH=str(tree/'tests')+os.pathsep+str(tree),HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',ASSISTANT_TASKS_DIR=str(folder/'test-tasks'),MEMORY_DB_PATH=str(folder/'test-memory.sqlite3'))
        with (folder/'tests.log').open('w') as log:
            os.chmod(folder/'tests.log',0o600)
            result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests'],cwd=tree,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,timeout=180)
        if result.returncode:raise ValueError('Release tests failed; inspect the private tests.log')
    if inventory(tree)!=files:raise ValueError('Tested source tree changed')
    state=dict(id=ident,phase='ready',files=files,base=inventory(root),tested=bool(test))
    atomic_json(folder/'release.json',state)
    return state


def load(root,ident):
    if not re.fullmatch('[a-f0-9]{12}',ident):raise ValueError('Invalid release identifier')
    folder=Path(root)/'.supervisor_releases'/ident
    if folder.is_symlink() or not folder.resolve().is_relative_to((Path(root)/'.supervisor_releases').resolve()):raise ValueError('Invalid release path')
    state=json.loads((folder/'release.json').read_text())
    for name in ('tree', 'previous'):
        if (folder/name).is_symlink():raise ValueError('Release symlinks are forbidden')
    if state['id']!=ident:raise ValueError('Invalid release state')
    return folder,state


def replace(target,data):
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.supervisor-update-'+uuid.uuid4().hex)
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        if target.exists():temporary.chmod(target.stat().st_mode&0o777)
        os.replace(temporary,target)
    finally:temporary.unlink(missing_ok=True)


def rollback_files(root,folder,state):
    for name,expected in state['files'].items():
        target=safe_file(root,name)
        if name in state['base']:
            original=safe_file(folder/'previous',name).read_bytes()
            if sha(original)!=state['base'][name]:raise ValueError('Recovery copy is corrupt')
            replace(target,original)
        elif target.exists():
            if sha(target.read_bytes())!=expected:raise ValueError('New file changed; refusing to delete it')
            target.unlink()


def apply(root,tasks,ident):
    root=Path(root).resolve();folder,state=load(root,ident)
    with stopped(root,tasks):
        if state['phase']!='ready' or not state['tested']:raise ValueError('Only a tested ready release can be applied')
        if inventory(root)!=state['base'] or inventory(folder/'tree')!=state['files']:raise ValueError('Source hashes changed; prepare again')
        for name in state['files']:
            if name in state['base']:
                target=safe_file(folder/'previous',name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(safe_file(root,name).read_bytes());target.chmod(0o600)
        state['phase']='applying';atomic_json(folder/'release.json',state)
        try:
            for name in state['files']:replace(safe_file(root,name),safe_file(folder/'tree',name).read_bytes())
            state['phase']='applied';atomic_json(folder/'release.json',state)
        except BaseException:
            rollback_files(root,folder,state);state['phase']='rolled_back';atomic_json(folder/'release.json',state)
            raise
    return state


def rollback(root,tasks,ident):
    root=Path(root).resolve();folder,state=load(root,ident)
    with stopped(root,tasks):
        if state['phase'] not in ('applying','applied'):raise ValueError('No applied release to restore')
        if state['phase']=='applied' and any(sha(safe_file(root,name).read_bytes())!=expected for name,expected in state['files'].items()):raise ValueError('Files changed after release; inspect before rollback')
        rollback_files(root,folder,state);state['phase']='rolled_back';atomic_json(folder/'release.json',state)
    return state


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','apply','rollback'));parser.add_argument('--root',default=str(Path(__file__).resolve().parent));parser.add_argument('--tasks-dir');parser.add_argument('--source');parser.add_argument('--release')
    args=parser.parse_args()
    if args.action=='prepare':
        if not args.source:parser.error('--source is required')
        result=prepare(args.root,args.source)
    else:
        if not args.release:parser.error('--release is required')
        result=(apply if args.action=='apply' else rollback)(args.root,args.tasks_dir or str(Path(args.root)/'tasks'),args.release)
    print(json.dumps(dict(id=result['id'],phase=result['phase'])))


if __name__=='__main__':main()
