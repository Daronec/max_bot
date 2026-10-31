import bot_test_support
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import personal_memory as pm
import memory_agent as ma

class MemoryAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=pm.MemoryStore(Path(self.temp.name)/'memory.sqlite3')
    def tools(self,source='Я работаю инженером',event='e',scope='a'):
        return ma.MemoryTools(self.store,scope,event,source)
    def save(self,t):
        t.search('');return t.write('self.profession','profession',t.source)
    def test_scope_search_and_read(self):
        t=self.tools();self.save(t)
        self.assertEqual(self.tools(scope='b').search('')['total'],0)
        self.assertEqual(t.search('инженер')['total'],1)
        self.assertEqual(t.read(['self.profession'])['records'][0]['evidence'],t.source)
        self.assertIn('error',t.execute('memory_read',{'slots':['self.profession'],'scope':'b'}))
    def test_search_required_and_create_does_not_overwrite(self):
        t=self.tools()
        self.assertIn('error',t.execute('memory_save',{'slot':'self.profession','category':'profession','evidence':t.source}))
        self.save(t)
        with self.assertRaises(ValueError):self.tools(event='new').write('self.profession','profession',t.source)
    def test_update_requires_read_version_and_rejects_concurrent_change(self):
        self.save(self.tools())
        t=self.tools('Я работаю архитектором','e2');t.search('')
        record=t.read(['self.profession'])['records'][0]
        with self.assertRaises(ValueError):t.write('self.profession','profession',t.source,expected_version='wrong')
        other=self.tools('Я работаю врачом','e3');other.search('')
        v=other.read(['self.profession'])['records'][0]['version']
        other.write('self.profession','profession',other.source,expected_version=v)
        with self.assertRaises(ValueError):t.write('self.profession','profession',t.source,expected_version=record['version'])
        v=t.read(['self.profession'])['records'][0]['version']
        t.write('self.profession','profession',t.source,expected_version=v)
        self.assertEqual(self.store.list('a')[0]['evidence'],t.source)
    def test_grounding_and_sensitive_consent(self):
        t=self.tools();t.search('')
        with self.assertRaises(ValueError):t.write('self.profession','profession','Я врач')
        t=self.tools('Мой адрес: Москва, улица Примерная, дом 9');t.search('')
        with self.assertRaises(ValueError):t.write('self.address','personal',t.source)
        t=self.tools('Сохрани, пожалуйста, мой адрес: Москва, улица Примерная, дом 9');t.search('')
        t.write('self.address','personal',t.source,consent_evidence='Сохрани')
        secret=self.tools('Запомни мой пароль 123456','secret');secret.search('')
        with self.assertRaises(ValueError):secret.write('self.password','personal',secret.source,consent_evidence='Запомни')
    def test_permission_uses_real_message_not_model_paraphrase(self):
        t=self.tools('Сохрани мой адрес: Москва, дом 9');t.search('')
        t.write('self.address','personal','Москва, дом 9',consent_evidence='Сохраните мой адрес')
        self.assertEqual(len(self.store.list('a')),1)
        t=self.tools('Мой адрес: Москва, дом 10','new');t.search('')
        with self.assertRaises(ValueError):t.write('other.address','personal',t.source,consent_evidence='Сохрани')
    def test_natural_correction_is_explicit_permission(self):
        self.store.save('a','seed',[pm.Fact('personal','self.address','Мой адрес: Москва, дом 9')])
        t=self.tools('Поправь мой адрес в памяти: теперь мой адрес Москва, дом 17','update')
        t.search('адрес');v=t.read(['self.address'])['records'][0]['version']
        t.write('self.address','personal','мой адрес Москва, дом 17',expected_version=v)
        self.assertIn('17',self.store.list('a')[0]['evidence'])
    def test_retry_does_not_resurrect_deleted_record(self):
        t=self.tools();self.save(t);t.finish('saved')
        retry=self.tools();self.save(retry)
        self.store.forget('a','всё')
        with self.assertRaises(ValueError):self.save(self.tools())
    def test_confirm_requires_actual_write_and_answer_requires_full_read(self):
        t=self.tools()
        with self.assertRaises(ValueError):t.finish('saved')
        self.save(t);t.search('')
        with self.assertRaises(ValueError):t.finish('answer','Вы инженер')
        t.read(['self.profession']);t.finish('answer','Вы инженер')
        self.assertIn('инженером',t.finished.context)
        self.assertTrue(t.finished.force_local)
    def test_failed_write_cannot_pass_and_null_version_is_rejected(self):
        t=self.tools();t.search('')
        args={'slot':'self.profession','category':'profession','evidence':t.source,'expected_version':None}
        self.assertIn('error',t.execute('memory_update',args))
        with self.assertRaises(ValueError):t.write('self.profession','profession','Я врач')
        with self.assertRaises(ValueError):t.finish('pass')
        t.finish('clarify','Не удалось сохранить факт. Уточните формулировку.')
    def test_semantic_miss_exposes_candidates_for_model(self):
        t=self.tools();self.save(t)
        result=t.search('зарабатывать')
        self.assertFalse(result['query_matched'])
        self.assertEqual(result['records'][0]['slot'],'self.profession')
    def test_pagination(self):
        self.store.save('a','seed',[pm.Fact('personal',f'self.item{i}',f'Мой номер {i}') for i in range(15)])
        t=self.tools();first=t.search('',limit=10);second=t.search('',offset=10,limit=10)
        self.assertTrue(first['has_more']);self.assertFalse(second['has_more'])
        self.assertEqual(len(second['records']),5)
    def test_age_uses_read_date(self):
        self.store.save('a','seed',[pm.Fact('personal','self.birth_date','Я родился 15.06.1990')])
        t=self.tools()
        with self.assertRaises(ValueError):t.calculate_age('self.birth_date')
        t.read(['self.birth_date'])
        from datetime import date
        with patch.object(ma,'today_local',return_value=date(2026,6,14)):
            self.assertEqual(t.calculate_age('self.birth_date')['age_years'],35)
    def test_agent_uses_tools_and_finishes(self):
        def response(name,args):
            r=Mock();r.json.return_value={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}]};return r
        responses=[response('memory_search',{'query':''}),response('memory_save',{'slot':'self.profession','category':'profession','evidence':'Я работаю инженером'}),response('memory_finish',{'mode':'saved'})]
        with patch.object(ma,'model_post',side_effect=responses),patch.object(ma,'LOCAL_LM_MODEL','test'):
            outcome=ma.QwenMemoryAgent().run(self.store,'a','e','Я работаю инженером')
        self.assertIsNone(outcome.reply)
        self.assertIn('Сохранила',outcome.notice)
    def test_own_age_cannot_use_relative_birth_date(self):
        self.store.save('a','seed',[pm.Fact('family','family.wife.birth_date','Моя жена родилась 20 мая 1993 года')])
        t=self.tools('Сколько полных лет мне исполнилось?');t.read(['family.wife.birth_date'])
        with self.assertRaises(ValueError):t.calculate_age('family.wife.birth_date')
    def test_failure_never_confirms_save(self):
        with patch.object(ma,'model_post',side_effect=RuntimeError),patch.object(ma,'LOCAL_LM_MODEL','test'):
            outcome=ma.QwenMemoryAgent().run(self.store,'a','e','Сохрани мою профессию: я инженер')
        self.assertIn('не подтверждено',outcome.reply)
        self.assertFalse(self.store.list('a'))
    def test_semantic_path_and_context_reset(self):
        agent=Mock();agent.run.return_value=ma.AgentOutcome('Ответ','private',True)
        memory=pm.PersonalMemory(self.store,enabled=True,allowed_chats={'10'},agent=agent,use_agent=True)
        with memory.turn('Где я обитаю?','10','20','e') as direct:
            self.assertEqual(direct,'Ответ');self.assertEqual(pm.current_memory_turn().context,'private')
        self.assertEqual(pm.current_memory_turn().context,'')
        with memory.turn('Где я обитаю?','10',None,'x'):pass
        agent.run.assert_called_once()
