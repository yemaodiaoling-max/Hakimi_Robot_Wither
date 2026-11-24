"""Budgeted chat completions for QQ tools and background reviews."""
import json
import logging
import math
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from http.client import HTTPException, IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .gateway import CognitionUnavailable
from .live import BudgetPaused
from .openai_provider import OpenAICompatibleClient, receipt_usd, upstream_pool_unavailable
from .provider_probe import _NoRedirect, _endpoint
from .video_budget import video_budget


_LOG = logging.getLogger(__name__)
_STREAM_TAIL_SECONDS = 1.0
_USAGE_SNAPSHOT_SECONDS = 2.0
_MAX_STREAM_LINE_BYTES = 256 * 1024


# Reservation bounds per million tokens. The relay's measured USD charges on
# uncached requests included a 1.2 multiplier. Haiku reserves its long-input tier.
_RATES = {
    'claude-haiku-5-5': (Decimal('.60'), Decimal('3')),
    'claude-sonnet-5-5': (Decimal('2.40'), Decimal('12')),
}


def rates_for(model):
    try:
        return _RATES[model]
    except KeyError:
        raise CognitionUnavailable('text_model_unconfigured') from None


def text_profile(config):
    path = config.get('text_provider_profile')
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        if raw.get('activated') is not True or raw.get('usage_panel_actual_cost') is not True:
            raise ValueError()
        if not Path(raw['api_key_file']).is_absolute():
            raise ValueError()
        _endpoint(raw['base_url'], False)
        for role in ('fast', 'reasoning'):
            rates_for(raw['text_models'][role])
        # This profile holds the dedicated Claude text account. Older live
        # profiles named the Chat Completions transport or omitted the field.
        return dict(raw, wire_protocol='anthropic')
    except (OSError, ValueError, KeyError, TypeError):
        raise CognitionUnavailable('text_profile_invalid') from None


def load_config(watcher_path):
    return json.loads(Path(watcher_path).read_text(encoding='utf-8'))


def claude_sonnet_effort(profile, model):
    from .flash_duty import SONNET_MODELS
    return 'low' if model in SONNET_MODELS and profile.get('sonnet_reasoning_effort') == 'low' else None


def text_settings(config, role):
    profile = text_profile(config)
    if profile is None:
        raise CognitionUnavailable('text_provider_unconfigured')
    model = profile['text_models'][role]
    return dict(model=model, api_key_file=profile['api_key_file'],
                base_url=profile['base_url'], budget=config,
                reasoning_effort=claude_sonnet_effort(profile, model),
                wire_protocol=profile['wire_protocol'])


def load_settings(watcher_path, role):
    return text_settings(load_config(watcher_path), role)


def review_standby_settings(config):
    from .flash_duty import HAIKU_MODELS
    from .profile import ORIGINAL_RELAY_URL

    settings = text_settings(config, 'fast')
    if (settings['base_url'].rstrip('/') != ORIGINAL_RELAY_URL
            or settings['model'] not in HAIKU_MODELS):
        raise CognitionUnavailable('companion_review_standby_invalid')
    return settings


def _complete_original(config, profile, messages, **kwargs):
    from .flash_duty import HYDROGEN_MODELS

    model = profile.observer_model
    raw = profile.raw
    effort = 'low' if model in HYDROGEN_MODELS else raw.get('reasoning_effort')
    settings = dict(model=model, api_key_file=profile.api_key_file,
                    base_url=profile.base_url, budget=config,
                    rates=profile.prices(model, time.time()), reasoning_effort=effort,
                    output_limit_parameter=raw['output_limit_parameter'],
                    send_json_mode=raw.get('send_json_mode', True),
                    wire_protocol=raw.get('wire_protocol', 'openai'),
                    original_relay=True)
    if raw['media_mode'] == 'text_only':
        settings['input_token_upper_bound'] = raw['input_token_upper_bound']
        if type(kwargs['max_tokens']) is int:
            kwargs['max_tokens'] = min(kwargs['max_tokens'], raw['max_output_tokens'])
    return complete(settings, messages, **kwargs)


