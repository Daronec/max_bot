import bot_test_support
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import code_deploy
import project_changes as pc
import project_agent as pa
import server
import task_processor
from tool_protocol import tool_calls

APP = Path(pc.__file__).parent


class ProjectChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'run.py').write_text('value = 1\n')
        (self.root/'demo.py').write_text('def value():\n    return 1\n')
        (self.root/'config.py').write_text('import os\nTOKEN = os.getenv("TOKEN", "")\n')
        (self.root/'tests').mkdir()
        (self.root/'tests/test_demo.py').write_text('import unittest\nfrom demo import value\nclass Tests(unittest.TestCase):\n    def test_positive(self): self.assertGreater(value(), 0)\n')
        (self.root/'.env').write_text('MAX_BOT_TOKEN=private-test-token-12345\n')
        (self.root/'memory').mkdir();(self.root/'memory/facts.py').write_text('private = True')
        # Use the current interpreter, including its dependency environment.
        (self.root/'.venv').symlink_to(Path(sys.prefix),target_is_directory=True)
        shutil.copy2(APP/'code_deploy.py',self.root/'code_deploy.py')
        self.manager = pc.ProjectChanges(self.root,True,{'10'},test_timeout=20,allowed_senders={'20'})
        self.task = {'task_id':'max_test','source':'max','chat_id':'10','sender_id':'20','bot_message_id':'message'}
        self.state,_ = self.manager.create(self.task,'Исправь в max_bot функцию value')
        self.tools = pa.ChangeTools(self.manager,self.state)

    def edit(self,text='def value():\n    return 2\n'):
        version = self.tools.read('demo.py')['sha256']
        return self.tools.write('demo.py',text,version)

    def ready(self):
        self.edit();self.assertTrue(self.manager.run_tests(self.state)['passed'])
        self.manager.review(self.state,'Изменён результат value.')
        return self.state

    def test_snapshot_excludes_private_data_and_can_read_own_control_sources(self):
        folder = self.manager.job_dir(self.state['id'])
        self.assertFalse((folder/'tree/.env').exists())
        self.assertFalse((folder/'tree/memory').exists())
        self.assertNotIn('.code_changes',self.state['base'])
        self.assertIn('TOKEN',self.tools.read('config.py')['content'])
        self.edit();self.assertIn('return 1',(self.root/'demo.py').read_text())
        self.assertEqual(folder.stat().st_mode & 0o777,0o700)

    def test_owner_and_original_message_required(self):
        for extra in ({'sender_id':'other'},{'chat_id':'other'}):
            task = {**self.task,**extra}
            with self.assertRaises((ValueError,PermissionError)):
                self.manager.load(self.state['id'],self.manager.owner(task))
        for extra in ({'source':'web'},{'sender_id':None},{'memory_excluded':True}):
            with self.assertRaises(PermissionError):self.manager.owner({**self.task,**extra})
        unconfigured=pc.ProjectChanges(self.root,True,{'10'},allowed_senders=set())
        with self.assertRaises(PermissionError):unconfigured.owner(self.task)

    def test_paths_and_links_are_rejected(self):
        for name in ('../config.py','/tmp/x.py','.env','memory/data.py','tests/../../a.py','tasks/a.py','.git/config','static/.env'):
            self.assertIn('error',self.tools.execute('code_read',{'path':name}))
        outside = self.root/'secret.txt';outside.write_text('secret')
        tree = self.tools.tree
        (tree/'leak.py').symlink_to(outside)
        self.assertIn('error',self.tools.execute('code_read',{'path':'leak.py'}))
        with self.assertRaises(ValueError):self.manager.job_dir('../escape')

    def test_write_requires_read_and_current_version(self):
        with self.assertRaises(ValueError):self.tools.write('demo.py','x=1','guess')
        version = self.tools.read('demo.py')['sha256']
        (self.tools.tree/'demo.py').write_text('changed = True')
        with self.assertRaises(ValueError):self.tools.write('demo.py','x=1',version)

    def test_tests_required_and_edits_invalidate_results(self):
        self.edit()
        with self.assertRaises(ValueError):self.manager.review(self.state,'Changed')
        self.assertTrue(self.manager.run_tests(self.state)['passed'])
        self.edit('def value():\n    return 3\n')
        with self.assertRaises(ValueError):self.manager.review(self.state,'Changed again')

    def test_original_tests_cannot_be_weakened_to_hide_failure(self):
        self.edit('def value():\n    return 0\n')
        version = self.tools.read('tests/test_demo.py')['sha256']
        self.tools.write('tests/test_demo.py','import unittest\n',version)
        self.assertFalse(self.manager.run_tests(self.state)['passed'])
        self.assertIn('FAIL',(self.manager.job_dir(self.state['id'])/'tests.log').read_text())

    def test_new_tests_are_also_executed(self):
        self.edit()
        self.tools.write('tests/test_new.py','import unittest\nclass New(unittest.TestCase):\n    def test_failure(self): self.fail("new test")\n','')
        self.assertFalse(self.manager.run_tests(self.state)['passed'])

    def test_secret_values_and_baseline_tampering_block_review(self):
        self.edit('token = "private-test-token-12345"\n')
        with self.assertRaises(ValueError):self.manager.verify(self.state)
        self.edit()
        base = self.manager.job_dir(self.state['id'])/'base/demo.py'
        base.chmod(0o600);base.write_text('tampered')
        with self.assertRaises(ValueError):self.manager.verify(self.state)

    def test_duplicate_message_reuses_draft_and_status_needs_no_model(self):
        state,created = self.manager.create(self.task,'different text')
        self.assertFalse(created);self.assertEqual(state['request'],self.state['request'])
        with patch.object(pa,'prepare_change') as prepare:
            result = pc.handle_project_request(self.task,'/code status '+state['id'],self.manager)
            self.assertIn(state['id'],result);prepare.assert_not_called()

    def test_stale_live_file_blocks_deploy_without_stopping_bot(self):
        self.ready();(self.root/'demo.py').write_text('manual edit')
        with patch.object(pc.subprocess,'Popen') as process:
            with self.assertRaises(ValueError):self.manager.deploy(self.state,self.task)
            process.assert_not_called()

    def test_unrelated_source_change_also_invalidates_review(self):
        self.ready();(self.root/'run.py').write_text('manual = True\n')
        with patch.object(pc.subprocess,'Popen') as process:
            with self.assertRaises(ValueError):self.manager.deploy(self.state,self.task)
            process.assert_not_called()

    def test_deploy_only_schedules_after_review_and_duplicate_is_idempotent(self):
        self.ready()
        with patch.object(pc.subprocess,'Popen') as process:
            answer = self.manager.deploy(self.state,self.task)
            self.assertIn('после доставки',answer)
            self.manager.deploy(self.state,self.task)
            process.assert_called_once()
        self.assertIn('return 1',(self.root/'demo.py').read_text())
        self.assertTrue((self.manager.job_dir(self.state['id'])/'deploy_runner.py').is_file())

    def test_job_not_editable_after_review(self):
        self.ready()
        with self.assertRaises(ValueError):self.edit()

    def test_textual_tool_really_executes_file_read(self):
        xml = '<tool_call>\n<function=read_file>\n<parameter=path>demo.py</parameter>\n</function>\n</tool_call>'
        def reply(content):
            response = Mock();response.json.return_value={'choices':[{'message':{'content':content}}]};return response
        with patch.object(task_processor,'model_post',side_effect=[reply(xml),reply('Файл прочитан')]), \
             patch.object(task_processor,'WEB_ENABLED',False), \
             patch('file_tools.execute_tool',return_value={'content':'example','sha256':'hash'}) as execute:
            answer=task_processor.openai_completion({},'local','Прочитай файл demo.py','test',allow_files=True)
        self.assertEqual(answer,'Файл прочитан');execute.assert_called_once_with('read_file',{'path':'demo.py'})

    def test_development_bypasses_personal_memory(self):
        with patch.object(pc,'handle_project_request',return_value='draft') as handler, \
             patch.object(server.personal_memory,'turn') as memory:
            result=server.execute_with_memory(self.task,'/code improve',Mock(),{})
        self.assertEqual(result,'draft');memory.assert_not_called();handler.assert_called_once()

    def test_worker_waits_for_deployment_but_retries_approval_delivery(self):
        with patch.object(pc,'deployment_pending',return_value=True),patch.object(server,'active_tasks',{}):
            self.assertFalse(server.process_next_task())
        pending={'task_id':'approval','status':'completed','delivered':False}
        with patch.object(pc,'deployment_pending',return_value=True),patch.object(server,'active_tasks',{'approval':pending}),patch.object(server,'deliver_result') as deliver:
            self.assertTrue(server.process_next_task())
            deliver.assert_called_once_with(pending)

    def test_generic_file_tools_cannot_bypass_project_workflow(self):
        import file_tools
        with patch.object(file_tools,'FS_ROOTS',(APP,)):
            for name in ('project_changes.py','project_agent.py','code_deploy.py','memory_agent.py'):
                with self.assertRaises(PermissionError):file_tools.resolve_path(str(APP/name),write=True)

    def test_agent_finish_tests_and_reviews_real_changes(self):
        def reply(name,args):
            response=Mock();response.json.return_value={'choices':[{'message':{'role':'assistant','tool_calls':[
                {'id':name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]};return response
        digest=self.tools.read('demo.py')['sha256']
        responses=[reply('code_read',{'path':'demo.py'}),reply('code_edit',{'path':'demo.py','old_text':'return 1','new_text':'return 2','expected_sha256':digest}),reply('code_finish',{'summary':'Исправлена функция.'})]
        with patch.object(pa,'model_post',side_effect=responses),patch.object(pa,'LOCAL_LM_MODEL','local'):
            result=pa.prepare_change(self.manager,self.state)
        self.assertIn('/code apply',result);self.assertEqual(self.state['phase'],'ready')
        self.assertIn('return 1',(self.root/'demo.py').read_text())

    def test_test_log_redacts_real_credentials(self):
        folder=self.manager.job_dir(self.state['id'])
        (folder/'tests.log').write_text('private-test-token-12345')
        result=pc.handle_project_request(self.task,'/code tests '+self.state['id'],self.manager)
        self.assertNotIn('private-test-token',result);self.assertIn('[PRIVATE]',result)



class DeploymentTests(unittest.TestCase):
    setUp=ProjectChangeTests.setUp
    edit=ProjectChangeTests.edit
    ready=ProjectChangeTests.ready

    def deployment(self,mode='apply'):
        state=self.ready()
        state.update(phase='deploying',deployment={'mode':mode,'changes':self.manager.changes(state),'host':'127.0.0.1','port':5001})
        self.manager.save(state)
        return state,self.manager.job_dir(state['id'])

    def test_successful_apply_and_manual_rollback(self):
        state,folder=self.deployment()
        with patch.object(code_deploy,'get_status',return_value={'bot_id':'synthetic'}),patch.object(code_deploy,'stop_bot'),patch.object(code_deploy,'launch_bot'),patch.object(code_deploy,'healthy'):
            code_deploy.deploy(self.root,folder,state)
            self.assertEqual(state['phase'],'applied');self.assertIn('return 2',(self.root/'demo.py').read_text())
            state['deployment']['mode']='rollback'
            code_deploy.deploy(self.root,folder,state)
            self.assertEqual(state['phase'],'rolled_back');self.assertIn('return 1',(self.root/'demo.py').read_text())

    def test_failed_new_start_restores_old_files_and_relaunches(self):
        state,folder=self.deployment()
        with patch.object(code_deploy,'get_status',return_value={'bot_id':'synthetic'}),patch.object(code_deploy,'stop_bot'),patch.object(code_deploy,'launch_bot') as launch,patch.object(code_deploy,'healthy',side_effect=[RuntimeError('unhealthy'),None]):
            code_deploy.deploy(self.root,folder,state)
        self.assertEqual(state['phase'],'rolled_back');self.assertEqual(launch.call_count,2)
        self.assertIn('return 1',(self.root/'demo.py').read_text())

    def test_changed_candidate_does_not_reach_stop(self):
        state,folder=self.deployment();(folder/'tree/demo.py').write_text('tampered')
        with patch.object(code_deploy,'stop_bot') as stop:
            with self.assertRaises(ValueError):code_deploy.deploy(self.root,folder,state)
            stop.assert_not_called()

    def test_bytecode_of_changed_modules_is_invalidated(self):
        cache=self.root/'__pycache__';cache.mkdir()
        stale=cache/'demo.cpython-312.pyc';stale.write_bytes(b'old cache')
        other=cache/'other.cpython-312.pyc';other.write_bytes(b'keep')
        code_deploy.invalidate_bytecode(self.root,['demo.py'])
        self.assertFalse(stale.exists());self.assertTrue(other.exists())

    def test_rollback_removes_only_files_created_by_change(self):
        self.tools.write('new_module.py','value = 1\n','')
        state,folder=self.deployment()
        with patch.object(code_deploy,'get_status',return_value={'bot_id':'synthetic'}),patch.object(code_deploy,'stop_bot'),patch.object(code_deploy,'launch_bot'),patch.object(code_deploy,'healthy'):
            code_deploy.deploy(self.root,folder,state)
            self.assertTrue((self.root/'new_module.py').exists())
            state['deployment']['mode']='rollback';code_deploy.deploy(self.root,folder,state)
            self.assertFalse((self.root/'new_module.py').exists())


class ProtocolAndRoutingTests(unittest.TestCase):
    def test_qwen_text_parameters_use_schema_types(self):
        xml='<tool_call><function=code_read><parameter=path>config.py</parameter><parameter=start_line>2</parameter></function></tool_call>'
        call=tool_calls({'content':xml},pa.SCHEMAS)[0]
        self.assertEqual(json.loads(call['function']['arguments']),{'path':'config.py','start_line':2})
        self.assertFalse(tool_calls({'content':xml},[]))
        self.assertFalse(tool_calls({'content':xml.replace('>2<','>__import__("os")<')},pa.SCHEMAS))

    def test_json_text_tool_call_and_no_unknown_execution(self):
        text='<tool_call>{"name":"code_list","arguments":{}}</tool_call>'
        self.assertEqual(tool_calls({'content':text},pa.SCHEMAS)[0]['function']['name'],'code_list')
        self.assertFalse(tool_calls({'content':text.replace('code_list','run_command')},pa.SCHEMAS))

    def test_natural_development_requests_and_normal_chat(self):
        for text in ['/code help','Исправь код самого бота','В проекте max_bot не работает адрес','Улучши проект макс бот']:
            self.assertTrue(pc.is_project_request(text),text)
        for text in ['Запомни мой адрес','Расскажи анекдот','Я работаю инженером']:
            self.assertFalse(pc.is_project_request(text),text)
