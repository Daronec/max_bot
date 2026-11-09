import json
import unittest
from unittest.mock import Mock
import requests
from assistant_app.core.execution_context import ExecutionContext,execution_session
from assistant_app.models.stream import consume


def response(events):
    obj=Mock(status_code=200,headers={'Content-Type':'text/event-stream'})
    obj.iter_lines.return_value=[('data: '+json.dumps(e)).encode() if isinstance(e,dict) else e for e in events]
    return obj


class StreamTests(unittest.TestCase):
    def test_content_tool_deltas_and_usage_without_reasoning_preview(self):
        raw=response([{'choices':[{'delta':{'reasoning_content':'private reasoning'}}]},
                      {'choices':[{'delta':{'content':'Hello'}}]},
                      {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call1','function':{'name':'read_file','arguments':'{"path":'}}]}}]},
                      {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"note.txt"}'}}]},'finish_reason':'tool_calls'}]},
                      {'usage':{'total_tokens':10},'choices':[]},b'data: [DONE]'])
        seen=[]
        with execution_session(ExecutionContext(progress_observer=seen.append)):result=consume(raw).json()
        self.assertNotIn('private reasoning',str(result)+str(seen))
        self.assertEqual(result['choices'][0]['message']['tool_calls'][0]['function']['arguments'],'{"path":"note.txt"}')
        self.assertEqual(result['usage']['total_tokens'],10)
        raw.close.assert_called_once()

    def test_interruption_never_becomes_completed_answer(self):
        raw=response([{'choices':[{'delta':{'content':'incomplete'}}]}])
        with self.assertRaises(requests.RequestException):consume(raw)
        raw.close.assert_called_once()


if __name__=='__main__':unittest.main()
