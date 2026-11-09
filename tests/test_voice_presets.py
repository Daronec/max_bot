import bot_test_support
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from assistant_app.core import server
from assistant_app.speech import clone as vc


class VoicePresetTests(unittest.TestCase):
    def test_panel_preserves_exact_pair_and_serves_only_registered_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'voices'
            model_names = list(vc.TTS_MODELS)
            presets = {}
            for index, model_name in enumerate(model_names):
                ident = str(index) * 32
                profile = directory / 'profiles' / ident
                profile.mkdir(parents=True)
                (profile / 'reference.wav').write_bytes(b'fixture')
                (profile / 'reference.txt').write_text('Эталон')
                (profile / 'manifest.json').write_text(json.dumps(dict(
                    audio_sha256=vc.fingerprint(profile / 'reference.wav'),
                    text_sha256=vc.fingerprint(profile / 'reference.txt'),
                    duration_seconds=12.3,
                )))
                for filename in vc.TTS_FILES:
                    file = root / '.models/tts' / model_name / filename
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.touch()
                presets[str(index)] = dict(profile=ident, model=model_name, label=model_name)
            sample = directory / 'generated' / '20261107-100607-c66253fb.wav'
            sample.parent.mkdir()
            with wave.open(str(sample), 'wb') as output:
                output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                output.writeframes(b'\0' * 48000)
            presets['0']['sample'] = sample.name
            presets['xtts'] = dict(label='Сравнение', model='XTTS-v2', sample=sample.name, preview_only=True)
            (directory / 'presets.json').write_text(json.dumps(presets))
            (directory / 'active.json').write_text(json.dumps(dict(profile=presets['1']['profile'])))
            with patch.object(vc,'BASE_DIR',root), patch.object(vc,'settings',return_value=(root/'.models/tts'/model_names[1],directory)), patch.object(server,'TASKS_DIR',str(root/'tasks')), patch.object(server,'active_tasks',{}):
                client = server.app.test_client()
                voices = client.get('/api/voices').get_json()
                self.assertTrue(voices['presets'][1]['active'])
                self.assertEqual(voices['presets'][0]['sample_seconds'], 1)
                self.assertTrue(voices['presets'][2]['preview_only'])
                self.assertEqual(client.post('/api/voices', json={'preset_id':'xtts'}).status_code, 400)
                with client.get('/api/voices/0/sample') as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data, sample.read_bytes())
                    self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(client.get('/api/voices/missing/sample').status_code, 404)
                response = client.post('/api/voices', json={'preset_id':'0'})
                self.assertTrue(response.get_json()['presets'][0]['active'])
                model, profile, _ = vc.active_voice(*vc.settings())
                self.assertEqual((model.name, profile.name), (model_names[0], presets['0']['profile']))
                self.assertEqual((directory/'active.json').stat().st_mode & 0o777, 0o600)
                self.assertEqual(client.post('/api/voices', json={'preset_id':'../other'}).status_code, 400)
                sample.unlink()
                sample.symlink_to(profile/'reference.wav')
                self.assertEqual(client.get('/api/voices/0/sample').status_code, 400)
