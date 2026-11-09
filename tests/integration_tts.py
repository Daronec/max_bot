"""Explicit local TTS check using the configured profile; never sends audio."""
import json
from pathlib import Path
import sys
import tempfile
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from assistant_app.speech.service import synthesize_task
from assistant_app.core.task_store import SQLiteTaskStore
with tempfile.TemporaryDirectory(prefix='assistant-tts-test-') as directory:
 store=SQLiteTaskStore(directory)
 original=store.submit_chat('Проверка речи','local_owner')
 original.update(status='completed',result='Это проверка локального голосового ответа.',completed_at='test',delivered=True);store.save(original)
 task=dict(task_id='speech_smoke',source='desktop',speech_source=original['task_id'])
 filename=synthesize_task(task,store,root)
 audio=store.directory/'speech'/filename
 assert audio.stat().st_size>1000
 assert filename.endswith('.mp3')
 print(json.dumps(dict(result='passed',local=True,checks=['configured_voice_profile','local_synthesis','mp3_encoding'],bytes=audio.stat().st_size)))
