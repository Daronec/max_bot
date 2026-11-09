"""Owner data export, conversation deletion, and bounded offline file retention."""
import json
from pathlib import Path
import time

from task_service import TaskConflict


def delete_conversation(store,conversation,owner='local_owner'):
    with store.connect() as db,store.transaction(db):
        if not db.execute('SELECT 1 FROM conversations WHERE id=? AND owner=?',(conversation,owner)).fetchone():raise KeyError(conversation)
        tasks=[store.decode(row) for row in db.execute('SELECT revision,payload FROM tasks')]
        related=[task for task in tasks if task.get('conversation_id')==conversation]
        if any(task['status'] in ('queued','processing','sending') or task.get('delivery_inflight') or task.get('delivery_uncertain') or not task.get('delivered',True) for task in related):
            raise TaskConflict('Сначала завершите запуск и разрешите доставку')
        for task in related:db.execute('DELETE FROM tasks WHERE task_id=?',(task['task_id'],))
        db.execute('DELETE FROM messages WHERE conversation_id=?',(conversation,))
        db.execute('DELETE FROM conversation_projects WHERE conversation=?',(conversation,))
        db.execute('DELETE FROM conversations WHERE id=?',(conversation,))
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'channel_links' in tables:db.execute("UPDATE channel_links SET conversation=NULL WHERE conversation=?",(conversation,))
        if 'conversation_rollups' in tables:db.execute('DELETE FROM conversation_rollups WHERE conversation=?',(conversation,))
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('deleted_conversation:'+conversation,'true'))
    for task in related:
        (store.directory/(task['task_id']+'.json')).unlink(missing_ok=True)
    return len(related)


def export_data(runtime):
    store=runtime.task_store()
    conversations=store.conversations('local_owner')
    projects=runtime.project_context().list()
    knowledge=[]
    for scope in ['local_owner',*['project:'+project['id'] for project in projects]]:
        for doc in runtime.knowledge_store().list(scope):
            knowledge.append(dict(scope=scope,**runtime.knowledge_store().document(scope,doc['id'])))
    return dict(format='assistant-personal-data-v1',conversations=[dict(c,messages=store.messages(c['id'],'local_owner',limit=1000000)) for c in conversations],
                runs=store.all(),memory=runtime.desktop_memory().facts(),projects=projects,knowledge=knowledge,
                automations=runtime.automation_service().list(),background_settings=runtime.background_settings().get(),channel_links=runtime.identity_service().links(),models=runtime.model_registry().get(),settings=runtime.assistant_settings().get())


def retention_candidates(root,tasks,days,now=None):
    if not isinstance(days,int) or isinstance(days,bool) or not 1<=days<=3650:raise ValueError('Retention: 1–3650 days')
    root,tasks=Path(root).resolve(),Path(tasks).resolve();cutoff=(time.time() if now is None else now)-days*86400
    result=[]
    for directory,suffix in ((root/'logs','.log'),(tasks/'audio',None),(tasks/'speech','.mp3')):
        if not directory.is_dir() or directory.is_symlink():continue
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and (suffix is None or path.suffix==suffix) and path.stat().st_mtime<cutoff:
                result.append(path)
    return result


def main():
    import argparse,fcntl
    from contextlib import ExitStack
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',default=str(Path(__file__).resolve().parent));parser.add_argument('--tasks-dir');parser.add_argument('--days',type=int,default=30);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();tasks=Path(args.tasks_dir or str(Path(args.root)/'tasks'))
    with ExitStack() as stack:
        if args.apply:
            for name in ('.api.lock','.worker.lock'):
                guard=stack.enter_context((tasks/name).open('a'))
                try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise RuntimeError('Stop API and worker before retention cleanup') from None
        candidates=retention_candidates(args.root,tasks,args.days)
        if args.apply:
            for path in candidates:path.unlink()
        print(json.dumps(dict(applied=args.apply,files=[str(p) for p in candidates]),ensure_ascii=False))


if __name__=='__main__':main()
