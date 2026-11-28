"""Paid Responses API transport for the original review account."""
import json
import time
import uuid
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


MODEL = 'gpt-6.2-sol'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _terminal_response(raw):
    if raw.lstrip().startswith(b'{'):
        return json.loads(raw)
    for line in reversed(raw.splitlines()):
        if not line.startswith(b'data:'):
            continue
        data = line[5:].strip()
        if not data or data == b'[DONE]':
            continue
        event = json.loads(data)
        kind = event.get('type')
        if kind in ('response.completed', 'response.incomplete', 'response.failed'):
            return event['response']
        if kind == 'error':
            break
    raise ValueError('REVIEW_API_INCOMPLETE')


def _accepted_output(response):
    if response.get('status') != 'completed':
        raise ValueError('REVIEW_API_INCOMPLETE')
    messages = [item for item in response['output'] if item.get('type') == 'message']
    if (len(messages) != 1 or messages[0].get('role') != 'assistant'
            or messages[0].get('status', 'completed') != 'completed'
            or any(item.get('type') not in ('message', 'reasoning') for item in response['output'])):
        raise ValueError('REVIEW_API_INVALID_RESPONSE')
    parts = messages[0]['content']
    if not parts or any(part.get('type') != 'output_text' or not isinstance(part.get('text'), str) for part in parts):
        raise ValueError('REVIEW_API_INVALID_RESPONSE')


def verified_profile(config):
    from bili_companion.cognition.profile import load_profile, load_qq_flash_low_profile
    from bili_companion.cognition.routing import _same_relay_account

    primary = load_qq_flash_low_profile(config)
    profile = load_profile(config.get('qq_review_sol_profile') or config['video_selection_profile'],
                           text_only=True)
    if (profile.observer_model != MODEL or profile.raw.get('wire_protocol') != 'responses'
            or not profile.raw.get('single_pass') or not _same_relay_account(
                primary.base_url, primary.api_key_file, profile.base_url, profile.api_key_file)):
        raise ValueError('REVIEW_API_CONFIG')
    return profile


def complete(config, messages, *, max_tokens, timeout):
    from bili_companion.cognition.openai_provider import (
        OpenAICompatibleClient, receipt_usd, reported_model_matches,
    )
    from bili_companion.cognition.openai_responses import _chat, request_body
    from bili_companion.cognition.provider_probe import _NoRedirect
    from bili_companion.cognition.video_budget import video_budget

    try:
        profile = verified_profile(config)
        client = OpenAICompatibleClient(
            profile.base_url, api_key_file=profile.api_key_file, media_mode='text_only',
            input_token_upper_bound=profile.raw['input_token_upper_bound'],
            output_limit_parameter='max_output_tokens', wire_protocol='responses', timeout=timeout,
        )
        key = client._key()
        incoming, outgoing = profile.prices(MODEL, time.time())
        ledger = video_budget(config, lane='review')
    except Exception:
        raise ValueError('REVIEW_API_CONFIG') from None

    chat = dict(model=MODEL, messages=messages, max_output_tokens=max_tokens,
                reasoning_effort='low', response_format={'type': 'json_object'})
    body = json.dumps(request_body(chat), ensure_ascii=False, allow_nan=False).encode()
    input_upper = (len(body) * 11 + 9) // 10 + 8192
    if input_upper > profile.raw['input_token_upper_bound']:
        raise ValueError('REVIEW_API_INPUT_TOO_LARGE')
    reservation = (incoming * input_upper + outgoing * max_tokens) / 1_000_000
    url = client.base_url + '/responses'
    request = Request(url, data=body, headers={
        'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'text/event-stream',
    })
    request_id = 'companion-text-' + MODEL + '-' + uuid.uuid4().hex
    before = client.usage_snapshot(MODEL, timeout=2)
    if not ledger.reserve(request_id, reservation, time.time()):
        raise ValueError('REVIEW_BUDGET_PAUSED')
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise ValueError('REVIEW_API_REDIRECT')
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('REVIEW_API_RESPONSE_TOO_LARGE')
        terminal = _terminal_response(raw)
        native_usage = terminal.get('usage')
        actual = receipt_usd(native_usage) if isinstance(native_usage, dict) else None
        if actual is not None:
            ledger.settle(request_id, actual)
        value = _chat(terminal)
        if actual is None and before is not None and reported_model_matches(MODEL, value.get('model')):
            try:
                normalized = client.normalize(value, require_usage=True)
            except (TypeError, ValueError, KeyError, AttributeError):
                pass
            else:
                actual = client.panel_actual_cost(before, client.usage_snapshot(MODEL, timeout=2), normalized)
            if actual is not None:
                ledger.settle(request_id, actual)
        if actual is None:
            ledger.ambiguous(request_id)
        if not reported_model_matches(MODEL, value.get('model')):
            raise ValueError('REVIEW_API_INVALID_RESPONSE')
        _accepted_output(terminal)
    except HTTPError as exc:
        ledger.ambiguous(request_id)
        with exc:
            if exc.code in (401, 403):
                raise ValueError('REVIEW_API_AUTH') from None
            if exc.code in (402, 429):
                raise ValueError('REVIEW_API_QUOTA') from None
            if 300 <= exc.code < 400:
                raise ValueError('REVIEW_API_REDIRECT') from None
            raise ValueError('REVIEW_API_HTTP') from None
    except Exception:
        ledger.ambiguous(request_id)
        raise
    return dict(value, request_id=request_id, _billing_state='unknown' if actual is None else 'settled',
                _requested_reasoning_effort='low')
