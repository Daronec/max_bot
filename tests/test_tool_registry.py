import bot_test_support
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import time
import os
from assistant_app.core.execution_context import ExecutionContext, execution_session, ExecutionStopped
from assistant_app.tools.tool_registry import invoke, catalogue
from assistant_app.tools.command_tools import capture_command_actions
from assistant_app.tools.file_tools import capture_file_actions


class ToolRegistryTests(unittest.TestCase):
    def test_permissions_and_schema_checked_before_dispatch(self):
        with patch('assistant_app.tools.command_tools.run_command') as command:
            denied=invoke('run_command',{'command':'echo no'},granted_names={'run_command'})
            self.assertIn('error',denied);command.assert_not_called()
            for args in ({'command':1},{'command':'echo','unknown':True},{'command':'echo','timeout':True}):
                with self.assertRaises(ValueError):invoke('run_command',args,granted_names={'run_command'},allow_commands=True)
            command.assert_not_called()

    def test_agent_receipts_precede_effect_and_real_command_completes(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'FS_ROOTS':temporary,'FS_SEARCH_ROOTS':temporary}):
            path=Path(temporary)/'new.txt';seen=[]
            with execution_session(ExecutionContext(local_only=True,local_agent=True)),capture_file_actions(lambda action:seen.append((action['stage'],path.exists()))) as actions:
                result=invoke('write_file',{'path':str(path),'content':'hello'},granted_names={'write_file'},allow_files=True)
            self.assertNotIn('error',result);self.assertEqual(path.read_text(),'hello')
            self.assertEqual(seen[0],('planned',False));self.assertEqual(actions[0]['stage'],'completed')
            with execution_session(ExecutionContext(local_only=True,local_agent=True)),capture_command_actions() as commands:
                result=invoke('run_command',{'command':'printf agent-ok','cwd':temporary},granted_names={'run_command'},allow_commands=True)
            self.assertEqual(result['stdout'],'agent-ok');self.assertEqual(commands[0]['exit_code'],0)

    def test_agent_command_cancellation_kills_group(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'FS_ROOTS':temporary}):
            started=time.monotonic()
            with execution_session(ExecutionContext(local_only=True,local_agent=True,cancel_check=lambda:time.monotonic()-started>.4)),capture_command_actions() as commands:
                with self.assertRaises(ExecutionStopped):invoke('run_command',{'command':'sleep 30','cwd':temporary},granted_names={'run_command'},allow_commands=True)
            self.assertLess(time.monotonic()-started,7)
            if commands and commands[0].get('pid'):
                with self.assertRaises(ProcessLookupError):os.kill(commands[0]['pid'],0)


if __name__=='__main__':unittest.main()
