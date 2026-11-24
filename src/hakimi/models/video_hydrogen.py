"""Serve QQ's native tool loop through an independent backup API or companion duty."""
import json
import sqlite3
import ssl
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from hakimi.models import aux_config
from hakimi.models.argon_session import NoAvailableAccounts, chat_request, chat_tools, messages
from hakimi.models.vertex_route import root_for


PROVIDER = 'video_claude'
FLASH_PROVIDER = 'video_flash'
HYDROGEN_PROVIDER = 'video_hydrogen'
CUSTOM_BACKUP_PROVIDER = 'qq_backup_api'
DUTY_PROVIDERS = frozenset({PROVIDER, FLASH_PROVIDER, HYDROGEN_PROVIDER, CUSTOM_BACKUP_PROVIDER})


def _companion(root, config_path=None):
    companion = root_for(root).parent / 'bili_companion'
    source = str(companion / 'src')
    if source not in sys.path:
        sys.path.insert(0, source)
    from bili_companion.cognition import companion_text
    cfg_file = Path(config_path) if config_path else (companion / 'deploy' / 'watcher.json')
    return companion_text, cfg_file


def backend(root, role, config_path=None):
    client, watcher = _companion(root, config_path=config_path)
    return client, client.load_settings(watcher, role)


def review_backend(root, config_path=None):
    client, watcher = _companion(root, config_path=config_path)
    return client, client.load_config(watcher)


def config(root):
    if aux_config.has_config(root):
        cfg = aux_config.qq_backup_config(root)
        if not cfg.get('enabled') or cfg.get('mode') == 'disabled':
            return None
        if cfg['mode'] == 'custom_api':
            if (not str(cfg.get('base_url') or '').strip()
                    or not str(cfg.get('model') or '').strip()
                    or not aux_config.resolve_api_key(cfg)):
                return None
            return {'mode': 'custom_api', 'model': str(cfg['model']).strip(), 'config': cfg, 'budget': {}}
        config_path = cfg.get('config_path') or None
    else:
        config_path = None
    try:
        client, budget = review_backend(root, config_path=config_path)
        if budget.get('qq_flash_low_profile'):
            from bili_companion.cognition.flash_duty import active_text_models
            return {'model': active_text_models(budget)['qq'], 'budget': budget, '_config_path': config_path}
        result = client.text_settings(budget, 'reasoning')
        if config_path and isinstance(result, dict):
            result['_config_path'] = config_path
        return result
    except Exception:
        return None


def duty_identity(settings):
    """Read the default QQ responder for status displays."""
    if settings.get('mode') == 'custom_api':
        return CUSTOM_BACKUP_PROVIDER, settings['model']
    config = settings['budget']
    if config.get('qq_flash_low_profile') or config.get('flash_duty_profile') or config.get('qq_hydrogen_profile'):
        from bili_companion.cognition.flash_duty import HYDROGEN_MODELS, active_text_models

        try:
            active = active_text_models(config).get('qq')
            if active:
                return (HYDROGEN_PROVIDER if active in HYDROGEN_MODELS else FLASH_PROVIDER), active
        except (OSError, sqlite3.Error, KeyError):
            pass
    return PROVIDER, settings['model']


def _invalid_tool_constant(_):
    raise ValueError('video_flash_invalid_tool_arguments')


class _AnthropicTurn(dict):
    """Keep signed blocks in Python-only attributes of this tool turn."""

    def __init__(self, content, route, native):
        super().__init__(content)
        self.anthropic_route = route
        self.anthropic_content = native


def _result(value, name_map, provider, prior_ids, *, anthropic_route=None):
    choices = value['choices']
    strict = anthropic_route is not None or provider in (FLASH_PROVIDER, HYDROGEN_PROVIDER)
    if strict and len(choices) != 1:
        raise ValueError('video_flash_invalid_choices')
    choice = choices[0]
    if choice['finish_reason'] not in {'stop', 'tool_calls'}:
        raise ValueError('video_claude_incomplete')
    message = choice['message']
    if strict and (message.get('refusal') or message.get('function_call')):
        raise ValueError('video_flash_invalid_response')
    calls = message.get('tool_calls') or []
    if strict and (bool(calls) != (choice['finish_reason'] == 'tool_calls') or type(calls) is not list):
        raise ValueError('video_flash_invalid_tool_finish')
    content = message.get('content')
    if isinstance(content, list):
        content = ''.join(p.get('text', '') for p in content if p.get('type') == 'text')
    parts = [{'text': content}] if isinstance(content, str) and content.strip() else []
    ids = set()
    for call in calls:
        if strict and (call.get('type') != 'function' or call['id'] in prior_ids):
            raise ValueError('video_flash_invalid_tool_call')
        ident = call['id']
        function = call['function']
        args = (json.loads(function['arguments'], parse_constant=_invalid_tool_constant) if strict
                else json.loads(function['arguments'] or '{}'))
        if not isinstance(ident, str) or not ident or ident in ids or not isinstance(args, dict):
            raise ValueError('video_claude_invalid_tool_call')
        ids.add(ident)
        parts.append({'functionCall': {'id': ident, 'name': name_map[function['name']], 'args': args}})
    if not parts or (choice['finish_reason'] == 'tool_calls' and not ids):
        raise ValueError('video_claude_empty_response')
    usage = value.get('usage') or {}
    reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
    counts = {'promptTokenCount': usage.get('prompt_tokens', 0),
              'candidatesTokenCount': max(0, usage.get('completion_tokens', 0) - reasoning),
              'thoughtsTokenCount': reasoning,
              'totalTokenCount': usage.get('total_tokens', 0),
              'cachedContentTokenCount': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)}
    turn = {'role': 'model', 'parts': parts}
    if anthropic_route is not None and calls:
        native = message.get('_anthropic_content')
        if not isinstance(native, list) or not native:
            raise ValueError('video_claude_tool_replay_missing')
        turn = _AnthropicTurn(turn, anthropic_route, native)
    return {'modelVersion': value['model'], '_provider': provider,
            **({'_provider_reasoning_effort': value['_requested_reasoning_effort']}
               if '_requested_reasoning_effort' in value else {}),
            '_provider_request_id': value.get('request_id'),
            'candidates': [{'content': turn, 'finishReason': 'STOP'}],
            'usageMetadata': counts}


