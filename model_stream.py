"""Consume bounded Chat Completions SSE; previews contain public content only."""
import json
import time
import requests
from execution_context import check_execution,current_execution


class StreamResponse:
    def __init__(self,response,payload):self.response=response;self.payload=payload;self.status_code=response.status_code
    def raise_for_status(self):self.response.raise_for_status()
    def json(self):return self.payload


def consume(response):
    content_type=response.headers.get('Content-Type','')
    if not isinstance(content_type,str) or 'text/event-stream' not in content_type or response.status_code!=200:return response
    content='';calls={};usage={};finished=False;done=False;total=0;last=0
    observer=current_execution().progress_observer
    try:
        for line in response.iter_lines(chunk_size=1024,decode_unicode=False):
            check_execution()
            if not line:continue
            if isinstance(line,bytes):line=line.decode('utf-8')
            total+=len(line.encode('utf-8'))
            if total>16*1024*1024:raise requests.RequestException('Model stream exceeds limits')
            if not line.startswith('data:'):continue
            raw=line[5:].strip()
            if raw=='[DONE]':done=True;break
            data=json.loads(raw)
            if data.get('error'):raise requests.RequestException('Model stream failed')
            if isinstance(data.get('usage'),dict):usage=data['usage']
            for choice in data.get('choices',[]):
                if choice.get('index',0)!=0:continue
                delta=choice.get('delta') or {}
                # Reasoning and provider internal fields are intentionally excluded.
                if isinstance(delta.get('content'),str):content+=delta['content']
                if len(content)>1000000:raise requests.RequestException('Model answer exceeds limits')
                for call in delta.get('tool_calls') or []:
                    index=call.get('index')
                    if not isinstance(index,int) or not 0<=index<8:raise ValueError('Invalid streamed tool index')
                    stored=calls.setdefault(index,dict(id='',type='function',function=dict(name='',arguments='')))
                    if call.get('id'):stored['id']=call['id']
                    for key in ('name','arguments'):
                        value=(call.get('function') or {}).get(key)
                        if isinstance(value,str):stored['function'][key]+=value
                        if len(stored['function'][key])>1000000:raise ValueError('Streamed tool arguments exceed limits')
                if choice.get('finish_reason') is not None:finished=True
            if observer and content and time.monotonic()-last>=.25:
                observer(content);last=time.monotonic()
        if not done and not finished:raise requests.RequestException('Model stream was interrupted')
        if observer and content:observer(content)
        message=dict(role='assistant',content=content or None)
        if calls:message['tool_calls']=[calls[index] for index in sorted(calls)]
        return StreamResponse(response,dict(choices=[dict(message=message)],usage=usage))
    finally:response.close()
