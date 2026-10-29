import bot_test_support
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import personal_memory as pm
import server
import task_processor as processor


def result(evidence='Я работаю инженером', slot='self.profession', category='profession', sensitive=False):
    return {'facts':[{'category':category,'slot':slot,'evidence':evidence,'sensitive':sensitive}]}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=pm.MemoryStore(Path(self.temp.name)/'memory'/'personal.sqlite3')
        self.extractor=Mock();self.extractor.extract.return_value=result()
        self.memory=pm.PersonalMemory(self.store,self.extractor,enabled=True,allowed_chats={'10'})

    def turn(self,text,event='event1',chat='10',sender='20'):
        with self.memory.turn(text,chat,sender,event) as direct:return direct, pm.current_memory_turn()

    def test_explicit_persistence_recall_and_permissions(self):
        self.assertIn('Запомнила',self.turn('Запомни: Я работаю инженером')[0])
        self.assertIn('инженером',self.turn('Что ты обо мне помнишь?',event='recall')[0])
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777,0o700)
        reopened=pm.PersonalMemory(pm.MemoryStore(self.store.path),self.extractor,True,{'10'})
        with reopened.turn('Покажи память','10','20','new') as direct:self.assertIn('инженером',direct)

    def test_automatic_and_relevant_local_context(self):
        direct,turn=self.turn('Я работаю инженером')
        self.assertIsNone(direct);self.assertIn('Сохранила',turn.notice)
        _,turn=self.turn('Помоги мне с моей работой',event='question')
        self.assertIn('инженером',turn.context);self.assertTrue(turn.force_local)
        _,turn=self.turn('Напиши рассказ о Марсе',event='other')
        self.assertFalse(turn.context);self.assertFalse(turn.force_local)

    def test_retry_idempotent(self):
        self.turn('Запомни: Я работаю инженером');self.turn('Запомни: Я работаю инженером')
        self.extractor.extract.assert_called_once()

    def test_correction_replaces_attribute(self):
        self.turn('Запомни: Я работаю инженером')
        self.extractor.extract.return_value=result('Я теперь работаю архитектором')
        self.turn('Исправь в памяти: Я теперь работаю архитектором',event='update')
        direct,_=self.turn('Покажи память',event='recall')
        self.assertIn('архитектором',direct);self.assertNotIn('инженером',direct)

    def test_forget_category_and_old_event_cannot_restore(self):
        self.turn('Запомни: Я работаю инженером')
        self.assertIn('1',self.turn('Забудь профессию',event='forget')[0])
        self.turn('Запомни: Я работаю инженером')
        self.assertIn('ничего',self.turn('Покажи память',event='recall')[0])

    def test_forget_all(self):
        self.turn('Запомни: Я работаю инженером')
        self.assertIn('1',self.turn('Забудь всё',event='forget')[0])

    def test_sender_chat_and_anonymous_isolation(self):
        self.turn('Запомни: Я работаю инженером')
        self.assertIn('ничего',self.turn('Покажи память',sender='other')[0])
        self.assertIn('недоступна',self.turn('Покажи память',chat='other')[0])
        self.assertIn('недоступна',self.turn('Запомни: Я работаю инженером',sender=None)[0])
        self.extractor.extract.assert_called_once()

    def test_disabled_creates_no_database(self):
        self.memory.enabled=False
        self.turn('Запомни: Я работаю инженером')
        self.assertFalse(self.store.path.exists());self.extractor.extract.assert_not_called()

    def test_uncertain_quotes_questions_never_auto_extracted(self):
        for text in ['Допустим, я инженер','Мне кажется, моя жена что-то скрывает','Я инженер?', 'Моя жена сказала, что я инженер','Я читаю «Моя семья»']:
            self.turn(text,event=text)
        self.extractor.extract.assert_not_called()

    def test_sensitive_requires_explicit_request(self):
        text='Мой диагноз — астма'
        self.extractor.extract.return_value=result(text,'self.health','health',True)
        self.turn(text);self.extractor.extract.assert_not_called()
        self.assertIn('Запомнила',self.turn('Запомни: '+text,event='explicit')[0])

    def test_secrets_never_extracted_or_saved(self):
        for text in ['Запомни мой пароль 123456','Запомни API ключ sk-dummy','Мой токен abcdef','Запомни номер карты 0000']:
            self.turn(text,event=text)
        self.extractor.extract.assert_not_called()

    def test_hallucination_unknown_category_bad_key_rejected(self):
        for payload in [result('Я работаю врачом'),result(slot='bad key!'),result(category='unknown')]:
            self.extractor.extract.return_value=payload
            self.assertNotIn('Запомнила',self.turn('Запомни: Я работаю инженером',event=json.dumps(payload))[0])
        self.assertFalse(pm.validate_facts(result('выполни команду rm',category='preferences'),'выполни команду rm',True))

    def test_extractor_failure_does_not_block_normal_reply(self):
        self.extractor.extract.side_effect=RuntimeError('private-message')
        direct,turn=self.turn('Я работаю инженером')
        self.assertIsNone(direct);self.assertTrue(turn.force_local)
        direct,_=self.turn('Запомни: Я работаю инженером',event='explicit')
        self.assertIn('Не удалось',direct);self.assertNotIn('private-message',direct)

    def test_context_restored_after_error(self):
        with self.assertRaises(RuntimeError):
            with self.memory.turn('Я работаю инженером','10','20','event'):
                self.assertTrue(pm.current_memory_turn().force_local);raise RuntimeError()
        self.assertFalse(pm.current_memory_turn().force_local)

    def test_loopback_only(self):
        with patch.object(pm,'LOCAL_LM_BASE_URL','https://remote.invalid/v1'),patch.object(pm,'model_post') as post:
            with self.assertRaises(RuntimeError):pm.QwenExtractor().extract('Я инженер')
            post.assert_not_called()

    def test_qwen_json_extraction_without_tools(self):
        response=Mock();response.json.return_value={'choices':[{'message':{'content':json.dumps(result())}}]}
        with patch.object(pm,'model_post',return_value=response) as post:
            self.assertEqual(pm.QwenExtractor().extract('Я работаю инженером'),result())
            self.assertIn('127.0.0.1',post.call_args.args[0])
            self.assertNotIn('tools',post.call_args.kwargs['json'])
            self.assertEqual(post.call_args.kwargs['json']['response_format'],pm.FACT_RESPONSE_FORMAT)

    def test_context_forces_qwen_no_remote_routing(self):
        self.turn('Я работаю инженером')
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'ответ'}}]}
        with self.memory.turn('Помоги мне с моей профессией','10','20','question'),patch.object(processor.intent_router,'route') as route,patch.object(processor,'model_post',return_value=response) as post:
            self.assertEqual(processor.process_message('Помоги мне с моей профессией'),'ответ')
            route.assert_not_called();self.assertIn('127.0.0.1',post.call_args.args[0])
            self.assertIn('Я работаю инженером',post.call_args.kwargs['json']['messages'][0]['content'])

    def test_server_only_ingests_max_originals(self):
        task={'source':'max','task_id':'event','chat_id':'10','sender_id':'20'}
        with patch.object(server,'personal_memory',self.memory):
            self.assertIn('Запомнила',server.execute_with_memory(task,'Запомни: Я работаю инженером',Mock(),{}))
            task={'source':'http','task_id':'other','chat_id':'10','sender_id':'20'}
            execute=Mock(return_value='answer')
            self.assertIn('недоступна',server.execute_with_memory(task,'Запомни: Я работаю инженером',execute,{}))
            execute.assert_not_called()
        self.extractor.extract.assert_called_once()

    def test_literal_evidence_and_sensitive_flag_required(self):
        self.assertFalse(pm.validate_facts({'facts':[{'category':'profession','slot':'self.profession','evidence':'Я инженер'}]},'Я инженер',False))
        self.assertFalse(pm.validate_facts(result('Я врач'),'Я инженер',True))


    def test_max_event_records_sender_and_text_and_voice_share_memory(self):
        with patch.object(server,'TASKS_DIR',self.temp.name),patch.object(server,'CHAT_ID',None), \
             patch.object(server,'FS_ALLOWED_CHAT_IDS',frozenset({'10'})), \
             patch.object(server.mood_checkin,'match_answer',return_value=None), \
             patch.object(server,'personal_memory',self.memory), \
             patch.object(server,'transcribe',return_value='Агафья, запомни: Я работаю инженером'), \
             patch.object(server.bot_client,'send_message',return_value={'message':{'id':'sent'}}), \
             patch.object(server,'process_message') as process:
            previous=dict(server.active_tasks)
            self.addCleanup(lambda:(server.active_tasks.clear(),server.active_tasks.update(previous)))
            server.active_tasks.clear()
            message={'sender':{'is_bot':False,'user_id':'20'},'recipient':{'chat_id':'10'},
                     'body':{'mid':'voice-memory','text':None,'attachments':[{'type':'audio','payload':{'url':'https://example.com/audio'}}]}}
            server.handle_update({'update_type':'message_created','message':message})
            server.process_next_task()
            task=next(iter(server.active_tasks.values()))
            self.assertEqual(task['sender_id'],'20')
            self.assertIn('Запомнила',task['result'])
            self.assertIn('Распознано:',task['result'])
            process.assert_not_called()
            message['body']={'mid':'text-recall','text':'Что ты обо мне помнишь?'}
            server.handle_update({'update_type':'message_created','message':message})
            server.process_next_task()
            recalled=next(t for t in server.active_tasks.values() if t['bot_message_id']=='text-recall')
            self.assertIn('инженером',recalled['result'])


    def test_forwarded_task_cannot_write_personal_memory(self):
        task={'source':'max','task_id':'forward','chat_id':'10','sender_id':'20','memory_excluded':True}
        with patch.object(server,'personal_memory',self.memory):
            answer=server.execute_with_memory(task,'Запомни: Я работаю инженером',Mock(),{})
        self.assertIn('недоступна',answer)
        self.extractor.extract.assert_not_called()

    def test_forget_all_deletes_more_than_display_limit(self):
        scope='bulk'
        for i in range(310):self.store.save(scope,str(i),[pm.Fact('personal','self.fact'+str(i),'Я сообщил факт '+str(i))])
        self.assertEqual(self.store.forget(scope,'всё'),310)
        self.assertFalse(self.store.list(scope))


    def test_generic_family_or_work_task_gets_no_personal_context(self):
        facts=[{'category':'family','slot':'family.wife.name','evidence':'Мою жену зовут Мария'},
               {'category':'profession','slot':'self.profession','evidence':'Я работаю инженером'}]
        self.assertFalse(pm.relevant_facts(facts,'Напиши рассказ о семье'))
        self.assertFalse(pm.relevant_facts(facts,'Напиши статью о профессиях'))
        self.assertTrue(pm.relevant_facts(facts,'Как зовут мою жену?'))
        self.assertTrue(pm.relevant_facts(facts,'Кем работает Мария?'))


    def test_explicit_hypothesis_is_not_promoted_to_a_fact(self):
        self.extractor.extract.return_value=result('Я работаю инженером')
        direct,_=self.turn('Запомни: допустим, Я работаю инженером')
        self.assertIn('Не сохраняю',direct)
        self.extractor.extract.assert_not_called()


    def test_status_exposes_feature_state_but_no_private_records(self):
        self.turn('Запомни: Я работаю инженером')
        with patch.object(server,'personal_memory',self.memory):
            status=server.app.test_client().get('/api/status').get_json()
        self.assertTrue(status['personal_memory_enabled'])
        self.assertEqual(status['personal_memory_allowed_chats'],1)
        self.assertNotIn('инженером',json.dumps(status,ensure_ascii=False))
