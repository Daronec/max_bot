import bot_test_support  # Load synthetic configuration before application imports.
import concurrent.futures
import sys
import unittest
from unittest.mock import Mock, patch
import requests
import intent_router as ir
import lm_connection as lm
import task_processor as processor


class RouterTests(unittest.TestCase):
    def router(self, label='data_analysis', score=.9, available=True, enabled=True):
        classifier = Mock()
        classifier.classify.return_value = label, score
        return ir.IntentRouter(classifier, ir.DeepSeekAvailability(available), enabled=enabled)

    def test_all_classes_and_russian_examples(self):
        for label in ir.LOCAL_INTENTS | ir.REMOTE_INTENTS:
            expected = 'deepseek' if label in ir.REMOTE_INTENTS else 'qwen'
            self.assertEqual(self.router(label).route('message').target, expected)
        self.assertEqual(self.router('general_chat').route('привет').target, 'qwen')
        for available in (True, False):
            self.assertEqual(self.router(available=available).route('проанализируй производительность этого кода').target,
                             'deepseek' if available else 'qwen')

    def test_confidence_and_unknown_labels(self):
        for score in (0, .549, float('nan'), float('inf'), -1, 1.1):
            self.assertEqual(self.router(score=score).route('message').target, 'qwen')
        self.assertEqual(self.router(score=.55).route('message').target, 'deepseek')
        self.assertEqual(self.router('LABEL_0').route('message').target, 'qwen')

    def test_rules_and_disabled_skip_classifier_and_availability(self):
        for message in ('открой локальный файл', 'открой файл', 'запусти скрипт',
                        'прочитай локально', 'без интернета проанализируй данные',
                        'ответь локально', 'run the script', 'read the local file',
                        'without internet', 'offline', '离线'):
            router = self.router()
            with patch.object(router.availability, 'available') as available:
                self.assertEqual(router.route(message).reason, 'local_rule')
                router.classifier.classify.assert_not_called()
                available.assert_not_called()
        router = self.router(enabled=False)
        with patch.object(router.availability, 'available') as available:
            self.assertEqual(router.route('analyse data').reason, 'deepseek_disabled')
            router.classifier.classify.assert_not_called()
            available.assert_not_called()

    def test_classifier_failure(self):
        router = self.router()
        router.classifier.classify.side_effect = RuntimeError('missing model')
        self.assertEqual(router.route('message').reason, 'classifier_unavailable')

    def test_local_intent_and_low_confidence_skip_availability(self):
        for label, score in [('general_chat', .99), ('data_analysis', .1)]:
            router = self.router(label, score)
            with patch.object(router.availability, 'available') as available:
                router.route('message')
                available.assert_not_called()

    def test_cooldown(self):
        clock = Mock(return_value=100)
        state = ir.DeepSeekAvailability(True, cooldown=60, clock=clock)
        state.mark_unavailable()
        clock.return_value = 159
        self.assertFalse(state.available())
        clock.return_value = 160
        self.assertTrue(state.available())
        state.mark_available()
        clock.return_value = 0
        self.assertTrue(state.available())

    def test_pipeline_loaded_once_offline_with_parallel_calls(self):
        fake = Mock()
        fake.AutoTokenizer.from_pretrained.return_value.model_max_length = 512
        fake.pipeline.return_value.return_value = {'labels': [ir.INTENT_DESCRIPTIONS['general_chat']], 'scores': [.9]}
        classifier = ir.IntentClassifier()
        with patch.dict(sys.modules, transformers=fake):
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                self.assertEqual(list(pool.map(classifier.classify, ['hello'] * 8)), [('general_chat', .9)] * 8)
        fake.pipeline.assert_called_once_with('zero-shot-classification',
            model=fake.AutoModelForSequenceClassification.from_pretrained.return_value,
            tokenizer=fake.AutoTokenizer.from_pretrained.return_value, device=-1)
        fake.pipeline.return_value.assert_called_with(
            'hello', candidate_labels=list(ir.INTENT_DESCRIPTIONS.values()),
            hypothesis_template=ir.HYPOTHESIS_TEMPLATE, multi_label=False)
        for loader in (fake.AutoTokenizer, fake.AutoModelForSequenceClassification):
            loader.from_pretrained.assert_called_once_with(ir.MODEL_ID, local_files_only=True)

    def test_zero_shot_maps_all_descriptions_to_stable_intent_ids(self):
        classifier = ir.IntentClassifier()
        classifier._pipeline = Mock()
        for label, description in ir.INTENT_DESCRIPTIONS.items():
            classifier._pipeline.return_value = {'labels': [description], 'scores': [.87]}
            self.assertEqual(classifier.classify('русский запрос'), (label, .87))
        self.assertEqual(set(ir.INTENT_DESCRIPTIONS), ir.LOCAL_INTENTS | ir.REMOTE_INTENTS)

    def test_zero_shot_unknown_output_fails_closed(self):
        classifier = ir.IntentClassifier()
        classifier._pipeline = Mock(return_value={'labels': ['unknown'], 'scores': [.99]})
        self.assertEqual(ir.IntentRouter(classifier, ir.DeepSeekAvailability(True)).route('запрос').target, 'qwen')

    def test_missing_cache_not_reloaded(self):
        fake = Mock()
        fake.AutoTokenizer.from_pretrained.side_effect = OSError('not cached')
        classifier = ir.IntentClassifier()
        with patch.dict(sys.modules, transformers=fake):
            for _ in range(2):
                with self.assertRaises((OSError, RuntimeError)):
                    classifier.classify('hello')
        fake.AutoTokenizer.from_pretrained.assert_called_once()

    def test_logging(self):
        with self.assertLogs('intent_router', level='INFO') as logs:
            self.router().route('analyse data')
        for field in ('message=', 'label=data_analysis', 'score=0.9', 'target=deepseek', 'reason='):
            self.assertIn(field, logs.output[0])


