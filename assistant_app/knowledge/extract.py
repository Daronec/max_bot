"""Bounded text extraction subprocess used by the background knowledge importer."""
import json
from pathlib import Path
import sys
import os
import subprocess
import tempfile

from assistant_app.paths import PROJECT_ROOT

RICH_TEXT_SUFFIXES = ('.rtf', '.docx')
SUPPORTED_SUFFIXES = ('.txt', '.md', '.markdown', '.pdf', *RICH_TEXT_SUFFIXES)
MAX_TEXT_BYTES = 512 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024


def upload_limit(suffix):
    return MAX_PDF_BYTES if suffix in ('.pdf', *RICH_TEXT_SUFFIXES) else MAX_TEXT_BYTES


def extract_isolated(data, suffix):
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError('Выберите файл TXT, Markdown, RTF, DOCX или PDF.')
    limit = upload_limit(suffix)
    if len(data) > limit:
        raise ValueError('Файл превышает 20 МБ.' if limit == MAX_PDF_BYTES else 'Файл превышает 512 КБ.')
    try:
        with tempfile.TemporaryDirectory(prefix='knowledge-extract-') as temporary:
            copy = Path(temporary) / ('document' + suffix)
            copy.write_bytes(data)
            copy.chmod(0o600)
            result = subprocess.run(
                [sys.executable, '-m', 'assistant_app.knowledge.extract', str(copy)],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
            )
            payload = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ValueError('Извлечение текста не завершено.') from error
    if not isinstance(payload, dict) or result.returncode or not isinstance(payload.get('text'), str):
        raise ValueError(payload.get('error', 'Ошибка извлечения текста.') if isinstance(payload, dict) else 'Ошибка извлечения текста.')
    return payload['text']


def extract_rich_text(path, suffix):
    converter = Path('/usr/bin/textutil')
    if not converter.is_file():
        raise ValueError('Для RTF и DOCX требуется macOS. Загрузите копию в TXT или PDF.')
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [str(converter), '-noload', '-format', suffix[1:], '-convert', 'txt',
             '-encoding', 'UTF-8', '-stdout', str(path)],
            stdout=output, stderr=subprocess.PIPE, timeout=25,
        )
        if result.returncode or result.stderr or not output.tell():
            raise ValueError('Не удалось прочитать документ. Проверьте формат или сохраните новую копию.')
        output.seek(0)
        data = output.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError('Извлеченный текст превышает 512 КБ.')
    return data.decode('utf-8')


def extract(path):
    path=Path(path)
    suffix = path.suffix.lower()
    if suffix in RICH_TEXT_SUFFIXES:
        return extract_rich_text(path, suffix)
    if suffix!='.pdf':
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
        engine=PROJECT_ROOT/'dist/document-ocr'
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
