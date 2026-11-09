"""Offline restore and consistent backup of the assistant task database.

The snapshot covers tasks, desktop conversations, settings and knowledge. Existing
personal memory, voices and other filesystem data require separate backups.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
from task_service import record_lifecycle

from task_store import SQLiteTaskStore


def restore(directory, snapshot):
    directory, snapshot = Path(directory), Path(snapshot)
    database = directory / 'assistant.sqlite3'
    if not database.is_file() or not snapshot.is_file() or snapshot.resolve() == database.resolve():
        raise ValueError('Specify an existing database and a separate snapshot')
    with ExitStack() as stack:
        for name in ('.api.lock', '.worker.lock'):
            guard = stack.enter_context((directory / name).open('a'))
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Stop the API and worker before restoring') from None
        # Validate a copy read-only before touching the current database.
        from urllib.parse import quote
        source = sqlite3.connect('file:' + quote(str(snapshot.resolve())) + '?mode=ro', uri=True)
        stack.callback(source.close)
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Snapshot integrity check failed')
        if not 1 <= source.execute('PRAGMA user_version').fetchone()[0] <= SQLiteTaskStore.SCHEMA_VERSION:
            raise ValueError('Unsupported snapshot schema')
        if not source.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
            raise ValueError('Not an assistant snapshot')
        store = SQLiteTaskStore(directory)
        with store.connect() as current:
            deletions = current.execute("SELECT key,value FROM metadata WHERE key LIKE 'deleted_document:%'").fetchall()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        recovery = directory / ('before-restore-' + stamp + '.sqlite3')
        store.backup(recovery)
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=directory) as staging:
            staged = sqlite3.connect(Path(staging) / 'staged.sqlite3')
            try:
                source.backup(staged)
                with staged:
                    has_knowledge = staged.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_documents'").fetchone()
                    for key, value in deletions:
                        ident = key.split(':', 1)[1]
                        if has_knowledge:
                            staged.execute('DELETE FROM knowledge_fts WHERE chunk_id IN (SELECT id FROM knowledge_chunks WHERE document_id=?)', (ident,))
                            staged.execute('DELETE FROM knowledge_chunks WHERE document_id=?', (ident,))
                            staged.execute('DELETE FROM knowledge_documents WHERE id=?', (ident,))
                        staged.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, value))
                    # Effects performed after the snapshot cannot be inferred from
                    # historical state. Restored pending work needs manual review.
                    rows = staged.execute('SELECT task_id,revision,payload FROM tasks').fetchall()
                    for ident, revision, payload in rows:
                        task = json.loads(payload)
                        if task['status'] in ('queued', 'processing', 'sending'):
                            task.update(status='failed', error='Восстановлено из копии; автоматический запуск остановлен. Проверьте ранее выполненные действия.')
                            record_lifecycle(task)
                        elif task['status'] == 'completed' and not task.get('delivered', True):
                            task.update(delivery_inflight=False, delivery_uncertain=True)
                        staged.execute('UPDATE tasks SET status=?,revision=?,payload=? WHERE task_id=?',
                            (task['status'], revision + 1, json.dumps(task, ensure_ascii=False), ident))
                    tables = {row[0] for row in staged.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    with store.connect() as current:
                        removed = current.execute("SELECT key,value FROM metadata WHERE key LIKE 'deleted_conversation:%'").fetchall()
                    for key, value in removed:
                        conversation = key.split(':', 1)[1]
                        for task_id, payload in staged.execute('SELECT task_id,payload FROM tasks').fetchall():
                            if json.loads(payload).get('conversation_id') == conversation:
                                staged.execute('DELETE FROM tasks WHERE task_id=?', (task_id,))
                        staged.execute('DELETE FROM messages WHERE conversation_id=?', (conversation,))
                        staged.execute('DELETE FROM conversations WHERE id=?', (conversation,))
                        if 'conversation_projects' in tables:
                            staged.execute('DELETE FROM conversation_projects WHERE conversation=?', (conversation,))
                        if 'conversation_rollups' in tables:
                            staged.execute('DELETE FROM conversation_rollups WHERE conversation=?', (conversation,))
                        staged.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, value))
                    if 'automations' in tables:
                        staged.execute('UPDATE automations SET enabled=0,revision=revision+1')
                    if 'knowledge_sources' in tables:
                        staged.execute('UPDATE knowledge_sources SET enabled=0')
                    if 'channel_links' in tables:
                        staged.execute("UPDATE channel_links SET state='revoked',conversation=NULL")
                    if 'pairing_codes' in tables:
                        staged.execute('DELETE FROM pairing_codes')
                    staged.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('background_settings', json.dumps(dict(version=0, enabled=dict(summary=False,mood=False,notifications=False)))))
                    # Legacy exports are never authoritative after an explicit restore.
                    staged.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('json_migration_v1',
                        json.dumps(dict(imported=0, existing=len(rows), rejected=[], backup=str(recovery), restored=True))))
                with store.connect() as target:
                    staged.backup(target)
            finally:
                staged.close()
        # Upgrade an older supported snapshot before its next use.
        SQLiteTaskStore(directory)
        return recovery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'restore'))
    parser.add_argument('--tasks-dir', default=str(Path(__file__).resolve().parent / 'tasks'))
    parser.add_argument('--file', required=True)
    args = parser.parse_args()
    if args.action == 'backup':
        if not (Path(args.tasks_dir) / 'assistant.sqlite3').is_file():
            parser.error('Task database does not exist')
        SQLiteTaskStore(args.tasks_dir).backup(args.file)
        print('Snapshot created and verified')
    else:
        recovery = restore(args.tasks_dir, args.file)
        print(json.dumps(dict(restored=True, recovery=str(recovery))))


if __name__ == '__main__':
    main()
