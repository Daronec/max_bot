"""Explicit local-process integration check; uses temporary data and a stub model.
Run from the project root. Requires permission to bind loopback sockets.
"""
import os,sys,json,tempfile,time,threading,socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
import requests
import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
from werkzeug.serving import make_server
from panel_auth import PanelAuth
from process_manager import ProcessManager
from supervisor import create_app
from task_store import SQLiteTaskStore
calls=[]
class Model(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  calls.append(payload)
  if payload.get('stream'):
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
   for delta in ({'reasoning_content':'DO_NOT_EXPOSE_REASONING'},{'content':'Код проекта: '},{'content':'Сапфир.'}):
    self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':delta}]})+'\n\n').encode());self.wfile.flush()
   self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':50,'completion_tokens':8}})+'\n\ndata: [DONE]\n\n').encode());self.wfile.flush()
   return
  result=json.dumps({'choices':[{'message':{'role':'assistant','content':'Код проекта: Сапфир.'}}],'usage':{'prompt_tokens':50,'completion_tokens':8}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(result)
def port():
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
def wait_for(check,timeout=20):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  try:
   value=check()
   if value:return value
  except (requests.RequestException,KeyError):pass
  time.sleep(.1)
 raise AssertionError('Timed out waiting for local component')
with tempfile.TemporaryDirectory() as temporary:
 d=Path(temporary);api_port=port()
 model=ThreadingHTTPServer(('127.0.0.1',0),Model)
 threading.Thread(target=model.serve_forever,daemon=True).start()
 model_url=f'http://127.0.0.1:{model.server_port}/v1'
 env=dict(os.environ,MAX_BOT_TOKEN='',MAX_BOT_ID='smoke',MAX_CHAT_ID='',DEEPSEEK_API_KEY='',LM_API_KEY='',LOCAL_LM_API_KEY='',PANEL_ACCESS_TOKEN='',PANEL_ALLOWED_HOSTS='localhost,127.0.0.1,::1',PANEL_AUTH_ENABLED='false',
  ASSISTANT_TASKS_DIR=str(d/'tasks'),PORT=str(api_port),HOST='127.0.0.1',WORKER_MODE='process',
  LOCAL_LM_BASE_URL=model_url,LM_BASE_URL=model_url,LOCAL_LM_MODEL='smoke-local',LM_MODEL='smoke-local',
  LM_PROVIDER='lmstudio',LM_API_MODE='openai',LM_AUTO_START='0',DEEPSEEK_ENABLED='false',
  TELEGRAM_ENABLED='false',TELEGRAM_BOT_TOKEN='',NOTIFICATIONS_ENABLED='false',SUMMARY_ENABLED='false',
  MOOD_ENABLED='false',MEMORY_ENABLED='false',CODE_CHANGES_ENABLED='false',FS_ENABLED='false',COMMANDS_ENABLED='false',
  WEB_ENABLED='false',VOICE_ENABLED='false',RAG_EMBEDDING_MODEL='',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
 manager=ProcessManager([sys.executable,'-u',str(root/'run.py')],root,d/'api.log',env,pid_path=d/'api.pid',stop_timeout=15)
 auth=PanelAuth(d/'secrets');app=create_app(manager,auth,f'http://127.0.0.1:{api_port}','smoke')
 panel=make_server('127.0.0.1',0,app,threaded=True)
 threading.Thread(target=panel.serve_forever,daemon=True).start()
 base=f'http://127.0.0.1:{panel.server_port}';headers={'Authorization':'Bearer '+auth.token}
 def api(path,method='GET',data=None):
  response=requests.request(method,base+path,headers=headers,json=data,timeout=30)
  response.raise_for_status();return response.json()
 try:
  manager.start();wait_for(lambda:api('/api/status')['worker_active'])
  document=api('/api/knowledge','POST',{'title':'project.md','text':'Код проекта: Сапфир.'})['document']
  created=api('/api/chat','POST',{'message':'Какой код проекта?'})
  answer=wait_for(lambda:(lambda result:result if result['status']=='completed' else None)(api('/api/result?task_id='+created['task_id'])))
  assert 'Сапфир' in answer['result'] and answer['sources'][0]['document_id']==document['id']
  assert 'Недоверенные выдержки' in calls[0]['messages'][-2]['content']
  old_pid=manager.status()['pid'];manager.process.kill()
  wait_for(lambda:manager.status()['restarts']==1)
  wait_for(lambda:api('/api/status')['worker_active'])
  assert manager.status()['pid']!=old_pid
  assert len(api('/api/conversations/'+created['conversation_id'])['messages'])==2
  api('/api/server','POST',{'action':'stop'})
  status=api('/api/status');assert status['supervisor']['active'] and not status['server_active']
  api('/api/server','POST',{'action':'start'});wait_for(lambda:api('/api/status')['worker_active'])
  second=api('/api/chat','POST',{'message':'Повтори ответ','conversation_id':created['conversation_id']})
  wait_for(lambda:api('/api/result?task_id='+second['task_id'])['status']=='completed')
  assert any(message['role']=='assistant' for message in calls[-1]['messages'])
  registry_model=dict(id='integration',model='smoke-local',base_url=model_url,local=True,tools=True,context_tokens=32768,output_tokens=1024)
  api('/api/models','POST',{'version':0,'models':[registry_model]})
  registered=api('/api/chat','POST',{'message':'Проверка реестра','model_target':'model:integration','use_knowledge':False})
  wait_for(lambda:api('/api/result?task_id='+registered['task_id'])['status']=='completed')
  task=SQLiteTaskStore(d/'tasks').get(registered['task_id'])
  assert task['routing']['target']=='model:integration' and 'DO_NOT_EXPOSE_REASONING' not in json.dumps(task)
  project=api('/api/projects','POST',{'title':'Изолированный проект','notes':'Рабочий маркер: Гелиос'})['id']
  api('/api/knowledge','POST',{'title':'isolated.md','text':'Секрет проекта: Гелиос','project_id':project})
  project_chat=api('/api/chat','POST',{'message':'Гелиос','project_id':project})
  wait_for(lambda:api('/api/result?task_id='+project_chat['task_id'])['status']=='completed')
  assert 'Гелиос' in json.dumps(calls[-1]['messages'],ensure_ascii=False)
  schedule=api('/api/automations','POST',{'action':'create','config':{'title':'Обзор','prompt':'Гелиос','kind':'interval','interval':60,'missed':'once','project_id':project}})['id']
  with SQLiteTaskStore(d/'tasks').connect() as db:db.execute('UPDATE automations SET next_due=? WHERE id=?',(time.time(),schedule))
  scheduled=wait_for(lambda:next((task for task in SQLiteTaskStore(d/'tasks').all() if task.get('automation_id')==schedule and task['status']=='completed'),None))
  assert scheduled['project_id']==project
  SQLiteTaskStore(d/'tasks').backup(d/'backup.sqlite3')
  print(json.dumps({'result':'passed','checks':['local_chat','streaming_without_reasoning','registered_model','project_isolation','durable_schedule','rag_sources','worker_process','api_crash_restart','history_persistence','independent_control_plane','stop_start','database_backup'],'model_calls':len(calls)}))
 finally:
  manager.stop();panel.shutdown();model.shutdown()
