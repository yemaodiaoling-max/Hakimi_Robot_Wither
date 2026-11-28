"""One background review slot: main gateway, independent HTTP API, or companion duty."""
import json
import re
import threading
from http.client import HTTPException

from hakimi.curation import review_sol_transport
from hakimi.models import aux_config, gateway_text, video_hydrogen

MAX_OUTPUT_TOKENS = 8192
HAIKU_REVIEW_TIMEOUT = 120
SYSTEM_PROMPT = '依据给定事件和原文证据完成定性复盘，仅返回任务指定的JSON对象。'
_slot = threading.Lock()
_historical_slot = threading.Lock()


class ReviewAPIError(RuntimeError):
    """Only fixed, non-sensitive error codes escape the transport."""


class ReviewResult(dict):
    def __init__(self, value, metadata):
        super().__init__(value)
        self.review_metadata = metadata


def reviewer_metadata(result):
    # Model JSON cannot nominate its own provider or overwrite audit provenance.
    return dict(result.review_metadata) if isinstance(result, ReviewResult) else {}


def _invalid_json_constant(_):
    raise ValueError('REVIEW_INVALID_JSON')


def parse_json(text, *, strict=False):
    text = str(text).strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    if len(text) > 32000:
        raise ValueError('REVIEW_OUTPUT_TOO_LARGE')
    value = json.loads(text, parse_constant=_invalid_json_constant if strict else None)
    if not isinstance(value, dict):
        raise ValueError('REVIEW_INVALID_JSON')
    return value


def is_enabled(base) -> bool:
    if aux_config.has_config(base):
        cfg = aux_config.review_config(base)
        return bool(cfg.get('enabled', True) and cfg.get('mode') != 'disabled')
    return True


def _map_gateway_error(exc: gateway_text.GatewayTextError) -> ReviewAPIError:
    mapping = {
        'TEXT_API_AUTH': 'REVIEW_API_AUTH',
        'TEXT_API_QUOTA': 'REVIEW_API_QUOTA',
        'TEXT_API_CONFIG': 'REVIEW_API_CONFIG',
        'TEXT_GATEWAY_DISABLED': 'REVIEW_API_CONFIG',
        'TEXT_API_INCOMPLETE': 'REVIEW_API_INCOMPLETE',
        'TEXT_API_INVALID_RESPONSE': 'REVIEW_API_INVALID_RESPONSE',
        'TEXT_API_RESPONSE_TOO_LARGE': 'REVIEW_API_RESPONSE_TOO_LARGE',
        'TEXT_API_HTTP': 'REVIEW_API_HTTP',
        'TEXT_API_UNAVAILABLE': 'REVIEW_API_UNAVAILABLE',
    }
    return ReviewAPIError(mapping.get(exc.code, 'REVIEW_API_UNAVAILABLE'))


def _classify_main_gateway(text, base, cfg):
    tier = str(cfg.get('model') or '').strip() or 'gemini-4-hydrogen'
    try:
        res = gateway_text.complete_via_gateway(
            base, text, system=SYSTEM_PROMPT, tier=tier,
            json_mode=True, max_tokens=MAX_OUTPUT_TOKENS, timeout=60,
        )
    except gateway_text.GatewayTextError as exc:
        raise _map_gateway_error(exc) from None
    parsed = parse_json(res['text'])
    metadata = {
        'reviewer': 'local_gateway',
        'review_model': res['tier'],
        'review_reported_model': res['model'],
        'review_usage': res['usage'],
    }
    return ReviewResult(parsed, metadata)


def _classify_custom_api(text, cfg):
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': text},
    ]
    try:
        res = gateway_text.complete_via_http_api(
            cfg, messages, json_mode=True, max_tokens=MAX_OUTPUT_TOKENS, timeout=60,
        )
    except gateway_text.GatewayTextError as exc:
        raise _map_gateway_error(exc) from None
    parsed = parse_json(res['text'], strict=True)
    metadata = {
        'reviewer': 'custom_review_api',
        'review_model': cfg['model'],
        'review_reported_model': res['model'],
        'review_usage': res['usage'],
    }
    return ReviewResult(parsed, metadata)