def _gemini_custom_stream(session, payload, timeout, cfg, api_key, model):
    import copy
    from hakimi.models.vertex_session import _NoRedirect
    base_url = str(cfg.get('base_url') or '').strip().rstrip('/')
    if not base_url.startswith('https://') and not base_url.startswith(('http://127.0.0.1', 'http://localhost')):
        raise RuntimeError('qq_backup_endpoint_invalid')
    url = f"{base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse"
    headers = {'Content-Type': 'application/json'}
    if cfg.get('auth') == 'bearer':
        headers['Authorization'] = 'Bearer ' + api_key
    else:
        headers['x-goog-api-key'] = api_key
    req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    with opener.open(req, timeout=max(1.0, float(timeout))) as response:
        result = {'candidates': [{'content': {'role': 'model', 'parts': []}}],
                  '_provider': CUSTOM_BACKUP_PROVIDER, '_provider_request_id': request_id}
        candidate = result['candidates'][0]
        data = []
        def flush_event():
            if not data:
                return
            event = json.loads('\n'.join(data))
            data.clear()
            if event.get('modelVersion'):
                result['modelVersion'] = event['modelVersion']
            for item in (event.get('candidates') or [])[:1]:
                parts = (item.get('content') or {}).get('parts') or []
                candidate['content']['parts'].extend(copy.deepcopy(parts))
                if 'finishReason' in item:
                    candidate['finishReason'] = item['finishReason']
                if any(p.get('text') or p.get('functionCall') for p in parts):
                    if session._progress:
                        session._progress()
            for key, count in (event.get('usageMetadata') or {}).items():
                if type(count) is int:
                    usage = result.setdefault('usageMetadata', {})
                    usage[key] = max(usage.get(key, 0), count)
        for raw in response:
            if time.monotonic() - started >= timeout:
                raise TimeoutError('model_response_timeout')
            line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
            if not line:
                flush_event()
            elif line.startswith('data:'):
                val = line[5:].lstrip()
                if val != '[DONE]':
                    data.append(val)
        flush_event()
        if candidate.get('finishReason') != 'STOP':
            raise RuntimeError('qq_backup_incomplete_stream')
        result.setdefault('modelVersion', model)
        return result


def _custom_api_request(session, payload, timeout, cfg, root):
    owner = getattr(session, '_owns_turn', None)
    if owner is not None and not owner():
        raise RuntimeError('vertex_turn_ownership_lost')
    if timeout <= 0:
        raise TimeoutError('model_response_timeout')
    api_key = aux_config.resolve_api_key(cfg)
    if not api_key:
        raise OSError('qq_backup_auth_missing')
    model = str(cfg.get('model') or '').strip()
    protocol = str(cfg.get('protocol') or 'openai_compatible')
    session.last_error = {'provider': CUSTOM_BACKUP_PROVIDER, 'model': model, 'message': 'qq_backup_failed'}
    started = time.monotonic()
    try:
        if protocol == 'gemini':
            try:
                res = _gemini_custom_stream(session, payload, timeout, cfg, api_key, model)
            except (HTTPError, URLError, RuntimeError):
                fb = str(cfg.get('fallback_model') or '').strip()
                rem = timeout - (time.monotonic() - started)
                if not fb or rem <= 1:
                    raise
                res = _gemini_custom_stream(session, payload, rem, cfg, api_key, fb)
        else:
            profile = {
                'activated': True,
                'provider': 'openai_compatible',
                'base_url': str(cfg.get('base_url') or '').strip(),
                'api_key': api_key,
                'output_limit_parameter': cfg.get('output_limit_parameter', 'max_tokens'),
                'reasoning_effort': cfg.get('reasoning_effort', 'low'),
            }
            inline_cfg = {'settings': profile, 'provider_name': CUSTOM_BACKUP_PROVIDER}
            try:
                res = chat_request(session, payload, timeout, inline_cfg, root, model)
            except NoAvailableAccounts:
                fb = str(cfg.get('fallback_model') or '').strip()
                rem = timeout - (time.monotonic() - started)
                if not fb or rem <= 1:
                    raise
                res = chat_request(session, payload, rem, inline_cfg, root, fb)
            res['_provider'] = CUSTOM_BACKUP_PROVIDER
        session.last_error = None
        return res
    except Exception as exc:
        if owner is not None and not owner():
            raise RuntimeError('vertex_turn_ownership_lost') from None
        raise OSError(session.last_error['message']) from exc


