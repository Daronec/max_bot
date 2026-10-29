import bot_test_support  # Load synthetic configuration before application imports.
"""Optional offline regressions against real cached weights, not part of unit runs."""
import os
import unittest
from unittest.mock import patch

from intent_router import IntentClassifier, IntentRouter, DeepSeekAvailability


@unittest.skipUnless(os.environ.get('RUN_INTENT_MODEL_TESTS') == '1', 'requires cached mDeBERTa weights')
class RussianModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network_guard = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        cls.network_guard.start()
        cls.addClassCleanup(cls.network_guard.stop)
        cls.classifier = IntentClassifier()

    def test_russian_analytical_request(self):
        text = 'проанализируй производительность этого кода'
        label, score = self.classifier.classify(text)
        self.assertEqual(label, 'data_analysis')
        self.assertGreaterEqual(score, .55)
        for available in (True, False):
            router = IntentRouter(self.classifier, DeepSeekAvailability(available), enabled=True)
            self.assertEqual(router.route(text).target, 'deepseek' if available else 'qwen')

    def test_russian_greeting_stays_local(self):
        router = IntentRouter(self.classifier, DeepSeekAvailability(True), enabled=True)
        self.assertEqual(router.route('привет').target, 'qwen')
