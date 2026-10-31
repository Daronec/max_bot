"""Offline preparation and synthesis using a private voice reference and MLX TTS.

Run with .venv-tts/bin/python. This utility does not upload recordings or enable
voice responses in MAX; it prepares a verified local voice profile first.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
AUDIO_SUFFIXES = {'.wav','.flac','.m4a','.mp3','.ogg','.opus','.aac','.aiff','.aif'}


def settings():
    values = dotenv_values(BASE_DIR/'.env',interpolate=False)
    def location(key,default):
        path = Path(os.environ.get(key,values.get(key) or default)).expanduser()
        return (BASE_DIR/path).resolve() if not path.is_absolute() else path.resolve()
    return location('TTS_MODEL_DIR','.models/tts/qwen3-tts-0.6b-base'),location('TTS_VOICE_DIR','.voices')


def private_directory(path):
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.chmod(0o700)
    return path


def write_private(path,data):
    descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(data)
    path.chmod(0o600)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_file(directory,name,suffixes):
    directory = directory.resolve()
    path = Path(name).expanduser()
    if not path.is_absolute():path = directory/path
    if path.is_symlink() or not path.resolve().is_relative_to(directory):
        raise ValueError('Копируйте обычный файл в папку входящих записей; ссылки и внешние пути не принимаются.')
    path = path.resolve()
    if not path.is_file() or path.stat().st_nlink != 1:raise ValueError('Не найден обычный файл записи.')
    if path.suffix.lower() not in suffixes:raise ValueError('Неподдерживаемый формат файла.')
    if path.stat().st_size>100*1024*1024:raise ValueError('Файл больше 100 МБ.')
    return path


def prepare(voice_dir,audio,text_file=None):
    import imageio_ffmpeg
    import numpy as np
    import soundfile as sf
    inbox = private_directory(voice_dir/'inbox')
    source = input_file(inbox,audio,AUDIO_SUFFIXES)
    transcript = input_file(inbox,text_file or source.with_suffix('.txt'),{'.txt'})
    text = transcript.read_text(encoding='utf-8-sig').strip()
    if not 3<=len(text)<=4000:raise ValueError('Нужна точная расшифровка записи в UTF-8, от 3 до 4000 символов.')
    profiles = private_directory(voice_dir/'profiles')
    with tempfile.TemporaryDirectory(prefix='.prepare-',dir=profiles) as temporary:
        wave = Path(temporary)/'reference.wav'
        command = [imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-hide_banner','-loglevel','error',
                   '-i',str(source),'-map','0:a:0','-vn','-ac','1','-ar','24000','-t','21',
                   '-c:a','pcm_s16le',str(wave)]
        result = subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
        if result.returncode:raise ValueError('FFmpeg не смог прочитать запись. Попробуйте WAV или M4A.')
        samples,rate = sf.read(wave,dtype='float32')
        duration = len(samples)/rate
        if not 3<=duration<=20:raise ValueError('Для эталона нужен фрагмент 3–20 секунд, лучше 10–15. Текст должен соответствовать всему фрагменту; автоматической обрезки нет.')
        if not np.isfinite(samples).all() or float(np.sqrt(np.mean(samples**2)))<0.001:
            raise ValueError('Запись содержит тишину или слишком тихая.')
        ident = uuid.uuid4().hex
        profile = private_directory(profiles/ident)
        write_private(profile/'reference.wav',wave.read_bytes())
        write_private(profile/'reference.txt',text.encode())
        manifest = {'id':ident,'created_at':datetime.now(timezone.utc).isoformat(),
                    'duration_seconds':round(duration,3),'sample_rate':rate,
                    'audio_sha256':fingerprint(profile/'reference.wav'),
                    'text_sha256':fingerprint(profile/'reference.txt')}
        write_private(profile/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode())
        active = voice_dir/'active.json'
        temporary_active = voice_dir/'.active.tmp'
        write_private(temporary_active,json.dumps({'profile':ident}).encode())
        os.replace(temporary_active,active)
    return manifest


def active_profile(voice_dir):
    active = voice_dir/'active.json'
    if not active.is_file():raise ValueError('Профиль голоса ещё не подготовлен. Скопируйте WAV и TXT в inbox и выполните prepare.')
    ident = json.loads(active.read_text())['profile']
    if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):raise ValueError('Некорректный профиль.')
    folder = voice_dir/'profiles'/ident
    if folder.is_symlink() or (voice_dir/'profiles').is_symlink():raise ValueError('Профиль не должен быть ссылкой.')
    manifest = json.loads((folder/'manifest.json').read_text())
    for name,key in [('reference.wav','audio_sha256'),('reference.txt','text_sha256')]:
        file = folder/name
        if file.is_symlink() or fingerprint(file)!=manifest[key]:raise ValueError('Эталон изменился. Повторите prepare с актуальной расшифровкой.')
    return folder,manifest


def load_tts(model_dir):
    # A missing file is an error, not a reason to download a model at inference time.
    required = ['config.json','model.safetensors','tokenizer_config.json','vocab.json',
                'merges.txt','speech_tokenizer/config.json','speech_tokenizer/model.safetensors']
    if any(not (model_dir/name).is_file() for name in required):raise ValueError('Модель TTS скачана не полностью.')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ.setdefault('HF_HOME',str(BASE_DIR/'.models/hf-tts'))
    from mlx_audio.tts.utils import load_model
    return load_model(str(model_dir))


def synthesize(model_dir,voice_dir,text):
    import numpy as np
    import soundfile as sf
    if not isinstance(text,str) or not 1<=len(text.strip())<=600:raise ValueError('Пробный текст должен содержать от 1 до 600 символов.')
    profile,_ = active_profile(voice_dir)
    model = load_tts(model_dir)
    arrays = []
    for result in model.generate(text=text.strip(),ref_audio=str(profile/'reference.wav'),
            ref_text=(profile/'reference.txt').read_text(),lang_code='russian',max_tokens=512,
            verbose=False,stream=False):
        arrays.append(np.asarray(result.audio,dtype=np.float32).reshape(-1))
    if not arrays:raise ValueError('Модель не вернула звук.')
    samples = np.concatenate(arrays)
    if not len(samples) or not np.isfinite(samples).all():raise ValueError('Некорректный результат синтеза.')
    output = private_directory(voice_dir/'generated')/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.wav')
    sf.write(output,samples,model.sample_rate,subtype='PCM_16');output.chmod(0o600)
    return {'output':str(output),'duration_seconds':round(len(samples)/model.sample_rate,3),'sample_rate':model.sample_rate}


def main():
    interpreter = BASE_DIR/'.venv-tts/bin/python'
    if interpreter.is_file() and Path(sys.prefix).resolve() != (BASE_DIR/'.venv-tts').resolve():
        os.execv(str(interpreter),[str(interpreter),str(Path(__file__).resolve()),*sys.argv[1:]])
    parser = argparse.ArgumentParser(description='Локальное клонирование вашего голоса: подготовка эталона и пробное озвучивание.')
    sub = parser.add_subparsers(dest='command',required=True)
    status = sub.add_parser('status');status.add_argument('--load',action='store_true')
    prep = sub.add_parser('prepare');prep.add_argument('audio');prep.add_argument('--text-file')
    speak = sub.add_parser('speak');speak.add_argument('--text',default='Привет! Это проверка локального клонирования голоса.')
    args = parser.parse_args()
    model_dir,voice_dir = settings()
    private_directory(voice_dir);private_directory(voice_dir/'inbox')
    try:
        if args.command=='prepare':result = prepare(voice_dir,args.audio,args.text_file)
        elif args.command=='speak':result = synthesize(model_dir,voice_dir,args.text)
        else:
            result = {'model_dir':str(model_dir),'inbox':str(voice_dir/'inbox'),'model_downloaded':(model_dir/'model.safetensors').is_file(),'voice_prepared':(voice_dir/'active.json').is_file()}
            if result['voice_prepared']:active_profile(voice_dir)
            if args.load:
                model = load_tts(model_dir)
                result.update(model_loaded=True,sample_rate=model.sample_rate)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,OSError,KeyError,subprocess.TimeoutExpired) as error:
        parser.exit(1,'Ошибка: '+str(error)+'\n')


if __name__=='__main__':main()