def complete_primary(config, model, messages, **kwargs):
    from .flash_duty import FLASH_LOW_MODEL, FLASH_MODEL, HAIKU_MODELS, HYDROGEN_MODELS, SONNET_MODELS
    from .profile import load_duty_profile, load_qq_flash_low_profile

    if model == FLASH_LOW_MODEL:
        return _complete_original(config, load_qq_flash_low_profile(config), messages, **kwargs)
    if model != FLASH_MODEL and model not in (SONNET_MODELS | HAIKU_MODELS | HYDROGEN_MODELS):
        raise CognitionUnavailable('companion_text_model_invalid')
    profile = load_duty_profile(config, qq_text=model in HYDROGEN_MODELS,
                                review=model in SONNET_MODELS, review_haiku=model in HAIKU_MODELS)
    if profile.observer_model != model:
        raise CognitionUnavailable('companion_text_model_mismatch')
    return _complete_original(config, profile, messages, **kwargs)


def complete_flash(config, messages, **kwargs):
    from .profile import load_flash_profile
    return _complete_original(config, load_flash_profile(config), messages, **kwargs)


def _wire_tokens(body):
    # Count the actual wire representation once, including signed tool history.
    # Base64 images have a per-image reservation, including nested tool results.
    images = 0

    def without_images(content):
        nonlocal images
        if not isinstance(content, list):
            return content
        blocks = []
        for block in content:
            if block.get('type') in ('image', 'image_url'):
                images += 1
                continue
            if block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                block = dict(block, content=without_images(block['content']))
            blocks.append(block)
        return blocks

    messages = [dict(message, content=without_images(message['content'])) if 'content' in message else message
                for message in body['messages']]
    size = len(json.dumps(dict(body, messages=messages), ensure_ascii=False).encode('utf-8'))
    return math.ceil(size * 1.1) + 2048 + images * 4096


@contextmanager
def _response_validation(strict):
    try:
        yield
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        if strict:
            raise CognitionUnavailable('flash_text_invalid_response') from None
        raise


def _sse_lines(response, deadline, tail_deadline):
    # read1 yields buffered bytes promptly, including providers whose SSE events
    # are split across HTTP chunks. Recompute the socket timeout for every read.
    stream_socket = response.fp.raw._sock
    pending = bytearray()
    while True:
        remaining = min(deadline, tail_deadline()) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('text_response_timeout')
        stream_socket.settimeout(remaining)
        data = response.read1(8192)
        if not data:
            if pending:
                yield bytes(pending)
            break
        pending.extend(data)
        while (end := pending.find(b'\n')) >= 0:
            if end > _MAX_STREAM_LINE_BYTES:
                raise CognitionUnavailable('provider_response_too_large')
            yield bytes(pending[:end])
            del pending[:end + 1]
        if len(pending) > _MAX_STREAM_LINE_BYTES:
            raise CognitionUnavailable('provider_response_too_large')


@contextmanager
def _stream_timeout(tail_deadline, awaiting):
    try:
        yield
    except (TimeoutError, IncompleteRead) as exc:
        if tail_deadline() == math.inf:
            if isinstance(exc, TimeoutError):
                raise TimeoutError('text_response_timeout') from None
            raise CognitionUnavailable('text_stream_incomplete') from None
        _LOG.info('Provider text stream ended while waiting for %s after its terminal result', awaiting())


