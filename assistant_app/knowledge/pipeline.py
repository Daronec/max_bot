"""Owner-selected folder sources. Import runs outside HTTP in the worker process."""
import hashlib
import os
from pathlib import Path
import time
import uuid

from .extract import MAX_PDF_BYTES, SUPPORTED_SUFFIXES, extract_isolated


class KnowledgePipeline:
    def __init__(self, knowledge):
        self.knowledge=knowledge;self.store=knowledge.store
        with self.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_sources (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL,
                next_scan REAL NOT NULL, error TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_source_files (
                source TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
                document TEXT NOT NULL, error TEXT, PRIMARY KEY(source,path))''')
            if 'owner' not in {row[1] for row in db.execute('PRAGMA table_info(knowledge_sources)')}:
                db.execute("ALTER TABLE knowledge_sources ADD COLUMN owner TEXT NOT NULL DEFAULT 'local_owner'")

    def list(self,owner=None):
        with self.store.connect() as db:
            sources=[dict(id=r[0],path=r[1],enabled=bool(r[2]),next_scan=r[3],error=r[4],owner=r[5]) for r in db.execute('SELECT * FROM knowledge_sources') if owner is None or r[5]==owner]
            for source in sources:
                source['files']=[dict(path=r[0],document_id=r[1],error=r[2]) for r in db.execute('SELECT path,document,error FROM knowledge_source_files WHERE source=?',(source['id'],))]
            return sources

    def add(self,path,owner='local_owner'):
        if not isinstance(path,str) or not Path(path).is_absolute(): raise ValueError('Укажите абсолютный путь папки')
        base=Path(path)
        if base.is_symlink() or not base.is_dir() or base.resolve() in (Path('/'),Path.home()): raise ValueError('Выберите отдельную папку с документами')
        if any(part.startswith('.') for part in base.parts): raise ValueError('Скрытые папки не индексируются')
        ident=uuid.uuid4().hex
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM knowledge_sources WHERE path=?',(str(base.resolve()),)).fetchone(): raise ValueError('Папка уже добавлена')
            db.execute('INSERT INTO knowledge_sources(id,path,enabled,next_scan,error,owner) VALUES(?,?,1,0,NULL,?)',(ident,str(base.resolve()),owner))
        return ident

    def toggle(self,ident,enabled,owner=None):
        if not isinstance(enabled,bool): raise ValueError('enabled должен быть логическим')
        with self.store.connect() as db:
            row=db.execute('SELECT owner FROM knowledge_sources WHERE id=?',(ident,)).fetchone()
            if not row or owner is not None and row[0]!=owner: raise KeyError(ident)
            db.execute('UPDATE knowledge_sources SET enabled=?,next_scan=0 WHERE id=?',(int(enabled),ident))
            if enabled:db.execute("UPDATE knowledge_source_files SET digest='' WHERE source=?",(ident,))

    @staticmethod
    def scan(base):
        found={};count=0
        for directory,folders,files in os.walk(base,followlinks=False):
            folders[:]=sorted(f for f in folders if not f.startswith('.') and not (Path(directory)/f).is_symlink())
            for name in sorted(files):
                count+=1
                if count>5000: raise ValueError('В папке более 5000 файлов; выберите меньшую папку')
                path=Path(directory)/name
                if name.startswith('.') or path.suffix.lower() not in SUPPORTED_SUFFIXES or path.is_symlink(): continue
                # Check every path component before opening; no traversal outside chosen root.
                if not path.resolve().is_relative_to(base.resolve()): continue
                found[path.relative_to(base).as_posix()]=path
        return found

    def tick(self,now=None):
        now=time.time() if now is None else now
        with self.store.connect() as db:
            source=db.execute('SELECT id,path,owner FROM knowledge_sources WHERE enabled=1 AND next_scan<=? ORDER BY next_scan LIMIT 1',(now,)).fetchone()
            if not source:return False
            db.execute('UPDATE knowledge_sources SET next_scan=? WHERE id=?',(now+30,source[0]))
        ident,root,owner=source; base=Path(root)
        try:
            if base.is_symlink() or not base.is_dir(): raise ValueError('Папка источника недоступна')
            found=self.scan(base)
            with self.store.connect() as db:
                known={r[0]:dict(digest=r[1],document=r[2],error=r[3]) for r in db.execute('SELECT path,digest,document,error FROM knowledge_source_files WHERE source=?',(ident,))}
            for relative,old in known.items():
                if relative not in found:
                    try:self.knowledge.delete(owner,old['document'])
                    except KeyError:pass
                    with self.store.connect() as db:db.execute('DELETE FROM knowledge_source_files WHERE source=? AND path=?',(ident,relative))
            changed=False
            for relative,path in found.items():
                old=known.get(relative)
                if path.stat().st_size>MAX_PDF_BYTES: fingerprint='oversize:'+str(path.stat().st_mtime_ns)
                else: fingerprint=hashlib.sha256(path.read_bytes()).hexdigest()
                if old and old['digest']==fingerprint:continue
                document=old['document'] if old else hashlib.sha256((ident+':'+relative).encode()).hexdigest()[:32]
                with self.store.connect() as db:
                    deleted=db.execute('SELECT 1 FROM metadata WHERE key=?',('deleted_document:'+document,)).fetchone()
                if deleted:continue  # Explicit deletion must not be undone by a folder watcher.
                error=None
                try:
                    if fingerprint.startswith('oversize:'):raise ValueError('Файл превышает 20 МБ')
                    # Open the selected file as a descriptor, prevent a final symlink swap.
                    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                    with os.fdopen(descriptor, 'rb') as stream:
                        data = stream.read(MAX_PDF_BYTES + 1)
                    if len(data) > MAX_PDF_BYTES:
                        raise ValueError('Файл превышает 20 МБ')
                    fingerprint = hashlib.sha256(data).hexdigest()
                    text = extract_isolated(data, path.suffix.lower())
                    try:self.knowledge.document(owner,document);existing=True
                    except KeyError:existing=False
                    self.knowledge.ingest(owner,path.name,text,document if existing else None,new_document_id=None if existing else document)
                except (ValueError,OSError) as exc:
                    error=str(exc)[:500]
                    with self.store.connect() as db:db.execute("UPDATE knowledge_documents SET status='source_error' WHERE id=?",(document,))
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO knowledge_source_files VALUES(?,?,?,?,?)',(ident,relative,fingerprint,document,error))
                    db.execute('UPDATE knowledge_sources SET next_scan=? WHERE id=?',(now+1,ident))
                changed=True
                break  # Bound one extraction per worker iteration.
            with self.store.connect() as db:db.execute('UPDATE knowledge_sources SET error=NULL WHERE id=?',(ident,))
            return changed
        except (ValueError,OSError) as exc:
            with self.store.connect() as db:db.execute('UPDATE knowledge_sources SET error=? WHERE id=?',(str(exc)[:500],ident))
            return False
