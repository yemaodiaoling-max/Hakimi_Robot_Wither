"""Translate Chat-compatible messages and tool turns to Anthropic Messages."""
from copy import deepcopy
import json

from .gateway import CognitionUnavailable


_IMAGE_TYPES = frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp'})


def _text_image_blocks(content):
    if content is None or content == '':
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    blocks = []
    for item in content:
        if item['type'] == 'text':
            blocks.append({'type': 'text', 'text': item['text']})
        elif item['type'] == 'image_url':
            prefix, separator, encoded = item['image_url']['url'].partition(',')
            mime = prefix.removeprefix('data:').removesuffix(';base64')
            if not separator or not encoded or mime not in _IMAGE_TYPES or prefix != f'data:{mime};base64':
                raise CognitionUnavailable('provider_media_unsupported')
            blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': encoded}})
        else:
            raise CognitionUnavailable('provider_media_unsupported')
    return blocks


def _assistant_blocks(message):
    calls = message.get('tool_calls') or []
    replay = message.get('_anthropic_content')
    if replay is not None and calls:
        # Replay the exact provider blocks, including signed or redacted thinking.
        native_ids = [block.get('id') for block in replay if block.get('type') == 'tool_use']
        if native_ids != [call['id'] for call in calls]:
            raise CognitionUnavailable('provider_tool_history_invalid')
        return deepcopy(replay)
    blocks = _text_image_blocks(message.get('content'))
    for call in calls:
        function = call['function']
        try:
            arguments = json.loads(function['arguments'])
        except (TypeError, ValueError):
            raise CognitionUnavailable('provider_tool_arguments_invalid') from None
        if call.get('type') != 'function' or not isinstance(arguments, dict):
            raise CognitionUnavailable('provider_tool_arguments_invalid')
        blocks.append({'type': 'tool_use', 'id': call['id'], 'name': function['name'], 'input': arguments})
    return blocks


def _append_message(messages, role, content):
    if not content:
        return
    if messages and messages[-1]['role'] == role:
        previous = messages[-1]['content']
        if content[0]['type'] == 'tool_result':
            end = next((i for i, block in enumerate(previous) if block['type'] != 'tool_result'), len(previous))
            previous[end:end] = content
        else:
            previous.extend(content)
    else:
        messages.append({'role': role, 'content': content})


def request_body(chat):
    body = {'model': chat['model'], 'max_tokens': chat.get('max_tokens') or chat['max_completion_tokens'],
            'messages': []}
    system = []
    for message in chat['messages']:
        role = message['role']
        if role in {'system', 'developer'}:
            system.append(message['content'])
        elif role == 'assistant':
            _append_message(body['messages'], role, _assistant_blocks(message))
        elif role == 'tool':
            result = {'type': 'tool_result', 'tool_use_id': message['tool_call_id']}
            content = message.get('content')
            if content is not None:
                result['content'] = content if isinstance(content, str) else _text_image_blocks(content)
            if message.get('is_error') is True:
                result['is_error'] = True
            _append_message(body['messages'], 'user', [result])
        elif role == 'user':
            _append_message(body['messages'], role, _text_image_blocks(message.get('content')))
        else:
            raise CognitionUnavailable('provider_message_role_unsupported')
    if system:
        body['system'] = '\n'.join(system)
    if chat.get('tools'):
        tools = []
        for tool in chat['tools']:
            if tool.get('type') != 'function':
                raise CognitionUnavailable('provider_tool_unsupported')
            function = tool['function']
            spec = {'name': function['name'], 'input_schema': function.get('parameters', {'type': 'object'})}
            if function.get('description'):
                spec['description'] = function['description']
            tools.append(spec)
        body['tools'] = tools
    if chat.get('reasoning_effort') is not None:
        body['output_config'] = {'effort': chat['reasoning_effort']}
    return body


def chat_response(message, *, require_cache_write_price=True):
    if message.get('type')!='message' or not isinstance(message.get('content'),list):
        raise CognitionUnavailable('provider_response_invalid')
    usage=message.get('usage') or {}
    counts=[usage.get('input_tokens'),usage.get('cache_read_input_tokens',0),
            usage.get('cache_creation_input_tokens',0),usage.get('output_tokens')]
    billed={}
    if all(type(value) is int and value>=0 for value in counts):
        ordinary,read,written,out=counts
        billed={'prompt_tokens':ordinary+read+written,'completion_tokens':out,
                'prompt_tokens_details':{'cached_tokens':read}}
        if written and require_cache_write_price:
            # The client requests no cache creation. If a relay adds it, a token-only settlement
            # would not know its write tariff; keep the liability unless the relay provides the bill.
            if usage.get('currency')!='USD' or 'cost' not in usage:
                raise CognitionUnavailable('provider_cache_write_unpriced')
    for key in ('cost', 'currency', 'actual_cost_usd', 'actual_cost'):
        if key in usage:
            billed[key] = usage[key]
    parts = message['content']
    text = ''.join(part['text'] for part in parts if part.get('type') == 'text')
    assistant = {'role': 'assistant', 'content': text}
    calls = []
    for part in parts:
        if part.get('type') != 'tool_use':
            continue
        if not isinstance(part.get('input'), dict) or not part.get('id') or not part.get('name'):
            raise CognitionUnavailable('provider_tool_response_invalid')
        calls.append({'type': 'function', 'id': part['id'], 'function': {
            'name': part['name'], 'arguments': json.dumps(part['input'], ensure_ascii=False, allow_nan=False)}})
    if calls:
        assistant['tool_calls'] = calls
        assistant['_anthropic_content'] = deepcopy(parts)
    stop_reason = message.get('stop_reason')
    finish = {'end_turn': 'stop', 'stop_sequence': 'stop', 'max_tokens': 'length',
              'model_context_window_exceeded': 'length', 'tool_use': 'tool_calls',
              'refusal': 'content_filter'}.get(stop_reason, 'other')
    if stop_reason == 'refusal':
        assistant['refusal'] = True
    return {'model': message.get('model'), 'usage': billed,
            'choices': [{'message': assistant, 'finish_reason': finish}]}
