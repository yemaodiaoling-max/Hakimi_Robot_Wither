"""Translate the bounded image/text request into OpenAI Responses SSE."""
import json

from .gateway import CognitionUnavailable


def request_body(chat):
    inputs = []
    for message in chat['messages']:
        content = message['content']
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        parts = []
        for part in content:
            if part['type'] == 'text':
                parts.append({'type': 'input_text', 'text': part['text']})
            elif part['type'] == 'image_url':
                image = part['image_url']
                parts.append({'type': 'input_image', 'image_url': image['url'], 'detail': image['detail']})
            else:
                raise CognitionUnavailable('provider_media_unsupported')
        inputs.append({'role': message['role'], 'content': parts})
    body = {'model': chat['model'], 'input': inputs, 'stream': True,
            'max_output_tokens': chat['max_output_tokens']}
    if 'reasoning_effort' in chat:
        body['reasoning'] = {'effort': chat['reasoning_effort']}
    if 'response_format' in chat:
        body['text'] = {'format': chat['response_format']}
    return body


def chat_response(raw):
    if raw.lstrip().startswith(b'{'):
        return _chat(json.loads(raw))
    for line in reversed(raw.splitlines()):
        if not line.startswith(b'data:'):
            continue
        data = line[5:].strip()
        if not data or data == b'[DONE]':
            continue
        event = json.loads(data)
        if event.get('type') in ('response.completed', 'response.incomplete'):
            return _chat(event['response'])
        if event.get('type') in ('error', 'response.failed') or 'error' in event:
            raise CognitionUnavailable('provider_responses_error')
    raise CognitionUnavailable('provider_responses_incomplete')


def _chat(response):
    status = response.get('status')
    if status not in ('completed', 'incomplete'):
        raise CognitionUnavailable('provider_responses_error')
    content = []
    calls = []
    for item in response.get('output', []):
        if item.get('type') == 'message':
            content.extend(part['text'] for part in item.get('content', []) if part.get('type') == 'output_text')
        elif item.get('type') == 'function_call':
            calls.append(item)
    usage = response.get('usage') or {}
    incoming = usage.get('input_tokens_details') or {}
    outgoing = usage.get('output_tokens_details') or {}
    receipt = {field: usage[field] for field in ('actual_cost_usd', 'actual_cost', 'currency', 'cost') if field in usage}
    finish = 'stop' if status == 'completed' else 'length'
    return {
        'model': response.get('model'),
        'choices': [{'message': {'content': ''.join(content), 'tool_calls': calls}, 'finish_reason': finish}],
        'usage': {'prompt_tokens': usage.get('input_tokens'), 'completion_tokens': usage.get('output_tokens'),
                  'total_tokens': usage.get('total_tokens'),
                  'prompt_tokens_details': {'cached_tokens': incoming.get('cached_tokens')},
                  'completion_tokens_details': {'reasoning_tokens': outgoing.get('reasoning_tokens', 0)}, **receipt}
    }
