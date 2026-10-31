import bot_test_support
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import voice_clone as vc
import file_tools
import project_changes
import code_deploy


class VoiceClonePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.inbox=vc.private_directory(self.root/'inbox')

    def test_inputs_must_be_regular_supported_files_inside_inbox(self):
        wave=self.inbox/'sample.wav';wave.write_bytes(b'placeholder')
        self.assertEqual(vc.input_file(self.inbox,'sample.wav',vc.AUDIO_SUFFIXES),wave.resolve())
        outside=self.root/'outside.wav';outside.write_bytes(b'outside')
        (self.inbox/'linked.wav').symlink_to(outside)
        for name in ['../outside.wav',str(outside),'linked.wav','missing.wav']:
            with self.assertRaises(ValueError):vc.input_file(self.inbox,name,vc.AUDIO_SUFFIXES)

    def test_profile_integrity_detects_changed_audio_or_text(self):
        ident='a'*32;profile=vc.private_directory(self.root/'profiles'/ident)
        vc.write_private(profile/'reference.wav',b'wave')
        vc.write_private(profile/'reference.txt','Тестовая речь'.encode())
        manifest={'audio_sha256':vc.fingerprint(profile/'reference.wav'),'text_sha256':vc.fingerprint(profile/'reference.txt')}
        vc.write_private(profile/'manifest.json',json.dumps(manifest).encode())
        vc.write_private(self.root/'active.json',json.dumps({'profile':ident}).encode())
        self.assertEqual(vc.active_profile(self.root)[0],profile)
        (profile/'reference.txt').write_text('Другой текст')
        with self.assertRaises(ValueError):vc.active_profile(self.root)

    def test_empty_and_invalid_profiles_do_not_claim_ready(self):
        with self.assertRaises(ValueError):vc.active_profile(self.root)
        (self.root/'active.json').write_text(json.dumps({'profile':'../escape'}))
        with self.assertRaises(ValueError):vc.active_profile(self.root)
        with self.assertRaises(ValueError):vc.load_tts(self.root/'missing-model')

    def test_profile_permissions_are_private(self):
        vc.write_private(self.inbox/'sample.txt',b'text')
        self.assertEqual(self.inbox.stat().st_mode & 0o777,0o700)
        self.assertEqual((self.inbox/'sample.txt').stat().st_mode & 0o777,0o600)

    def test_voice_paths_load_from_project_env_with_process_override(self):
        (self.root/'.env').write_text('TTS_MODEL_DIR=models\nTTS_VOICE_DIR=private-voice\n')
        with patch.object(vc,'BASE_DIR',self.root),patch.dict(os.environ,{},clear=True):
            self.assertEqual(vc.settings(),(self.root/'models',self.root/'private-voice'))
            os.environ['TTS_VOICE_DIR']=str(self.inbox)
            self.assertEqual(vc.settings()[1],self.inbox)

    def test_voice_data_and_environment_are_not_generic_file_tools(self):
        with patch.object(file_tools,'FS_ROOTS',(self.root,)):
            for path in ['.voices/inbox/sample.txt','.venv-tts/pyvenv.cfg']:
                with self.assertRaises(PermissionError):file_tools.resolve_path(str(self.root/path))
        self.assertEqual(project_changes.source_path('requirements-tts.txt'),'requirements-tts.txt')
        self.assertEqual(code_deploy.safe_path(self.root,'requirements-tts.txt'),self.root/'requirements-tts.txt')