def _stream_result(response, progress, deadline, *, strict=False, bounded_tail=False):
    content, calls = [], {}
    model = finish = refusal = None
    usage = {}
    tail = math.inf
    lines = _sse_lines(response, deadline, lambda: tail) if bounded_tail else iter(response)
    with _stream_timeout(lambda: tail, lambda: 'EOF' if usage else 'usage'):
        for raw in lines:
            if time.monotonic() >= deadline:
                raise TimeoutError('text_response_timeout')
            if not raw.startswith(b'data:'):
                continue
            line = raw[5:].strip()
            if line == b'[DONE]':
                break
            with _response_validation(strict):
                event = json.loads(line)
                if event.get('error'):
                    raise CognitionUnavailable('text_stream_error')
                if event.get('model'):
                    if model and model != event['model']:
                        raise CognitionUnavailable('text_model_mismatch')
                    model = event['model']
                frame_usage = event.get('usage')
                if frame_usage is not None:
                    if not isinstance(frame_usage, dict):
                        raise TypeError('text_stream_invalid_usage')
                    usage.update(frame_usage)
                choices = event.get('choices', [])
                if not isinstance(choices, list):
                    raise TypeError('text_stream_invalid_choices')
            for choice in choices:
                with _response_validation(strict):
                    if strict and (choice.get('index', 0) != 0 or finish is not None):
                        raise CognitionUnavailable('text_stream_invalid_choice')
                    delta = choice.get('delta', {})
                    if strict and (delta.get('refusal') or delta.get('function_call')):
                        refusal = True
                    if delta.get('content'):
                        content.append(delta['content'])
                    for call in delta.get('tool_calls', []):
                        item = calls.setdefault(call['index'], {'id': '', 'type': 'function',
                                                                'function': {'name': '', 'arguments': ''}})
                        if call.get('id'):
                            item['id'] = call['id']
                        for key in ('name', 'arguments'):
                            item['function'][key] += (call.get('function') or {}).get(key) or ''
                    active = delta.get('content') or delta.get('tool_calls') or delta.get('reasoning_content')
                    finish = choice.get('finish_reason') or finish
                if active and not (bounded_tail and finish) and progress and progress() is False:
                    raise CognitionUnavailable('turn_ownership_lost')
            if bounded_tail and finish:
                if isinstance(frame_usage, dict) and receipt_usd(frame_usage) is not None:
                    break
                if tail == math.inf:
                    tail = time.monotonic() + _STREAM_TAIL_SECONDS
    if not finish:
        raise CognitionUnavailable('text_stream_incomplete')
    with _response_validation(strict):
        message = {'role': 'assistant', 'content': ''.join(content) or None}
        if refusal:
            message['refusal'] = True
        if calls:
            message['tool_calls'] = [calls[i] for i in sorted(calls)]
    return {'model': model, 'usage': usage, 'choices': [{'finish_reason': finish, 'message': message}]}


def _anthropic_stream(response, progress, deadline, *, strict):
    message = None
    blocks = {}
    fragments = {}
    delta_fields = {'text_delta': 'text', 'thinking_delta': 'thinking',
                    'signature_delta': 'signature', 'input_json_delta': 'partial_json'}
    tail = math.inf
    with _stream_timeout(lambda: tail, lambda: 'message_stop'):
        for raw in _sse_lines(response, deadline, lambda: tail):
            if not raw.startswith(b'data:'):
                continue
            with _response_validation(strict):
                event = json.loads(raw[5:])
                kind = event['type']
                if kind == 'error':
                    raise CognitionUnavailable('text_stream_error')
                if kind == 'message_start':
                    if message is not None:
                        raise CognitionUnavailable('provider_response_invalid')
                    message = event['message']
                    message['usage'] = message.get('usage') or {}
                    blocks.update(enumerate(message.get('content') or []))
                elif kind == 'content_block_start':
                    blocks[event['index']] = event['content_block']
                elif kind == 'content_block_delta':
                    delta = event['delta']
                    index = event['index']
                    field = delta_fields.get(delta['type'])
                    if field is not None:
                        fragments.setdefault((index, field), []).append(delta[field])
                    elif delta['type'] == 'citations_delta':
                        blocks[index].setdefault('citations', []).append(delta['citation'])
                elif kind == 'message_delta' and message is not None:
                    message.update(event['delta'])
                    message['usage'].update(event.get('usage') or {})
                    if message.get('stop_reason') and tail == math.inf:
                        tail = time.monotonic() + _STREAM_TAIL_SECONDS
                    if tail != math.inf and receipt_usd(message['usage']) is not None:
                        break
                elif kind == 'message_stop':
                    break
            if kind == 'content_block_delta' and progress and progress() is False:
                raise CognitionUnavailable('turn_ownership_lost')
    if not message or not message.get('stop_reason'):
        raise CognitionUnavailable('text_stream_incomplete')
    with _response_validation(strict):
        for (index, field), chunks in fragments.items():
            block = blocks[index]
            joined = ''.join(chunks)
            if field == 'partial_json':
                if joined:
                    try:
                        block['input'] = json.loads(joined)
                    except ValueError:
                        raise CognitionUnavailable('text_stream_incomplete') from None
            else:
                block[field] = block.get(field, '') + joined
        message['content'] = [blocks[index] for index in sorted(blocks)]
        return _anthropic_response(message)


