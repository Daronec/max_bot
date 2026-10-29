"""Bounded MAX audio downloads and offline Russian speech recognition."""
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import urllib3
import requests
from config import VOICE_MODEL, VOICE_MODEL_DIR, VOICE_MAX_BYTES, VOICE_MAX_SECONDS, MAX_CA_BUNDLE
from web_tools import public_target
from assistant_identity import ASR_NAME_HINT

_lock = threading.Lock()
_model = None


class VoiceError(RuntimeError):
    pass


def audio_attachment(message):
    attachments = message.get('body', {}).get('attachments') or []
    for attachment in attachments:
        if not isinstance(attachment, dict): continue
        if attachment.get('type') == 'audio': return attachment
        if attachment.get('type') == 'file':
            name = attachment.get('filename') or ''
            if isinstance(name, str) and Path(name).suffix.lower() in ('.mp3','.m4a','.ogg','.opus','.wav','.aac','.flac','.aiff','.aif'):
                return attachment
    return None


def download_audio(url, destination):
    """No bot token is sent to media URLs. Each redirect is validated and IP-pinned."""
    deadline = time.monotonic() + 60
    for _ in range(6):
        parts, host, port, ip = public_target(url)
        if parts.scheme != 'https':
            raise VoiceError('Аудиовложение должно использовать HTTPS.')
        pool = urllib3.HTTPSConnectionPool(host=ip, port=port, server_hostname=host,
            assert_hostname=host, cert_reqs='CERT_REQUIRED',
            ca_certs=MAX_CA_BUNDLE if isinstance(MAX_CA_BUNDLE,str) else requests.certs.where(),
            timeout=urllib3.Timeout(connect=10, read=15), retries=False)
        response = None
        try:
            path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
            response = pool.request('GET', path, headers={'Host':host}, redirect=False, preload_content=False)
            if response.status in (301,302,303,307,308):
                location = response.headers.get('Location')
                if not location: raise VoiceError('Не удалось скачать голосовое сообщение.')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise VoiceError('Не удалось скачать голосовое сообщение. Отправьте его повторно.')
            size = 0
            with open(destination, 'wb') as out:
                for block in response.stream(65536, decode_content=True):
                    size += len(block)
                    if size > VOICE_MAX_BYTES or time.monotonic() > deadline:
                        raise VoiceError('Голосовое сообщение слишком большое или загрузка заняла больше минуты.')
                    out.write(block)
            return
        finally:
            if response is not None: response.close()
            pool.close()
    raise VoiceError('Слишком много перенаправлений при загрузке аудио.')


def decode_bounded_audio(path):
    import av
    import numpy as np
    frames = []
    total = 0
    deadline = time.monotonic() + 60
    with av.open(str(path)) as container:
        if container.duration and container.duration / av.time_base > VOICE_MAX_SECONDS:
            raise VoiceError('Голосовое сообщение длиннее 5 минут.')
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        def collect(frame):
            nonlocal total
            samples = frame.to_ndarray().flatten()
            total += samples.size
            if total > VOICE_MAX_SECONDS * 16000:
                raise VoiceError('Голосовое сообщение длиннее 5 минут.')
            frames.append(samples)
        for frame in container.decode(audio=0):
            if time.monotonic() > deadline:
                raise VoiceError('Декодирование аудио заняло больше минуты.')
            for converted in resampler.resample(frame): collect(converted)
        for converted in resampler.resample(None): collect(converted)
    if not total:
        raise VoiceError('В записи нет звука. Отправьте голосовое повторно.')
    return np.concatenate(frames).astype(np.float32) / 32768.0


def transcribe(attachment):
    # Always use local ASR: do not rely on external MAX transcription.
    url = (attachment.get('payload') or {}).get('url')
    if not isinstance(url,str):
        raise VoiceError('В голосовом сообщении нет ссылки на аудио. Отправьте его повторно.')
    global _model
    try:
        with tempfile.TemporaryDirectory(prefix='max-voice-') as directory:
            audio = Path(directory)/'voice.audio'
            download_audio(url, audio)
            samples = decode_bounded_audio(audio)
            with _lock:
                if _model is None:
                    from faster_whisper import WhisperModel
                    _model = WhisperModel(VOICE_MODEL, device='cpu', compute_type='int8',
                        cpu_threads=4, download_root=str(VOICE_MODEL_DIR), local_files_only=True)
                segments, _ = _model.transcribe(samples, language='ru', beam_size=5,
                    vad_filter=True, condition_on_previous_text=False, hotwords=ASR_NAME_HINT)
                text = ' '.join(segment.text.strip() for segment in segments).strip()
            if not text:
                raise VoiceError('Не удалось разобрать речь. Повторите голосовое или отправьте текст.')
            return text
    except VoiceError:
        raise
    except Exception:
        # URLs may carry signed media tokens. Never expose raw transport/decoder errors.
        raise VoiceError('Не удалось распознать голосовое сообщение локально. Проверьте модель распознавания или отправьте текст.') from None
