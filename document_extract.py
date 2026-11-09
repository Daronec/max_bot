"""Bounded text extraction subprocess used by the background knowledge importer."""
import json
from pathlib import Path
import sys
import os
import subprocess


def extract(path):
    path=Path(path)
    if path.suffix.lower()!='.pdf':
        data=path.read_bytes()
        if len(data)>512*1024: raise ValueError('Текст превышает 512 КБ')
        return data.decode('utf-8')
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError('Для PDF установите requirements-rag.txt') from None
    reader=PdfReader(path)
    if reader.is_encrypted: raise ValueError('Зашифрованный PDF: загрузите расшифрованную копию')
    if len(reader.pages)>200: raise ValueError('PDF превышает 200 страниц')
    pages=[];size=0;needs_ocr=False
    for index,page in enumerate(reader.pages):
        content=page.get_contents()
        if content is not None and len(content.get_data())>4*1024*1024:
            raise ValueError('Слишком сложная страница PDF')
        text=page.extract_text() or ''
        needs_ocr = needs_ocr or not text.strip()
        size+=len(text.encode('utf-8'))
        if size>480*1024: raise ValueError('Извлеченный PDF превышает 480 КБ')
        pages.append(f'\n[Страница {index+1}]\n'+text)
    if needs_ocr:
        engine=Path(__file__).resolve().parent/'dist/document-ocr'
        if os.environ.get('RAG_OCR_ENABLED','0').lower() in ('1','true','yes') and engine.is_file():
            result=subprocess.run([str(engine),str(path)],capture_output=True,text=True,timeout=25)
            payload=json.loads(result.stdout)
            if result.returncode or not isinstance(payload.get('text'),str):raise ValueError('Локальный OCR не завершен')
            if len(payload['text'].encode('utf-8'))>512*1024:raise ValueError('Текст OCR превышает лимит')
            return payload['text']
        if not any(len(p.split(']\n',1)[-1].strip()) for p in pages):
            raise ValueError('PDF без текстового слоя: требуется локальный OCR')
        pages.append('[Некоторые страницы без текстового слоя: требуется OCR для полной индексации]')
    return '\n'.join(pages)


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(20,20))
        # Address-space enforcement is supported on Linux; macOS may reject it.
        try: resource.setrlimit(resource.RLIMIT_AS,(1024**3,1024**3))
        except (ValueError,OSError): pass
        text=extract(sys.argv[1])
        print(json.dumps(dict(text=text),ensure_ascii=False))
    except Exception as error:
        print(json.dumps(dict(error=str(error)[:500]),ensure_ascii=False))
        sys.exit(1)


if __name__=='__main__':main()