class RoutedFlowTests(unittest.TestCase):
    def response(self, answer):
        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': answer}}]}
        return response

    def test_errors_return_qwen_and_cooldown_blocks_next_attempt(self):
        for error in (requests.Timeout(), requests.ConnectionError(), requests.RequestException(),
                      requests.HTTPError(response=Mock(status_code=400))):
            with self.subTest(error=type(error).__name__):
                state = ir.DeepSeekAvailability(True)
                classifier = Mock()
                classifier.classify.return_value = ('data_analysis', .99)
                router = ir.IntentRouter(classifier, state)
                with patch.object(processor, 'intent_router', router), patch.object(ir, 'deepseek_availability', state), \
                     patch.object(lm, '_request', side_effect=[error, self.response('Локальный ответ'), self.response('Ещё ответ')]) as request:
                    self.assertEqual(processor.process_message('проанализируй данные'), 'Локальный ответ')
                    self.assertFalse(state.available())
                    self.assertEqual(processor.process_message('проанализируй данные'), 'Ещё ответ')
                    self.assertEqual(request.call_count, 3)
                    self.assertIn('api.deepseek.com', request.call_args_list[0].args[1])
                    for call in request.call_args_list[1:]:
                        self.assertIn('127.0.0.1', call.args[1])
                        self.assertEqual(call.kwargs['json']['model'], 'qwen/qwen3.5-9b')

    def test_http_error_falls_back_even_if_legacy_fallback_disabled(self):
        state = ir.DeepSeekAvailability(True)
        with lm.intent_session(ir.RouteDecision('deepseek', 'data_analysis', .9, 'test')), \
             patch.object(ir, 'deepseek_availability', state), patch.object(lm, 'LM_FALLBACK_ENABLED', False), \
             patch.object(lm, '_request', side_effect=[Mock(status_code=400), self.response('answer')]) as request:
            lm.model_post('https://api.deepseek.com/v1/chat/completions', json={'messages': []})
            self.assertEqual(request.call_count, 2)
            self.assertFalse(state.available())

    def test_disabled_and_device_rule_never_send_remote(self):
        for text, enabled in [('analyse data', False), ('открой локальный файл', True)]:
            classifier = Mock()
            router = ir.IntentRouter(classifier, ir.DeepSeekAvailability(True), enabled=enabled)
            with patch.object(processor, 'intent_router', router), \
                 patch.object(lm, '_request', return_value=self.response('answer')) as request:
                self.assertEqual(processor.process_message(text), 'answer')
                self.assertIn('127.0.0.1', request.call_args.args[1])
                classifier.classify.assert_not_called()

    def test_mood_respects_offline_mode(self):
        router = ir.IntentRouter(Mock(), ir.DeepSeekAvailability(True), enabled=False)
        with patch.object(processor, 'intent_router', router), \
             patch.object(lm, '_request', return_value=self.response('Комментарий')) as request:
            self.assertEqual(processor.comment_on_mood('Хорошо'), 'Комментарий')
            self.assertIn('127.0.0.1', request.call_args.args[1])
            router.classifier.classify.assert_not_called()

    def test_context_restored_after_exception(self):
        self.assertIsNone(lm.current_backend())
        with self.assertRaises(RuntimeError):
            with lm.intent_session(ir.RouteDecision('qwen', None, None, 'test')):
                raise RuntimeError()
        self.assertIsNone(lm.current_backend())
