"""Offline retrieval regression benchmark. Synthetic fixtures, never personal documents."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import tempfile
import time

from assistant_app.knowledge.index import KnowledgeStore
from assistant_app.core.task_store import SQLiteTaskStore
from assistant_app.core.task_service import record_lifecycle


FIXTURES=[
    ('orion','orion.md','Код проекта Орион: аметист. Ответственный инженер: Олег.','Какой код проекта Орион?'),
    ('vega','vega.md','Код проекта Вега: сапфир. Ответственный инженер: Мария.','Кто ответственный инженер проекта Вега?'),
    ('travel','travel.md','Поездка в Казань запланирована на 15 декабря. Номер поезда: 024.','Когда поездка в Казань?'),
    ('network','network.md','Сервер резервного копирования доступен на порту 8822.','Какой порт сервера резервного копирования?'),
]


def retrieval():
    with tempfile.TemporaryDirectory(prefix='assistant-eval-') as directory:
        knowledge=KnowledgeStore(SQLiteTaskStore(directory))
        expected={key:knowledge.ingest('evaluation',title,text)['id'] for key,title,text,_ in FIXTURES}
        knowledge.ingest('other-owner','private.md','Орион Вега Казань порт сервера: чужие личные документы.')
        timings=[];ranks=[];isolation=True;citations=True
        for key,_,_,query in FIXTURES:
            started=time.perf_counter();hits=knowledge.search('evaluation',query);timings.append(time.perf_counter()-started)
            ids=[hit['document_id'] for hit in hits]
            ranks.append(1/(ids.index(expected[key])+1) if expected[key] in ids else 0)
            isolation &= all(hit['document_id'] in expected.values() for hit in hits)
            for hit in hits:
                doc=knowledge.document('evaluation',hit['document_id'])
                chunk=next(c for c in doc['chunks'] if c['index']==hit['index'])
                citations &= chunk['content']==hit['content'] and chunk['line_start']==hit['line_start']
        missing=knowledge.search('evaluation','несуществующий_маркер_xyz')
        result=dict(dataset='synthetic-retrieval-v1',cases=len(FIXTURES),recall_at_5=sum(r>0 for r in ranks)/len(ranks),
                    mean_reciprocal_rank=statistics.mean(ranks),scope_isolation=isolation,citation_provenance=citations,
                    abstains_without_matches=not missing,median_seconds=statistics.median(timings),
                    limits='Lexical synthetic regression; does not measure model answer truthfulness or real-world semantic retrieval')
        result['passed']=result['recall_at_5']==1 and isolation and citations and not missing
        return result


def storage_load(count=500):
    with tempfile.TemporaryDirectory(prefix='assistant-load-') as directory:
        store=SQLiteTaskStore(directory)
        def register(index):
            task=dict(task_id='load_'+str(index),message='synthetic',source='desktop',chat_id='load',status='queued',result=None)
            record_lifecycle(task);store.save(task)
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(register,range(count)))
        elapsed=time.perf_counter()-started
        return dict(dataset='synthetic-store-v1',writers=4,tasks=count,stored=len(store.all()),seconds=elapsed,tasks_per_second=count/elapsed,passed=len(store.all())==count)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--load',action='store_true');parser.add_argument('--output')
    args=parser.parse_args();result=dict(retrieval=retrieval())
    if args.load:result['storage_load']=storage_load()
    result['passed']=all(section['passed'] for section in result.values())
    data=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:Path(args.output).write_text(data)
    print(data)
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