def request(session, payload, timeout, settings, root):
    if settings.get('mode') == 'custom_api':
        return _custom_api_request(session, payload, timeout, settings['config'], root)
    ownership_lost = False
    owner = session._owns_turn

    def progress():
        nonlocal ownership_lost
        if not owner() or (session._progress and session._progress() is False):
            ownership_lost = True
        return not ownership_lost

    if not progress():
        raise RuntimeError('vertex_turn_ownership_lost')
    model = settings['model']
    config = settings['budget']
    flash_low_standby = bool(config.get('qq_flash_low_profile'))
    duty_enabled = flash_low_standby or bool(config.get('flash_duty_profile') or config.get('qq_hydrogen_profile'))
    companion_text = None
    session.last_error = {'provider': PROVIDER, 'model': model, 'message': 'video_claude_failed'}
    try:
        companion_text, _ = _companion(root, config_path=settings.get('_config_path'))
        declarations, name_map = chat_tools(payload)
        rows = messages(payload)
        turn_timeout = session.timeout if timeout == float('inf') else timeout
        deadline = time.monotonic() + turn_timeout
        options = dict(max_tokens=8192, tools=declarations or None, budget_lane='qq_duty')
        prior_ids = {call['id'] for row in rows for call in row.get('tool_calls', [])}

        def original(primary_model, recover):
            provider = HYDROGEN_PROVIDER if primary_model in HYDROGEN_MODELS else FLASH_PROVIDER
            remaining = turn_timeout if recover else deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('model_response_timeout')
            if not recover:
                if provider == HYDROGEN_PROVIDER or not flash_low_standby:
                    remaining -= min(45 if provider == HYDROGEN_PROVIDER else 30, remaining / 2)
                session.last_error = {'provider': provider, 'model': primary_model,
                                      'message': provider + '_failed'}
            limits = dict(options, max_tokens=2048) if recover else options
            value = companion_text.complete_primary(config, primary_model, rows, **limits, timeout=remaining,
                                                    progress=(lambda: True) if recover else progress)
            try:
                return _result(value, name_map, provider, prior_ids)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                raise companion_text.CognitionUnavailable('companion_qq_invalid_response') from None

        def hybrid(_):
            session.last_error = {'provider': PROVIDER, 'model': model, 'message': 'video_claude_failed'}
            remaining = deadline - time.monotonic() if duty_enabled else turn_timeout
            if remaining <= 0:
                raise TimeoutError('model_response_timeout')
            route = None
            claude_rows = rows
            if settings.get('wire_protocol') == 'anthropic':
                route = (settings['base_url'], str(settings['api_key_file']), model)
                claude_rows = messages(payload, anthropic_route=route)
            value = companion_text.complete(settings, claude_rows, **options, timeout=remaining, progress=progress)
            return _result(value, name_map, PROVIDER, prior_ids, anthropic_route=route)

        if duty_enabled:
            from bili_companion.cognition.flash_duty import HYDROGEN_MODELS, run_text_duty

            if not flash_low_standby and getattr(session, '_video_hybrid_owner', None) is owner:
                result = hybrid(None)
            else:
                result = run_text_duty(config, 'qq', original, hybrid,
                    allow_hydrogen=getattr(session, '_video_flash_owner', None) is not owner)
                if result['_provider'] == PROVIDER:
                    session._video_hybrid_owner = owner
                elif result['_provider'] == FLASH_PROVIDER:
                    session._video_flash_owner = owner
        else:
            result = hybrid(None)
        if not progress():
            raise RuntimeError('vertex_turn_ownership_lost')
    except Exception as exc:
        if ownership_lost or not owner() or getattr(exc, 'code', None) == 'turn_ownership_lost':
            raise RuntimeError('vertex_turn_ownership_lost') from None
        failure = session.last_error['message']
        if companion_text is not None and isinstance(exc, companion_text.BudgetPaused):
            failure = 'model_budget_exhausted'
            session.last_error['message'] = failure
        raise OSError(failure) from None
    session.last_error = None
    return result