def _classify_companion_duty(text, base, config_path=None):
    try:
        client, config = video_hydrogen.review_backend(base, config_path=config_path)
    except Exception:
        raise ReviewAPIError('REVIEW_API_CONFIG') from None
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': text},
    ]
    dedicated_review = bool(config.get('qq_flash_low_profile') or config.get('qq_review_sonnet_profile'))

    def limits(model, recover=False):
        return dict(max_tokens=2048 if recover else MAX_OUTPUT_TOKENS, json_mode=True,
                    timeout=HAIKU_REVIEW_TIMEOUT if model.startswith('claude-haiku-') else 60,
                    budget_lane='review')

    def original(model, recover):
        value = client.complete_primary(config, model, messages, **limits(model, recover))
        try:
            return _review_result(value, model, strict=True,
                                  account='original' if dedicated_review else None)
        except (ReviewAPIError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            raise client.CognitionUnavailable('companion_review_invalid_response') from None

    def standby(_):
        try:
            settings = (client.review_standby_settings(config) if dedicated_review
                        else client.text_settings(config, 'fast'))
        except client.CognitionUnavailable:
            raise ReviewAPIError('REVIEW_API_CONFIG') from None
        value = client.complete(settings, messages, **limits(settings['model']))
        return _review_result(value, settings['model'], strict=dedicated_review,
                              account='standby' if dedicated_review else None)

    try:
        if config.get('qq_flash_low_profile') and (
                config.get('qq_review_sol_profile') or config.get('video_selection_profile')):
            from bili_companion.cognition.flash_duty import FLASH_LOW_MODEL

            try:
                return original(FLASH_LOW_MODEL, False)
            except (client.CognitionUnavailable, OSError, HTTPException):
                pass
            return _classify_original_sol(messages, config)
        if dedicated_review or config.get('flash_duty_profile'):
            from bili_companion.cognition.flash_duty import run_text_duty
            return run_text_duty(config, 'review', original, standby)
        return standby(None)
    except client.BudgetPaused:
        raise ReviewAPIError('REVIEW_API_QUOTA') from None
    except (ReviewAPIError, ValueError, KeyError, TypeError, IndexError):
        raise
    except Exception:
        raise ReviewAPIError('REVIEW_API_UNAVAILABLE') from None


def _classify_original_sol(messages, config):
    try:
        value = review_sol_transport.complete(config, messages, max_tokens=MAX_OUTPUT_TOKENS, timeout=90)
    except ValueError as exc:
        error = ReviewAPIError(str(exc))
        code = error_code(error)
        if code == 'REVIEW_FAILED':
            code = 'REVIEW_API_INVALID_RESPONSE'
        raise ReviewAPIError(code) from None
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ReviewAPIError('REVIEW_API_INVALID_RESPONSE') from None
    except Exception:
        raise ReviewAPIError('REVIEW_API_UNAVAILABLE') from None
    return _review_result(value, review_sol_transport.MODEL, strict=True, account='original')


def _review_prompt(task):
    from hakimi.persona.prompt_language import prompt

    text = prompt(task)
    if len(text.encode('utf-8')) > 64000:
        raise ReviewAPIError('REVIEW_API_INPUT_TOO_LARGE')
    return text


def _sol_backend(base):
    config_path = None
    if aux_config.has_config(base):
        cfg = aux_config.review_config(base)
        if not cfg.get('enabled', True) or cfg.get('mode') == 'disabled':
            raise ReviewAPIError('REVIEW_DISABLED')
        if cfg['mode'] != 'companion_duty':
            raise ReviewAPIError('REVIEW_API_CONFIG')
        config_path = cfg.get('config_path') or None
    try:
        _, config = video_hydrogen.review_backend(base, config_path=config_path)
    except Exception:
        raise ReviewAPIError('REVIEW_API_CONFIG') from None
    return config


def sol_backfill_enabled(base):
    try:
        review_sol_transport.verified_profile(_sol_backend(base))
    except Exception:
        return False
    return True


def classify_sol(task, base):
    """Review historical work once on the same original-account Sol route."""
    with _historical_slot:
        text = _review_prompt(task)
        return _classify_original_sol([
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': text},
        ], _sol_backend(base))


def classify(task, base):
    with _slot:
        text = _review_prompt(task)
        if aux_config.has_config(base):
            cfg = aux_config.review_config(base)
            if not cfg.get('enabled', True) or cfg.get('mode') == 'disabled':
                raise ReviewAPIError('REVIEW_DISABLED')
            mode = cfg['mode']
            if mode == 'main_gateway':
                return _classify_main_gateway(text, base, cfg)
            if mode == 'custom_api':
                return _classify_custom_api(text, cfg)
            return _classify_companion_duty(text, base, config_path=cfg.get('config_path') or None)
        # Legacy installs without aux_config.json: try companion duty first, else main gateway if active.
        try:
            video_hydrogen.review_backend(base)
        except Exception:
            from hakimi.models import vertex_route
            if vertex_route.load(base).enabled:
                return _classify_main_gateway(text, base, aux_config.review_config(base))
            raise ReviewAPIError('REVIEW_API_CONFIG') from None
        return _classify_companion_duty(text, base)


def _review_result(value, model, *, strict=False, account=None):
    choices = value.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
        raise ReviewAPIError('REVIEW_API_INCOMPLETE')
    message = choices[0].get('message', {})
    if message.get('tool_calls') or message.get('function_call') or (strict and message.get('refusal')):
        raise ReviewAPIError('REVIEW_API_INVALID_RESPONSE')
    content = message.get('content')
    if isinstance(content, list):
        content = ''.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    if not isinstance(content, str):
        raise ReviewAPIError('REVIEW_API_INVALID_RESPONSE')
    usage = value.get('usage')
    recorded_haiku = (model.startswith('claude-haiku-') and value.get('_billing_state') in ('settled', 'unknown')
                      and isinstance(value.get('request_id'), str)
                      and re.fullmatch('companion-text-' + re.escape(model) + r'-[0-9a-f]{32}', value['request_id']) is not None)
    if usage is None and recorded_haiku:
        usage = {}
    if not isinstance(usage, dict):
        raise ReviewAPIError('REVIEW_API_INVALID_USAGE')
    token_fields = ('prompt_tokens', 'completion_tokens')
    safe_usage = {k: usage[k] for k in token_fields if usage.get(k) is not None}
    usage_complete = len(safe_usage) == len(token_fields)
    if (any(type(v) is not int or v < 0 for v in safe_usage.values())
            or (not usage_complete and not recorded_haiku)):
        raise ReviewAPIError('REVIEW_API_INVALID_USAGE')
    details = usage.get('completion_tokens_details')
    reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
    if type(reasoning) is int and 0 <= reasoning <= safe_usage.get('completion_tokens', -1):
        safe_usage['reasoning_tokens'] = reasoning
    metadata = {'reviewer': 'video_understanding_api', 'review_model': model,
                'review_reported_model': value['model'], 'review_usage': safe_usage}
    if recorded_haiku or model == review_sol_transport.MODEL:
        metadata['review_billing_state'] = value['_billing_state']
        if not usage_complete:
            metadata['review_usage_status'] = 'incomplete'
    if account:
        metadata['review_account'] = account
        if value.get('request_id'):
            metadata['review_request_id'] = value['request_id']
    if '_requested_reasoning_effort' in value:
        metadata['review_requested_reasoning_effort'] = value['_requested_reasoning_effort']
    return ReviewResult(parse_json(content, strict=strict), metadata)


def error_code(exc):
    # Never persist arbitrary provider error strings or prompt excerpts.
    value = str(exc)
    if isinstance(exc, ReviewAPIError):
        return value if value in {
            'REVIEW_DISABLED',
            'REVIEW_BUDGET_PAUSED',
            'REVIEW_API_CONFIG', 'REVIEW_API_REDIRECT', 'REVIEW_API_INPUT_TOO_LARGE',
            'REVIEW_API_RESPONSE_TOO_LARGE', 'REVIEW_API_INCOMPLETE', 'REVIEW_API_INVALID_RESPONSE',
            'REVIEW_API_INVALID_USAGE', 'REVIEW_API_AUTH', 'REVIEW_API_QUOTA',
            'REVIEW_API_HTTP', 'REVIEW_API_UNAVAILABLE'} else 'REVIEW_FAILED'
    validation_codes = {
        'invalid_relationship_events': 'REVIEW_INVALID_RELATIONSHIP_EVENTS',
        'invalid_relationship_source': 'REVIEW_INVALID_RELATIONSHIP_SOURCE',
        'invalid_relationship_tendency': 'REVIEW_INVALID_RELATIONSHIP_TENDENCY',
        'duplicate_window_person': 'REVIEW_DUPLICATE_WINDOW_PERSON',
    }
    if isinstance(exc, ValueError) and value in validation_codes:
        return validation_codes[value]
    for code in ('TEXT_JUDGE_WORKER_DISCONNECTED', 'TEXT_JUDGE_WORKER_NOT_READY',
                 'TEXT_JUDGE_WORKER_FAILED', 'TEXT_JUDGE_AGENT_FAILED',
                 'REVIEW_OUTPUT_TOO_LARGE', 'REVIEW_INVALID_JSON'):
        if value.startswith(code):
            return code
    return 'REVIEW_INVALID_RESULT' if isinstance(exc, (ValueError, KeyError, TypeError, StopIteration)) else 'REVIEW_FAILED'
