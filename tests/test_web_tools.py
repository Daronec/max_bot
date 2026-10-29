import bot_test_support  # Load synthetic configuration before application imports.
import json
import socket
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import web_tools
import task_processor
from intent_router import RouteDecision


class WebToolsTests(unittest.TestCase):
    def test_rejects_private_and_non_web_urls(self):
        for url in ('file:///etc/passwd', 'ftp://example.com', 'http://user:password@example.com'):
            with self.assertRaises(ValueError):
                web_tools.public_target(url)
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '192.168.1.1'):
            with patch.object(socket, 'getaddrinfo', return_value=[(0,0,0,'',(ip,80))]):
                with self.assertRaises(ValueError):
                    web_tools.public_target('http://private.test')

    def test_mixed_public_private_dns_is_rejected(self):
        with patch.object(socket, 'getaddrinfo', return_value=[
                (0,0,0,'',('8.8.8.8',80)), (0,0,0,'',('127.0.0.1',80))]):
            with self.assertRaises(ValueError):
                web_tools.public_target('http://mixed.test')

    def test_extracts_readable_text_links_and_omits_scripts(self):
        html = b'<html><title>Title</title><nav>menu</nav><main><h1>Hello</h1><script>secret()</script><a href="/docs">Docs</a></main></html>'
        with patch.object(web_tools, 'download_page', return_value=('https://example.com', html)):
            result = web_tools.open_url('https://example.com')
        self.assertEqual(result['title'], 'Title')
        self.assertIn('Hello', result['text'])
        self.assertNotIn('secret', result['text'])
        self.assertNotIn('menu', result['text'])
        self.assertEqual(result['links'][0]['url'], 'https://example.com/docs')
        self.assertTrue(result['untrusted_content'])

    def test_long_text_is_explicitly_truncated(self):
        with patch.object(web_tools, 'WEB_MAX_CHARS', 20), \
                patch.object(web_tools, 'download_page', return_value=('https://example.com', b'<p>' + b'x'*50 + b'</p>')):
            result = web_tools.open_url('https://example.com')
        self.assertEqual(len(result['text']), 20)
        self.assertTrue(result['truncated'])

    def test_search_returns_urls_and_limits_result_count(self):
        with patch.object(web_tools, 'DDGS') as ddgs:
            ddgs.return_value.text.return_value = [{'title':'Python','href':'https://python.org','body':'Official'}]
            result = web_tools.web_search('Python', 1000)
            self.assertEqual(ddgs.return_value.text.call_args.kwargs['max_results'], 8)
            self.assertEqual(result['results'][0]['url'], 'https://python.org')
        self.assertIn('error', web_tools.web_search(''))

    def test_pinned_public_ip_keeps_original_tls_hostname(self):
        response = MagicMock(status=200, headers={'Content-Type':'text/html'})
        response.stream.return_value = [b'hello']
        with patch.object(web_tools, 'public_target', return_value=(urlsplit('https://example.com/page'), 'example.com',443,'8.8.8.8')), \
                patch.object(web_tools.urllib3, 'HTTPSConnectionPool') as pool:
            pool.return_value.request.return_value = response
            self.assertEqual(web_tools.download_page('https://example.com/page')[1], b'hello')
            self.assertEqual(pool.call_args.kwargs['host'], '8.8.8.8')
            self.assertEqual(pool.call_args.kwargs['assert_hostname'], 'example.com')
            self.assertEqual(pool.call_args.kwargs['cert_reqs'], 'CERT_REQUIRED')
            self.assertFalse(pool.return_value.request.call_args.kwargs['redirect'])

    def test_redirect_target_is_revalidated(self):
        response = MagicMock(status=302, headers={'Location':'http://127.0.0.1/admin'})
        with patch.object(web_tools, 'public_target', side_effect=[
                (urlsplit('https://example.com'), 'example.com',443,'8.8.8.8'), ValueError('private')]) as validate, \
                patch.object(web_tools.urllib3, 'HTTPSConnectionPool') as pool:
            pool.return_value.request.return_value = response
            self.assertIn('error', web_tools.open_url('https://example.com'))
            self.assertEqual(validate.call_args.args[0], 'http://127.0.0.1/admin')

    def test_binary_and_oversize_pages_are_rejected(self):
        for content_type, chunks in [('application/octet-stream', [b'x']), ('text/html', [b'x'*100])]:
            response = MagicMock(status=200, headers={'Content-Type':content_type})
            response.stream.return_value = chunks
            with patch.object(web_tools, 'WEB_MAX_BYTES', 50), \
                    patch.object(web_tools, 'public_target', return_value=(urlsplit('https://example.com'), 'example.com',443,'8.8.8.8')), \
                    patch.object(web_tools.urllib3, 'HTTPSConnectionPool') as pool:
                pool.return_value.request.return_value = response
                self.assertIn('error', web_tools.open_url('https://example.com'))

    def test_no_arbitrary_tool_dispatch(self):
        self.assertIn('error', web_tools.execute_tool('eval', {'code':'anything'}))
        self.assertIn('error', web_tools.execute_tool('open_url', {'unexpected':'value'}))


