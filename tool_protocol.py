"""Normalize structured and Qwen textual tool calls; never evaluate arguments."""
import html
import json
import re
import uuid

MARKUP = re.compile(r'<(?:tool_call|function[=>])', re.I)


def tool_calls(message, schemas):
    native = message.get('tool_calls')
    if isinstance(native, list) and native:
        return native
    text = message.get('content') or ''
    if not isinstance(text, str) or len(text) > 100000:
        return []
    definitions = {s['function']['name']: s['function']['parameters'] for s in schemas}
    calls = []
    for block in re.findall(r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.S):
        try:
            if block.lstrip().startswith('{'):
                obj = json.loads(block)
                name, arguments = obj['name'], obj.get('arguments', {})
                if isinstance(arguments, str):arguments = json.loads(arguments)
            else:
                function = re.fullmatch(r'<function=([a-zA-Z_]\w*)>\s*(.*?)\s*</function>', block.strip(), re.S)
                if not function:continue
                name, body = function.groups()
                arguments = {}
                for key, value in re.findall(r'<parameter=([a-zA-Z_]\w*)>\s*(.*?)\s*</parameter>', body, re.S):
                    value = html.unescape(value)
                    rule = definitions.get(name, {}).get('properties', {}).get(key, {})
                    if rule.get('type') in ('integer','number','boolean','array','object'):
                        value = json.loads(value)
                    arguments[key] = value
            if name not in definitions or not isinstance(arguments, dict):continue
            calls.append({'id':'text_'+uuid.uuid4().hex,'type':'function',
                          'function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}})
        except (ValueError,KeyError,TypeError):
            continue
    return calls[:8]
