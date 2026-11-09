"""Explicit readiness/latency check with a configured local model; no personal data."""
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
import requests
from config import LOCAL_LM_BASE_URL,LOCAL_LM_MODEL,LOCAL_LM_API_KEY
parsed=urlsplit(LOCAL_LM_BASE_URL)
if parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.scheme!='http':raise SystemExit('Refusing a non-local model endpoint')
headers={'Authorization':'Bearer '+LOCAL_LM_API_KEY} if LOCAL_LM_API_KEY else {}
try:
 started=time.monotonic()
 response=requests.get(LOCAL_LM_BASE_URL+'/models',headers=headers,timeout=5,allow_redirects=False);response.raise_for_status()
 ids=[m['id'] for m in response.json()['data'] if 'embed' not in m['id'].lower()]
 model=LOCAL_LM_MODEL or (ids[0] if ids else None)
 if not model:raise ValueError('No local language model')
 response=requests.post(LOCAL_LM_BASE_URL+'/chat/completions',headers=headers,json=dict(model=model,messages=[dict(role='user',content='Вычисли 2+2. Ответь только числом.')],max_tokens=256,stream=False,reasoning_effort="none"),timeout=(5,90),allow_redirects=False);response.raise_for_status()
 answer=response.json()['choices'][0]['message']['content']
 assert isinstance(answer,str) and '4' in answer,'Unexpected arithmetic answer'
 print(json.dumps(dict(result='passed',local=True,latency_seconds=round(time.monotonic()-started,3),usage=response.json().get('usage',{}))))
except Exception as error:
 print(json.dumps(dict(result='unavailable',error=type(error).__name__)))
 raise SystemExit(1)
