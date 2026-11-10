"""Local text knowledge index: scoped FTS plus optional local embeddings."""
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from .embeddings import E5Embeddings, EMBEDDING_ERRORS
from .extract import SUPPORTED_SUFFIXES


def chunks(text, size=1200, overlap=200):
    position = 0
    index = 0
    while position < len(text):
        end = min(position + size, len(text))
        yield dict(index=index, content=text[position:end],
                   line_start=text.count('\n', 0, position) + 1,
                   line_end=text.count('\n', 0, end) + 1)
        if end == len(text):
            break
        position = end - overlap
        index += 1


class KnowledgeStore:
    def __init__(self, task_store, embeddings=None):
        self.store = task_store
        self.embeddings = embeddings
        with self.store.connect() as db, self.store.transaction(db):
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_documents (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                sha256 TEXT NOT NULL, version INTEGER NOT NULL, embedding_model TEXT,
                status TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
                embedding TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS knowledge_document_chunks ON knowledge_chunks(document_id)')
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(chunk_id UNINDEXED, content, tokenize='unicode61')")

    def list(self, owner):
        with self.store.connect() as db:
            return [dict(id=row[0], title=row[1], version=row[2], status=row[3], embedding_model=row[4])
                    for row in db.execute('SELECT id,title,version,status,embedding_model FROM knowledge_documents WHERE owner=?', (owner,))]

    def ingest(self, owner, title, text, document_id=None, new_document_id=None):
        if new_document_id is not None and not re.fullmatch(r'[a-f0-9]{32}', new_document_id):
            raise ValueError('Некорректный идентификатор документа')
        if document_id is not None and (not isinstance(document_id, str) or not re.fullmatch(r'[a-f0-9]{32}', document_id)):
            raise ValueError('Некорректный идентификатор документа')
        if (not isinstance(title, str) or len(title) > 200 or Path(title).name != title
                or title.startswith('.') or '\\' in title or Path(title).suffix.lower() not in SUPPORTED_SUFFIXES):
            raise ValueError('Загрузите TXT, Markdown, RTF, DOCX или PDF с обычным именем файла')
        if not isinstance(text, str) or not text.strip() or '\0' in text or len(text.encode('utf-8')) > 512 * 1024:
            raise ValueError('Требуется текст UTF-8 размером до 512 КБ')
        sections = list(chunks(text))
        vectors, status = [], 'text_ready'
        if self.embeddings:
            # Compute outside the write transaction so reads and tasks remain available.
            try:
                for offset in range(0, len(sections), 16):
                    vectors.extend(self.embeddings.embed([chunk['content'] for chunk in sections[offset:offset + 16]]))
                status = 'hybrid_ready'
            except EMBEDDING_ERRORS:
                vectors, status = [], 'embedding_failed_text_ready'
        with self.store.connect() as db, self.store.transaction(db):
            version = 1
            if document_id:
                row = db.execute('SELECT owner,version FROM knowledge_documents WHERE id=?', (document_id,)).fetchone()
                if row is None or row[0] != owner:
                    raise KeyError(document_id)
                version = row[1] + 1
                self._delete_chunks(db, document_id)
            else:
                document_id = new_document_id or uuid.uuid4().hex
                if db.execute('SELECT 1 FROM knowledge_documents WHERE id=?', (document_id,)).fetchone():
                    raise ValueError('Документ уже существует; обновите его версию')
            db.execute('INSERT OR REPLACE INTO knowledge_documents VALUES(?,?,?,?,?,?,?)',
                       (document_id, owner, title, hashlib.sha256(text.encode()).hexdigest(), version,
                        self.embeddings.model if vectors else None, status))
            for index, chunk in enumerate(sections):
                ident = f'{document_id}:{index}'
                db.execute('INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?)',
                           (ident, document_id, index, chunk['content'], chunk['line_start'], chunk['line_end'],
                            json.dumps(vectors[index]) if vectors else None))
                db.execute('INSERT INTO knowledge_fts VALUES(?,?)', (ident, chunk['content']))
        return dict(id=document_id, version=version, chunks=len(sections), status=status)

    @staticmethod
    def _delete_chunks(db, document_id):
        db.execute('DELETE FROM knowledge_fts WHERE chunk_id IN (SELECT id FROM knowledge_chunks WHERE document_id=?)', (document_id,))
        db.execute('DELETE FROM knowledge_chunks WHERE document_id=?', (document_id,))

    def delete(self, owner, document_id):
        with self.store.connect() as db, self.store.transaction(db):
            if not db.execute('SELECT 1 FROM knowledge_documents WHERE id=? AND owner=?', (document_id, owner)).fetchone():
                raise KeyError(document_id)
            self._delete_chunks(db, document_id)
            db.execute('DELETE FROM knowledge_documents WHERE id=?', (document_id,))
            # Retain only a deletion marker for backup-restore reconciliation.
            db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('deleted_document:' + document_id, 'true'))

    def document(self, owner, document_id):
        with self.store.connect() as db:
            row = db.execute('SELECT title,version FROM knowledge_documents WHERE id=? AND owner=?', (document_id, owner)).fetchone()
            if row is None:
                raise KeyError(document_id)
            sections = [dict(index=part[0], content=part[1], line_start=part[2], line_end=part[3]) for part in
                        db.execute('SELECT chunk_index,content,line_start,line_end FROM knowledge_chunks WHERE document_id=? ORDER BY chunk_index', (document_id,))]
            return dict(id=document_id, title=row[0], version=row[1], chunks=sections)

    def search(self, owner, query, limit=5):
        if not isinstance(query, str) or len(query) > 32000:
            raise ValueError('Поисковый запрос превышает допустимый размер')
        if not self.list(owner):
            return []
        terms = re.findall(r'\w+', query, re.UNICODE)[:24]
        if not terms:
            return []
        query_vector = None
        if self.embeddings:
            try:
                options = {'query': True} if isinstance(self.embeddings, E5Embeddings) else {}
                query_vector = self.embeddings.embed([query], **options)[0]
            except EMBEDDING_ERRORS:
                pass
        with self.store.connect() as db:
            # The ownership predicate is inside both retrieval queries.
            fields = 'c.id,c.document_id,d.title,d.version,c.chunk_index,c.content,c.line_start,c.line_end'
            lexical = db.execute(f'''SELECT {fields} FROM knowledge_fts
                JOIN knowledge_chunks c ON c.id=knowledge_fts.chunk_id
                JOIN knowledge_documents d ON d.id=c.document_id
                WHERE knowledge_fts MATCH ? AND d.owner=? AND d.status!='source_error' ORDER BY bm25(knowledge_fts) LIMIT 30''',
                (' OR '.join('"' + term + '"' for term in terms), owner)).fetchall()
            vector_rows = []
            if query_vector is not None:
                candidates = db.execute(f'''SELECT {fields},c.embedding FROM knowledge_chunks c
                    JOIN knowledge_documents d ON d.id=c.document_id WHERE d.owner=? AND d.embedding_model=?
                    AND c.embedding IS NOT NULL AND d.status!='source_error' ''', (owner, self.embeddings.model)).fetchall()
                def similarity(row):
                    vector = json.loads(row[-1])
                    if len(vector) != len(query_vector):
                        return -1
                    denominator = math.sqrt(sum(value * value for value in vector) * sum(value * value for value in query_vector))
                    return sum(a * b for a, b in zip(vector, query_vector)) / denominator if denominator else -1
                ranked = sorted(((similarity(row), row[:-1]) for row in candidates), key=lambda pair: pair[0], reverse=True)
                threshold = .78 if isinstance(self.embeddings, E5Embeddings) else .25
                vector_rows = [row for score, row in ranked[:30] if score >= threshold]
            scores, records, methods = {}, {}, {}
            for method, rows in (('lexical', lexical), ('semantic', vector_rows)):
                for rank, row in enumerate(rows):
                    scores[row[0]] = scores.get(row[0], 0) + 1 / (60 + rank + 1)
                    records[row[0]] = row
                    methods.setdefault(row[0], []).append(method)
            result = []
            for ident in sorted(scores, key=scores.get, reverse=True)[:limit]:
                row = records[ident]
                result.append(dict(id=row[0], document_id=row[1], title=row[2], version=row[3],
                                   index=row[4], content=row[5], line_start=row[6], line_end=row[7], retrieval=methods[ident],
                                   citation=f'[K:{row[0]}]', url=f'/api/knowledge/{row[1]}' + ('?project_id=' + owner[8:] if owner.startswith('project:') else '')))
            return result
