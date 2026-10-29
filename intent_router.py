"""Offline intent decisions; provisioning model files is a separate setup step."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import re
import threading
import time

from config import (ROUTER_CONFIDENCE_THRESHOLD, DEEPSEEK_ENABLED,
                    DEEPSEEK_COOLDOWN_SECONDS, DEEPSEEK_API_KEY)

logger = logging.getLogger(__name__)
MODEL_ID = 'MoritzLaurer/mDeBERTa-v3-base-mnli-xnli'
# English NLI hypotheses classify Russian input directly; no translation is used.
# These descriptions performed better than Russian hypotheses in smoke tests.
INTENT_DESCRIPTIONS = {
    'code_generation': 'writing or debugging computer code',
    'web_search': 'searching the web for current information',
    'math_calculation': 'mathematical calculations',
    'file_operation': 'working with files and directories',
    'api_call': 'calling an external API',
    'creative_writing': 'creative writing',
    'data_analysis': 'analyzing data or evaluating performance',
    'general_chat': 'greetings, thanks or casual conversation',
}
DESCRIPTION_TO_INTENT = {description: intent for intent, description in INTENT_DESCRIPTIONS.items()}
HYPOTHESIS_TEMPLATE = 'This text is about {}.'
REMOTE_INTENTS = frozenset(('data_analysis', 'creative_writing', 'api_call', 'web_search'))
LOCAL_INTENTS = frozenset(('code_generation', 'math_calculation', 'file_operation', 'general_chat'))
LOCAL_RULE = re.compile(
    r'без\s+интернета|ответь\s+локально|(?:только|строго)\s+локально|офлайн|'
    r'\boffline\b|without\s+(?:the\s+)?internet|\blocal\s+only\b|answer\s+locally|'
    r'不开网络|不联网|离线|'
    r'(?:открой|открыть|прочитай|прочитать|измени|изменить|создай|удали|сохрани|найди)\s+(?:(?:локальн\w*|этот|этот\s+локальный)\s+)?(?:файл|папк|каталог)|'
    r'(?:прочитай|читай)\s+локально|(?:запусти|выполни|запустить|выполнить)\s+(?:(?:локальн\w*|этот)\s+)?(?:скрипт|команд|приложени|тест)|'
    r'\b(?:open|read|write|edit|delete|find)\s+(?:(?:a|the|local)\s+)*(?:file|folder|directory)\b|'
    r'\b(?:run|execute|launch)\s+(?:(?:a|the|local)\s+)*(?:script|command|application|tests)\b|'
    r'\b(?:read_file|write_file|edit_file|run_command|launch_application|create_reminder)\b|'
    r'(?:создай|добавь|сохрани)\s+(?:контакт|напоминан)|напомни\b', re.I)


class IntentClassifier:
    """One lazily loaded CPU pipeline, including a cached failure if not provisioned."""
    def __init__(self):
        self._pipeline = None
        self._error = None
        self._lock = threading.Lock()

    def classify(self, text):
        with self._lock:
            if self._error is not None:
                raise RuntimeError('Intent model unavailable; provision it and restart') from self._error
            if self._pipeline is None:
                try:
                    from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline
                    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, local_files_only=True)
                    model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, local_files_only=True)
                    tokenizer.model_max_length = min(tokenizer.model_max_length, 512)
                    self._pipeline = pipeline('zero-shot-classification', model=model, tokenizer=tokenizer, device=-1)
                except Exception as error:
                    self._error = error
                    logger.warning('Intent model could not be loaded from local cache: %s', type(error).__name__)
                    raise
            result = self._pipeline(
                text, candidate_labels=list(INTENT_DESCRIPTIONS.values()),
                hypothesis_template=HYPOTHESIS_TEMPLATE, multi_label=False,
            )
            # Scores are relative to these eight candidates, not calibrated accuracy.
            label = DESCRIPTION_TO_INTENT[result['labels'][0]]
            return label, float(result['scores'][0])


class DeepSeekAvailability:
    """Local circuit breaker; after cooldown the next candidate is a trial request."""
    def __init__(self, available=True, cooldown=DEEPSEEK_COOLDOWN_SECONDS, clock=time.monotonic):
        self._available = available
        self._retry_at = None
        self._clock = clock
        self.cooldown = cooldown
        self._lock = threading.Lock()

    def available(self):
        with self._lock:
            return self._available or (self._retry_at is not None and self._clock() >= self._retry_at)

    def retry_in_seconds(self):
        with self._lock:
            return max(0, round((self._retry_at or 0) - self._clock()))

    def mark_unavailable(self):
        with self._lock:
            self._available = False
            self._retry_at = self._clock() + self.cooldown

    def mark_available(self):
        with self._lock:
            self._available = True
            self._retry_at = None


@dataclass(frozen=True)
class RouteDecision:
    target: str
    label: str | None
    score: float | None
    reason: str


class IntentRouter:
    def __init__(self, classifier=None, availability=None, enabled=DEEPSEEK_ENABLED,
                 threshold=ROUTER_CONFIDENCE_THRESHOLD):
        self.classifier = classifier if classifier is not None else IntentClassifier()
        self.availability = availability if availability is not None else deepseek_availability
        self.enabled = enabled
        self.threshold = threshold

    def route(self, text):
        label = score = None
        target = 'qwen'
        if LOCAL_RULE.search(text):
            reason = 'local_rule'
        elif not self.enabled:
            reason = 'deepseek_disabled'
        else:
            try:
                label, score = self.classifier.classify(text)
                if not math.isfinite(score) or not 0 <= score <= 1:
                    reason = 'invalid_score'
                elif score < self.threshold:
                    reason = 'low_confidence'
                elif label in LOCAL_INTENTS:
                    reason = 'local_intent'
                elif label not in REMOTE_INTENTS:
                    reason = 'unknown_intent'
                elif not self.availability.available():
                    reason = 'deepseek_unavailable'
                else:
                    target, reason = 'deepseek', 'candidate_available'
            except Exception as error:
                reason = 'classifier_unavailable'
                logger.debug('Intent inference failed: %s', type(error).__name__)
        decision = RouteDecision(target, label, score, reason)
        logger.info('intent_route message=%r label=%s score=%s target=%s reason=%s',
                    text[:160], label, score, target, reason)
        return decision


# Configured credentials are initial permission to try, not a claim of verified connectivity.
deepseek_availability = DeepSeekAvailability(available=bool(DEEPSEEK_API_KEY))
intent_router = IntentRouter()
