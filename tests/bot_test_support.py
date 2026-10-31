"""Synthetic settings for unit tests; imported before application modules."""
import os
os.environ.update({
    'MAX_BOT_TOKEN': 'unit-test-token',
    'MAX_BOT_ID': '@unit_test_bot',
    'MAX_CHAT_ID': '',
    'DEEPSEEK_API_KEY': 'unit-test-key',
    'LM_API_KEY': '',
    'LOCAL_LM_API_KEY': '',
    'LM_PROVIDER': 'lmstudio',
    'FS_ALLOWED_CHAT_IDS': '123456789',
    'NOTIFICATIONS_CHAT_ID': '123456789',
    'SUMMARY_CHAT_ID': '123456789',
    'MOOD_CHAT_ID': '123456789',
    'MEMORY_ENABLED': 'true',
    'MEMORY_ALLOWED_CHAT_IDS': '123456789',
    'CODE_CHANGES_ENABLED': 'true',
    'CODE_ALLOWED_CHAT_IDS': '123456789',
    'CODE_ALLOWED_SENDER_IDS': 'unit-test-sender',
})
