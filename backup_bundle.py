"""Offline portable bundle/export. Secrets and downloaded model weights are excluded."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import shutil
import tempfile
import zipfile


MAX_BUNDLE_BYTES = 2 * 1024**3


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def snapshot_database(source, target):
    from urllib.parse import quote
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect('file:'+quote(str(source.resolve()))+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)
        if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid database: '+source.name)
    target.chmod(0o600)


def create(root, tasks, destination, memory=None, artifacts=()):
    root, tasks, destination = Path(root).resolve(), Path(tasks).resolve(), Path(destination).resolve()
    if destination.exists(): raise ValueError('Bundle already exists')
    if not (tasks/'assistant.sqlite3').is_file(): raise ValueError('Task database is missing')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        for name in ('.api.lock','.worker.lock'):
            guard=stack.enter_context((tasks/name).open('a'))
            try: fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Stop API and worker for a consistent full bundle') from None
        staging=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='assistant-bundle-')))
        files={}
        def add(source,relative):
            if source.is_symlink() or not source.is_file(): raise ValueError('Symlinks and special files are excluded')
            target=staging/relative; target.parent.mkdir(parents=True,exist_ok=True)
            if source.suffix in ('.sqlite3','.db'):
                snapshot_database(source,target)
            else:
                with source.open('rb') as src,target.open('xb') as dst:
                    for chunk in iter(lambda:src.read(1024*1024),b''): dst.write(chunk)
                target.chmod(0o600)
            files[relative]=dict(sha256=digest(target),bytes=target.stat().st_size)
            if sum(f['bytes'] for f in files.values()) > MAX_BUNDLE_BYTES: raise ValueError('Bundle exceeds 2 GiB')
        # Explicit data locations; never sweep tasks/legacy-backups or secrets.
        for name in ('assistant.sqlite3','personal-memory.sqlite3'):
            source=tasks/name
            if source.is_file(): add(source,'data/'+source.name)
        memory=Path(memory) if memory else root/'memory'/'personal.sqlite3'
        if memory.is_file(): add(memory,'memory/personal.sqlite3')
        for name in ('mood','summaries','notifications'):
            for source in sorted((root/name).glob('*.sqlite3')):
                add(source,'state/'+name+'/'+source.name)
        for name in ('audio','speech'):
            for source in sorted((tasks/name).glob('*')):
                if source.is_file():add(source,'media/'+name+'/'+source.name)
        # Preserve the indexed originals only, not unrelated files in watched folders.
        with sqlite3.connect(tasks/'assistant.sqlite3') as db:
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {'knowledge_source_files','knowledge_sources','knowledge_documents'}<=tables:
                originals=db.execute('''SELECT s.id,s.path,f.path,f.digest FROM knowledge_source_files f
                    JOIN knowledge_sources s ON s.id=f.source JOIN knowledge_documents d ON d.id=f.document''').fetchall()
                for ident,base,relative,expected in originals:
                    source=Path(base)/relative
                    if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(Path(base).resolve()) and digest(source)==expected:
                        add(source,'documents/'+ident+'/'+relative)
        for pattern in ('*.py','*.md','requirements*.txt','.env.example'):
            for source in sorted(root.glob(pattern)): add(source,'project/'+source.name)
        for folder in ('static','tests','scripts','desktop'):
            base=root/folder
            if base.is_dir():
                for source in sorted(base.rglob('*')):
                    if source.is_file() and not source.is_symlink() and '__pycache__' not in source.parts and source.suffix not in ('.pyc',):
                        add(source,'project/'+source.relative_to(root).as_posix())
        artifacts=list(artifacts)
        if (root/'.voices').is_dir() and root/'.voices' not in [Path(p).resolve() for p in artifacts]:
            artifacts.insert(0,root/'.voices')
        for index,folder in enumerate(artifacts):
            base=Path(folder).resolve()
            if not base.is_dir(): raise ValueError('Artifact directory is missing')
            for source in sorted(base.rglob('*')):
                if source.is_symlink(): raise ValueError('Artifact symlinks are excluded')
                if source.is_file(): add(source,f'artifacts/{index}/'+source.relative_to(base).as_posix())
        manifest=dict(format=1,created_at=datetime.now(timezone.utc).isoformat(),files=files,
                      excludes=['credentials','downloaded_models','logs','legacy_backups'],
                      artifact_names=[Path(p).name for p in artifacts])
        descriptor=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            with os.fdopen(descriptor,'wb') as output,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
                for relative in files: archive.write(staging/relative,relative)
            with destination.open('rb') as stream: os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return manifest


def extract(bundle, destination):
    """Export to a NEW directory only. Never overwrite the running installation."""
    destination=Path(destination).resolve()
    if destination.exists(): raise ValueError('Use a new empty destination path')
    with zipfile.ZipFile(bundle) as archive:
        infos=archive.infolist()
        if sum(i.file_size for i in infos)>MAX_BUNDLE_BYTES or len(infos)>50000: raise ValueError('Bundle is too large')
        names=[i.filename for i in infos]
        if len(set(names))!=len(names) or 'manifest.json' not in names: raise ValueError('Invalid bundle inventory')
        for info in infos:
            path=PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in info.filename or (info.external_attr >> 16)&0o170000 == 0o120000:
                raise ValueError('Unsafe archive path')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('format')!=1 or not isinstance(manifest.get('files'),dict) or set(names)!=(set(manifest['files'])|{'manifest.json'}):
            raise ValueError('Invalid bundle manifest')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.bundle-restore-',dir=destination.parent) as temporary:
            staged=Path(temporary)/'restored'; staged.mkdir(mode=0o700)
            for relative,expected in manifest['files'].items():
                target=staged/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with archive.open(relative) as source,target.open('xb') as output:
                    for block in iter(lambda:source.read(1024*1024),b''):output.write(block)
                target.chmod(0o600)
                if target.stat().st_size!=expected['bytes'] or digest(target)!=expected['sha256']:
                    raise ValueError('Bundle hash mismatch')
            # Restore to a portable tree; inspect/export or move explicitly later.
            (staged/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
            (staged/'manifest.json').chmod(0o600)
            staged.rename(destination)
    return manifest


def prepare_installation(bundle,destination):
    """Restore a new installation; dependencies and credentials stay separate."""
    destination=Path(destination).resolve()
    if destination.exists():raise ValueError('Use a new installation directory')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.assistant-install-',dir=destination.parent) as temporary:
        exported=Path(temporary)/'export';manifest=extract(bundle,exported)
        staged=Path(temporary)/'installation';staged.mkdir(mode=0o700)
        from project_changes import source_path
        from storage_admin import restore
        from task_store import SQLiteTaskStore
        for relative in manifest['files']:
            parts=PurePosixPath(relative).parts
            target=None
            if parts[0]=='project':
                name='/'.join(parts[1:]);source_path(name);target=staged/name
            elif relative=='data/personal-memory.sqlite3':target=staged/'tasks/personal-memory.sqlite3'
            elif relative=='memory/personal.sqlite3':target=staged/'memory/personal.sqlite3'
            elif parts[0]=='media' and len(parts)==3 and parts[1] in ('audio','speech'):
                target=staged/'tasks'/parts[1]/parts[2]
            elif parts[0]=='documents':target=staged/'restored-documents'/Path(*parts[1:])
            elif parts[0]=='state' and len(parts)==3 and parts[1] in ('mood','summaries','notifications') and parts[2].endswith('.sqlite3'):
                target=staged/parts[1]/parts[2]
            elif parts[0]=='artifacts':
                index=int(parts[1]);name=manifest['artifact_names'][index]
                base=staged/'.voices' if name=='.voices' else staged/'imported-artifacts'/str(index)
                target=base.joinpath(*parts[2:])
            if target:
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                shutil.copyfile(exported/relative,target);target.chmod(0o600)
        if not (exported/'data/assistant.sqlite3').is_file():raise ValueError('Assistant database missing')
        store=SQLiteTaskStore(staged/'tasks')
        restore(store.directory,exported/'data/assistant.sqlite3')
        (staged/'RESTORED_INSTALLATION.md').write_text('Install dependencies and configure credentials separately. Schedules, channel links and folder watchers are disabled.\n')
        staged.rename(destination)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('create','extract','prepare-installation'))
    parser.add_argument('--file',required=True)
    parser.add_argument('--root',default=str(Path(__file__).resolve().parent))
    parser.add_argument('--tasks-dir')
    parser.add_argument('--memory')
    parser.add_argument('--artifacts',action='append',default=[])
    parser.add_argument('--destination')
    args=parser.parse_args()
    if args.action=='create':
        result=create(args.root,args.tasks_dir or str(Path(args.root)/'tasks'),args.file,args.memory,args.artifacts)
    else:
        if not args.destination: parser.error('--destination is required')
        result=(prepare_installation if args.action=='prepare-installation' else extract)(args.file,args.destination)
    print(json.dumps(dict(files=len(result['files']),format=result['format'])))


if __name__=='__main__': main()
