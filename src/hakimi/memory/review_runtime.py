"""One background review slot through the video companion."""
import json
import re
import threading

from hakimi.models import video_hydrogen

MAX_OUTPUT_TOKENS = 8192
HAIKU_REVIEW_TIMEOUT = 120
_slot = threading.Lock()


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


def classify(task, base):
    from hakimi.prompt.prompt_language import prompt
    # Preserve the independent single review slot and the existing task/schema.
    with _slot:
        try:
            client, config = video_hydrogen.review_backend(base)
        except Exception:
            raise ReviewAPIError('REVIEW_API_CONFIG') from None
        text = prompt(task)
        if len(text.encode('utf-8')) > 64000:
            raise ReviewAPIError('REVIEW_API_INPUT_TOO_LARGE')
        messages = [
            {'role': 'system', 'content': '依据给定事件和原文证据完成定性复盘，仅返回任务指定的JSON对象。'},
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
    if recorded_haiku:
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
