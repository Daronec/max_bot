"""Local ASR transport and on-demand TTS, without remote model uploads."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

def transcribe_local(path):
    from .recognition import transcribe_local as recognize
    return recognize(path)


def transcribe_task(task, store, telegram_token):
    from .recognition import VoiceError
    if task.get('telegram_audio'):
        from assistant_app.channels.telegram import TelegramClient, TelegramError
        try:
            with tempfile.TemporaryDirectory(prefix='telegram-voice-') as directory:
                path=Path(directory)/'voice.audio'
                TelegramClient(telegram_token).download_audio(task['telegram_audio'],path)
                return transcribe_local(path)
        except TelegramError:
            raise VoiceError('Не удалось скачать аудио Telegram. Повторите запись или отправьте текст.') from None
    ident=task.get('audio_upload')
    if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):raise VoiceError('Некорректное вложение')
    path=store.directory/'audio'/ident
    try:
        if path.is_symlink() or not path.is_file():raise VoiceError('Аудиофайл недоступен. Отправьте запись снова.')
        return transcribe_local(path)
    finally:path.unlink(missing_ok=True)


def synthesize_task(task,store,root):
    source=store.get(task['speech_source'])
    if not source or source.get('status')!='completed':raise ValueError('Нет завершенного ответа для озвучивания')
    if task.get('source')=='desktop':
        if source.get('source')!='desktop':raise ValueError('Ответ недоступен локальному запросу озвучивания')
    elif (source.get('delivery_channel',source.get('source'))!=task.get('source')
          or str(source.get('chat_id'))!=str(task.get('chat_id')) or str(source.get('sender_id'))!=str(task.get('sender_id'))):
        raise ValueError('Озвучить можно только ответ тому же отправителю в том же чате')
    return synthesize_reply(source['result'], task['task_id'], store, root)


def synthesize_reply(text, task_id, store, root):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task_id):
        raise ValueError('Некорректный идентификатор озвучки')
    root = Path(root).resolve()
    interpreter = root / '.venv-tts/bin/python'
    if not interpreter.is_file():raise ValueError('Не установлено локальное окружение TTS; используйте текстовый ответ')
    result = subprocess.run(
        [str(interpreter), '-m', 'assistant_app.speech.service', 'synthesize'],
        cwd=root, input=json.dumps(dict(text=text[:600])), text=True, capture_output=True, timeout=180,
    )
    try:payload=json.loads(result.stdout)
    except ValueError:raise ValueError('Локальный синтез не завершен; текстовый ответ сохранен') from None
    if result.returncode or payload.get('error'):raise ValueError('Локальная модель TTS или профиль голоса недоступны; текстовый ответ сохранен')
    from .clone import settings
    _,voices=settings()
    output=Path(payload['output']).resolve()
    if not output.is_relative_to((voices/'generated').resolve()) or output.suffix!='.mp3' or not output.is_file():raise ValueError('Некорректный результат TTS')
    target=store.directory/'speech';target.mkdir(exist_ok=True,mode=0o700)
    destination=target/(task_id+'.mp3')
    shutil.copyfile(output,destination);destination.chmod(0o600);output.unlink()
    return destination.name


if __name__=='__main__':
    try:
        import contextlib
        from .clone import settings,synthesize
        values=json.load(sys.stdin)
        model,voices=settings()
        with contextlib.redirect_stdout(sys.stderr):result=synthesize(model,voices,values['text'])
        import imageio_ffmpeg
        wave=Path(result['output']);audio=wave.with_suffix('.mp3')
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-hide_banner','-loglevel','error','-i',str(wave),'-codec:a','libmp3lame','-b:a','96k',str(audio)],check=True,timeout=30,capture_output=True)
        audio.chmod(0o600);wave.unlink();result['output']=str(audio)
        print(json.dumps(result))
    except Exception:
        print(json.dumps(dict(error='Local speech synthesis unavailable')))
        sys.exit(1)