def _anthropic_response(message):
    from .anthropic_messages import chat_response

    value = chat_response(message, require_cache_write_price=False)
    output_config = message.get('output_config')
    if isinstance(output_config, dict) and output_config.get('effort') in ('low', 'medium', 'high', 'xhigh', 'max'):
        value['_reported_reasoning_effort'] = output_config['effort']
    if any(part.get('type') not in {'text', 'thinking', 'redacted_thinking', 'tool_use'}
           for part in message['content']):
        value['choices'][0]['message']['refusal'] = True
    return value


def complete(settings, messages, *, max_tokens, tools=None, json_mode=False, timeout=60,
             progress=None, deadline=None, budget_lane='other'):
    from .flash_duty import HAIKU_MODELS
    from .routing import reported_model_matches

    model = settings['model']
    original = settings.get('original_relay') is True or settings.get('flash') is True
    native = settings.get('wire_protocol') == 'anthropic'
    input_rate, output_rate = settings['rates'] if original else rates_for(model)
    stream_text = model in HAIKU_MODELS or (native and (not original or model == 'claude-opus-5-5'))
    streaming = stream_text or progress is not None
    output_limit = settings['output_limit_parameter'] if original else 'max_tokens'
    if not native or original:
        # Signed replay belongs to its originating account. Other providers
        # may still use the portable tool calls and results for the same turn.
        messages = [{k: v for k, v in msg.items() if k != '_anthropic_content'}
                    if '_anthropic_content' in msg else msg for msg in messages]
    body = {'model': model, 'messages': messages, output_limit: max_tokens, 'stream': streaming}
    if type(max_tokens) is not int or max_tokens < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('text_request_limits_invalid')
    started = time.monotonic()
    local_deadline = started + timeout
    deadline = local_deadline if deadline is None else min(deadline, local_deadline)
    if tools:
        body['tools'] = tools
    if json_mode and settings.get('send_json_mode', True):
        body['response_format'] = {'type': 'json_object'}
    if settings.get('reasoning_effort') is not None:
        body['reasoning_effort'] = settings['reasoning_effort']
    if streaming and not native:
        body['stream_options'] = {'include_usage': True}
    client = OpenAICompatibleClient(settings['base_url'], api_key_file=settings['api_key_file'],
                                    input_token_upper_bound=200_000, timeout=max(1, min(timeout, 180)))
    if native:
        from .anthropic_messages import request_body
        if original and (tools or (streaming and not stream_text)):
            raise CognitionUnavailable('provider_text_only_required')
        body = request_body(body)
        if streaming:
            body['stream'] = True
        url = client.base_url + '/messages'
        headers = {'Content-Type': 'application/json', 'x-api-key': client._key(),
                   'anthropic-version': '2023-06-01'}
    else:
        url = client.base_url + '/chat/completions'
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + client._key()}
    if native and streaming:
        headers['Accept'] = 'text/event-stream'
    size = _wire_tokens(body)
    if size > settings.get('input_token_upper_bound', float('inf')):
        raise CognitionUnavailable('provider_input_bound_exceeded')
    upper = (input_rate * size + output_rate * max_tokens) / 1_000_000
    request = Request(url, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                      headers=headers)
    ledger = video_budget(settings['budget'], lane=budget_lane)
    request_id = 'companion-text-' + model + '-' + uuid.uuid4().hex
    phase = 'before_headers'

    def stream_progress():
        nonlocal phase
        phase = 'after_content'
        return progress() if progress else None

    def report(status):
        if stream_text:
            _LOG.info('Text request model=%s protocol=%s status=%s phase=%s elapsed_ms=%d request_id=%s',
                      model, 'anthropic' if native else 'openai', status, phase,
                      (time.monotonic() - started) * 1000, request_id)

    if progress and progress() is False:
        raise CognitionUnavailable('turn_ownership_lost')

    def usage_snapshot():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if stream_text:
            return client.usage_snapshot(model, timeout=min(remaining, _USAGE_SNAPSHOT_SECONDS))
        return client.usage_snapshot(model)

    before = usage_snapshot()
    if progress and progress() is False:
        raise CognitionUnavailable('turn_ownership_lost')
    if time.monotonic() >= deadline:
        raise TimeoutError('text_response_timeout')
    if not ledger.reserve(request_id, upper, time.time()):
        raise BudgetPaused('model_budget_exhausted')
    try:
        if progress and progress() is False:
            ledger.settle(request_id, 0)
            raise CognitionUnavailable('turn_ownership_lost')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            ledger.settle(request_id, 0)
            raise TimeoutError('text_response_timeout')
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=remaining) as response:
            phase = 'before_content'
            events = streaming and response.headers.get_content_type() == 'text/event-stream'
            if events and native:
                value = _anthropic_stream(response, stream_progress, deadline, strict=original)
            elif native:
                with _response_validation(original):
                    raw = response.read(256 * 1024 + 1)
                    if len(raw) > 256 * 1024:
                        raise CognitionUnavailable('provider_response_too_large')
                    value = _anthropic_response(json.loads(raw))
            elif events:
                value = _stream_result(response, stream_progress, deadline, strict=original, bounded_tail=stream_text)
            else:
                with _response_validation(original):
                    value = json.load(response)
        phase = 'after_terminal'
        if native and not tools:
            message = value['choices'][0]['message']
            if message.get('tool_calls'):
                message['refusal'] = True
        if original and (not isinstance(value, dict) or
                      (value.get('usage') is not None and not isinstance(value['usage'], dict))):
            raise CognitionUnavailable('flash_text_invalid_response')
        normalized = None if original else client.normalize(value, require_usage=False)
        usage = value.get('usage') or {}
        cost = receipt_usd(usage)
        if cost is None and before is not None:
            # Usage panel attribution is keyed by the requested model.
            if not original or reported_model_matches(model, value.get('model')):
                if normalized is None:
                    with _response_validation(original):
                        normalized = client.normalize(value, require_usage=False)
                cost = client.panel_actual_cost(before, usage_snapshot(), normalized)
        if cost is None:
            ledger.ambiguous(request_id)
        else:
            ledger.settle(request_id, cost)
        if not reported_model_matches(model, value.get('model')):
            raise CognitionUnavailable('text_model_mismatch')
        if original and (value.get('error') or not isinstance(value.get('choices'), list)
                      or len(value['choices']) != 1):
            raise CognitionUnavailable('flash_text_invalid_response')
        if progress and progress() is False:
            raise CognitionUnavailable('turn_ownership_lost')
        value['request_id'] = request_id
        value['_billing_state'] = 'settled' if cost is not None else 'unknown'
        effort = body.get('output_config', {}).get('effort') if native else body.get('reasoning_effort')
        if effort is not None:
            value['_requested_reasoning_effort'] = effort
        report(value['_billing_state'])
        return value
    except HTTPError as exc:
        status = exc.code
        code = 'provider_http_' + str(status)
        with exc:
            if (original and status == 400) or status in (502, 503):
                try:
                    if status in (502, 503):
                        error_timeout = min(1.0, deadline - time.monotonic())
                        if error_timeout <= 0:
                            raise TimeoutError()
                        if hasattr(exc.fp, 'fp'):
                            exc.fp.fp.raw._sock.settimeout(error_timeout)
                    raw = exc.read(8192)
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = {}
                    error = body.get('error', body) if isinstance(body, dict) else {}
                    detail = error.get('message') if isinstance(error, dict) else None
                    provider_code = error.get('code') if isinstance(error, dict) else None
                    if upstream_pool_unavailable(status, provider_code, detail or raw.decode('utf-8','replace')):
                        code = 'upstream_pool_unavailable'
                    elif status == 400 and isinstance(detail,str) and detail.strip().casefold() == 'unknown provider for model ' + model.casefold():
                        code = 'provider_model_unrouted'
                except (ValueError, TypeError, AttributeError, OSError, HTTPException):
                    pass
        ledger.ambiguous(request_id)
        failure = CognitionUnavailable(code)
        failure.http_status = status
        failure.request_id = request_id
        report(code)
        raise failure from None
    except URLError as exc:
        ledger.ambiguous(request_id)
        if isinstance(exc.reason, TimeoutError):
            report('text_response_timeout')
            timeout_error = TimeoutError('text_response_timeout')
            timeout_error.request_id = request_id
            raise timeout_error from None
        report('transport_error')
        exc.request_id = request_id
        raise
    except Exception as exc:
        ledger.ambiguous(request_id)
        exc.request_id = request_id
        if isinstance(exc, TimeoutError):
            report('text_response_timeout')
        elif isinstance(exc, CognitionUnavailable):
            code = str(exc)
            report(code if code in {'text_stream_incomplete', 'text_stream_error',
                                    'turn_ownership_lost', 'text_model_mismatch'} else 'response_error')
        else:
            report(type(exc).__name__)
        raise