class ModelWebIntegrationTests(unittest.TestCase):
    def setUp(self):
        routing = patch.object(task_processor.intent_router, 'route', return_value=RouteDecision('qwen', 'general_chat', .9, 'test'))
        routing.start()
        self.addCleanup(routing.stop)
        provider = patch.object(task_processor, 'LM_PROVIDER', 'lmstudio')
        provider.start()
        self.addCleanup(provider.stop)

    def test_web_uses_standard_tool_calls_even_with_native_preference(self):
        with patch.object(task_processor, 'LM_PROVIDER', 'lmstudio'), \
                patch.object(task_processor, 'LM_API_MODE', 'lmstudio'), \
                patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor, 'WEB_ENABLED', True), \
                patch.object(task_processor.requests, 'post') as post:
            post.return_value.json.return_value = {'choices':[{'message':{'content':'answer'}}]}
            task_processor.process_message('task')
            payload = post.call_args.kwargs['json']
            self.assertEqual([t['function']['name'] for t in payload['tools']], ['web_search','open_url'])
            self.assertNotIn('integrations', payload)
            self.assertEqual(payload['reasoning_effort'], 'none')

    def test_openai_tool_results_are_returned_to_model(self):
        response1 = MagicMock()
        response1.json.return_value = {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
            {'id':'call1','type':'function','function':{'name':'web_search','arguments':json.dumps({'query':'Python'})}}]}}]}
        response2 = MagicMock()
        response2.json.return_value = {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
            {'id':'call2','type':'function','function':{'name':'open_url','arguments':json.dumps({'url':'https://python.org'})}}]}}]}
        response3 = MagicMock()
        response3.json.return_value = {'choices':[{'message':{'role':'assistant','content':'See https://python.org'}}]}
        with patch.object(task_processor, 'LM_API_MODE', 'openai'), \
                patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor, 'WEB_ENABLED', True), \
                patch.object(task_processor.requests, 'post', side_effect=[response1,response2,response3]) as post, \
                patch.object(web_tools, 'execute_tool', side_effect=[
                    {'results':[{'url':'https://python.org'}]},
                    {'url':'https://python.org','text':'Official Python website'}]) as execute:
            answer = task_processor.process_message('find Python')
            self.assertIn('https://python.org', answer)
            self.assertEqual(execute.call_count, 2)
            messages = post.call_args.kwargs['json']['messages']
            self.assertEqual(messages[-1]['tool_call_id'], 'call2')
            self.assertIn('https://python.org', messages[-1]['content'])

    def test_explicit_web_request_cannot_return_an_unverified_answer(self):
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor, 'WEB_ENABLED', True), \
                patch.object(task_processor.requests, 'post') as post:
            post.return_value.json.return_value = {'choices':[{'message':{'content':'I searched'}}]}
            with self.assertRaises(task_processor.ProcessingError):
                task_processor.process_message('Найди информацию в интернете')
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['tool_choice'], 'required')
            self.assertEqual([tool['function']['name'] for tool in payload['tools']], ['web_search'])

    def test_missed_tool_call_is_retried_and_source_is_attached(self):
        responses = []
        for message in (
            {'content':'answer from memory'},
            {'content':None, 'tool_calls':[{'id':'open1','function':{
                'name':'open_url','arguments':json.dumps({'url':'https://example.com'})}}]},
            {'content':'Example Domain'},
        ):
            response = MagicMock()
            response.json.return_value = {'choices':[{'message':message}]}
            responses.append(response)
        with patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor, 'WEB_ENABLED', True), \
                patch.object(task_processor.requests, 'post', side_effect=responses), \
                patch.object(web_tools, 'execute_tool', return_value={'url':'https://example.com','text':'Example Domain'}) as execute:
            answer = task_processor.process_message('Прочитай https://example.com')
            self.assertIn('Example Domain', answer)
            self.assertIn('https://example.com', answer)
            execute.assert_called_once_with('open_url', {'url':'https://example.com'})

    def test_disabled_web_sends_no_native_integrations(self):
        with patch.object(task_processor, 'LM_API_MODE', 'lmstudio'), \
                patch.object(task_processor, 'LM_MODEL', 'local'), \
                patch.object(task_processor, 'WEB_ENABLED', False), \
                patch.object(task_processor.requests, 'post') as post:
            post.return_value.json.return_value = {'output':[{'type':'message','content':'answer'}]}
            task_processor.process_message('task')
            self.assertNotIn('integrations', post.call_args.kwargs['json'])


if __name__ == '__main__':
    unittest.main()
