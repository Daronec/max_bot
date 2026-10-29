import bot_test_support  # Load synthetic configuration before application imports.
import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


class EnvConfigurationTests(unittest.TestCase):
    def load(self, directory):
        target=Path(directory)/'config.py'
        shutil.copy2(ROOT/'config.py',target)
        spec=importlib.util.spec_from_file_location('isolated_bot_config', target)
        config=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        return config

    def test_project_env_loads_secrets_and_private_destinations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            (Path(directory)/'.env').write_text("MAX_BOT_TOKEN='dummy-token'\nMAX_BOT_ID='dummy-bot'\nDEEPSEEK_API_KEY='dummy-key'\nFS_ALLOWED_CHAT_IDS='111,222'\nNOTIFICATIONS_CHAT_ID='111'\nSUMMARY_CHAT_ID='111'\nMOOD_CHAT_ID='111'\nLM_PROVIDER=deepseek\n",encoding='utf-8')
            config=self.load(directory)
            self.assertEqual(config.BOT_TOKEN,'dummy-token')
            self.assertEqual(config.BOT_ID,'dummy-bot')
            self.assertEqual(config.DEEPSEEK_API_KEY,'dummy-key')
            self.assertEqual(config.LM_API_KEY,'dummy-key')
            self.assertEqual(config.FS_ALLOWED_CHAT_IDS,frozenset({'111','222'}))
            self.assertEqual(config.NOTIFICATIONS_CHAT_ID,'111')
            self.assertEqual(config.SUMMARY_CHAT_ID,'111')
            self.assertEqual(config.MOOD_CHAT_ID,'111')

    def test_environment_including_empty_value_wins(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'MAX_BOT_TOKEN':'process-value','DEEPSEEK_API_KEY':''}, clear=True):
            (Path(directory)/'.env').write_text('MAX_BOT_TOKEN=file-value\nDEEPSEEK_API_KEY=file-key\n')
            config=self.load(directory)
            self.assertEqual(config.BOT_TOKEN,'process-value')
            self.assertEqual(config.DEEPSEEK_API_KEY,'')

    def test_no_env_has_no_credentials_or_private_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config=self.load(directory)
            for name in ('BOT_TOKEN','BOT_ID','DEEPSEEK_API_KEY','LM_API_KEY','LOCAL_LM_API_KEY','NOTIFICATIONS_CHAT_ID','SUMMARY_CHAT_ID','MOOD_CHAT_ID'):
                self.assertEqual(getattr(config,name),'',name)
            self.assertIsNone(config.CHAT_ID)
            self.assertFalse(config.FS_ALLOWED_CHAT_IDS)

    def test_no_legacy_secret_loading_and_no_parent_env_search(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            parent=Path(directory)
            (parent/'.env').write_text('MAX_BOT_TOKEN=parent-token\n')
            child=parent/'child';child.mkdir()
            (child/'.secrets').mkdir()
            (child/'.secrets/deepseek.key').write_text('legacy-key')
            (child/'.secrets/provider').write_text('deepseek')
            config=self.load(child)
            self.assertEqual(config.BOT_TOKEN,'')
            self.assertEqual(config.DEEPSEEK_API_KEY,'')
            self.assertEqual(config.LM_PROVIDER,'lmstudio')

    def test_literal_dollar_and_quoted_values_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'REFERENCE':'do-not-expand'}, clear=True):
            (Path(directory)/'.env').write_text("MAX_BOT_TOKEN='literal-${REFERENCE}-#-value'\nLOCAL_LM_API_KEY='with space'\nMAX_CHAT_ID=\n")
            config=self.load(directory)
            self.assertEqual(config.BOT_TOKEN,'literal-${REFERENCE}-#-value')
            self.assertEqual(config.LOCAL_LM_API_KEY,'with space')
            self.assertIsNone(config.CHAT_ID)

    def test_bot_token_has_no_embedded_fallback(self):
        tree=ast.parse((ROOT/'config.py').read_text())
        assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='BOT_TOKEN' for t in n.targets))
        self.assertEqual([v.value for v in ast.walk(assignment.value) if isinstance(v,ast.Constant)], ['MAX_BOT_TOKEN',''])

    @unittest.skipUnless(shutil.which('git'),'requires git')
    def test_git_ignores_env_but_keeps_safe_example(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy2(ROOT/'.gitignore',Path(directory)/'.gitignore')
            subprocess.run(['git','init','-q',directory],check=True,capture_output=True)
            for filename in ('.env','.env.local'):
                result=subprocess.run(['git','-C',directory,'-c','core.excludesFile=/dev/null','check-ignore','--no-index','-q',filename],capture_output=True)
                self.assertEqual(result.returncode,0,filename)
            result=subprocess.run(['git','-C',directory,'-c','core.excludesFile=/dev/null','check-ignore','--no-index','-q','.env.example'],capture_output=True)
            self.assertEqual(result.returncode,1)
