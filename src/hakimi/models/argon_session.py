"""Shared API backups for the authorized Gemini tool loop."""
import json
import hashlib
import time
import os
from http.client import HTTPException
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from hakimi.models.vertex_session import VertexSession, _NoRedirect


def backup_config(root):
    try:
        value = json.loads((Path(root)/'state/argon_backup.json').read_text())
        if (value.get('enabled') is True and value.get('model') == 'gemini-4-argon-high'
                and Path(value['profile']).is_absolute()):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def wire_name(name):
    return 'f_' + hashlib.sha256(name.encode()).hexdigest()[:48]


def _media_blocks(parts):
    blocks = []
    for part in parts:
        if part.get('text'):
            blocks.append({'type': 'text', 'text': part['text']})
        if 'inlineData' in part:
            image = part['inlineData']
            blocks.append({'type': 'image_url', 'image_url': {
                'url': 'data:' + image['mimeType'] + ';base64,' + image['data']}})
    return blocks


def messages(payload, *, anthropic_route=None):
    rows = []
    system = payload.get('systemInstruction', {}).get('parts', [])
    instructions = '\n'.join(part['text'] for part in system if part.get('text'))
    if instructions:
        rows.append({'role': 'system', 'content': instructions})
    pending = []
    for content in payload['contents']:
        parts = content.get('parts', [])
        if content['role'] == 'model':
            calls = []
            text = []
            for p in parts:
                if p.get('text') and not p.get('thought'): text.append(p['text'])
                if 'functionCall' in p:
                    c = p['functionCall']; ident = c.get('id') or 'call_' + str(len(rows)) + '_' + str(len(calls))
                    calls.append({'id': ident, 'type': 'function', 'function': {'name': wire_name(c['name']), 'arguments': json.dumps(c.get('args', {}))}})
            row = {'role': 'assistant', 'content': '\n'.join(text) or None}
            if calls: row['tool_calls'] = calls
            if calls and anthropic_route is not None and getattr(content, 'anthropic_route', None) == anthropic_route:
                row['_anthropic_content'] = content.anthropic_content
            pending = [(c['id'], c['function']['name']) for c in calls]
            rows.append(row)
            continue
        media = []
        for p in parts:
            if 'functionResponse' in p:
                r = p['functionResponse']
                match = next(((i, n) for i, n in pending if (i == r['id'] if r.get('id') else n == wire_name(r['name']))), None)
                if match is None: raise RuntimeError('argon_unmatched_tool_result')
                pending.remove(match)
                body = json.dumps(r['response'], ensure_ascii=False)
                images = [image for image in r.get('parts', []) if 'inlineData' in image]
                if images and anthropic_route is not None:
                    body = [{'type': 'text', 'text': body}, *_media_blocks(images)]
                else:
                    media.extend(images)
                rows.append({'role': 'tool', 'tool_call_id': match[0], 'content': body})
            else: media.append(p)
        blocks = _media_blocks(media)
        if blocks: rows.append({'role': 'user', 'content': blocks})
    return rows


class NoAvailableAccounts(OSError):
    """Parsed provider rejection before response streaming; safe fixed code."""


class ArgonSession(VertexSession):
    def _vertex_request(self, payload, timeout):
        return super()._request(payload, timeout)

    def generate(self, prompt, coordinator, claim, **kwargs):
        from hakimi.models import hydrogen_duty, vertex_route, video_hydrogen
        self._duty_root = vertex_route.root_for(self.cwd)
        self._video_profile = video_hydrogen.config(self._duty_root)
        self._video_turn = False
        self._duty_checked = False
        self._failure_counted = False
        if self._video_profile:
            duty = hydrogen_duty.snapshot(self._duty_root)
            self._duty_epoch = duty['epoch']
            self._video_turn = duty['mode'] == 'video_hydrogen'
        self._probe_due = self._video_turn
        self._probe_event = claim['payload']
        self._owns_turn = lambda: coordinator.check_token(claim['token'], 'gemini')
        self._using_backup = False
        try:
            return super().generate(prompt, coordinator, claim, **kwargs)
        finally:
            self._owns_turn = None
            self._probe_event = None

    def _defer_vertex_handoff(self):
        return False

    def _observed_vertex_request(self, payload, timeout):
        from hakimi.models import hydrogen_duty
        if not getattr(self, '_video_profile', None):
            return self._vertex_request(payload, timeout)
        started = time.monotonic()
        try:
            value = self._vertex_request(payload, timeout)
            candidate = (value.get('candidates') or [{}])[0]
            if not value.get('_carbon_error') and not candidate.get('content', {}).get('parts'):
                raise RuntimeError('vertex_empty_response')
        except (RuntimeError, OSError, HTTPException) as exc:
            key = hydrogen_duty.classify_vertex_error(exc, self.last_error)
            if not key or self._failure_counted:
                raise
            self._failure_counted = True
            duty = hydrogen_duty.record(self._duty_root, self._duty_epoch, key)
            if duty['mode'] != 'video_hydrogen' or self._defer_vertex_handoff():
                raise
            self._video_turn = True
            # This turn has already sent its real Vertex request.
            return self._video_request(payload, timeout - (time.monotonic() - started))
        if not value.get('_carbon_error'):
            hydrogen_duty.record(self._duty_root, self._duty_epoch, None)
            self._failure_counted = False
        return value

    def _video_request(self, payload, timeout):
        from hakimi.models import hydrogen_duty, video_hydrogen
        if not self._owns_turn():
            raise RuntimeError('vertex_turn_ownership_lost')
        if timeout <= 0:
            raise TimeoutError('model_response_timeout')
        if self._probe_due:
            self._probe_due = False
            hydrogen_duty.start_probe(self._duty_root, self._duty_epoch, self.env, self._probe_event)
        self._using_backup = True
        self.last_error = None
        return video_hydrogen.request(self, payload, timeout, self._video_profile, self._duty_root)

    def _request(self, payload, timeout):
        from hakimi.models import vertex_route, video_hydrogen

        started = time.monotonic()
        duty_checked = getattr(self, '_duty_checked', False)
        if getattr(self, '_video_turn', False) and duty_checked:
            return self._video_request(payload, timeout)
        route = vertex_route.load(self.cwd)
        config = backup_config(route.root) if route.model == 'gemini-4-argon' else None
        has_video = bool(getattr(self, '_video_profile', None))
        status = {}
        if config or (has_video and not duty_checked):
            status = route.check()
            if status.get('blocked'):
                raise RuntimeError('vertex_route_blocked')
        self._duty_checked = True
        if has_video and (self._video_turn or status.get('active_provider') in video_hydrogen.DUTY_PROVIDERS):
            self._video_turn = True
            return self._video_request(payload, timeout - (time.monotonic() - started))

        if not getattr(self, '_using_backup', False) and status.get('active_provider') != 'argon_backup':
            try:
                return self._observed_vertex_request(payload, timeout - (time.monotonic() - started))
            except RuntimeError:
                if getattr(self, '_video_turn', False):
                    raise
                error = self.last_error or {}
                message = error.get('message')
                monthly = message == vertex_route.MONTHLY_EXHAUSTED
                upstream_limit = (error.get('http_status') == 429
                                  and message not in ('vertex_budget_paused', 'manual_pause', 'daily_request_limit', 'gateway_busy'))
                upstream_timeout = (error.get('http_status') == 502 and message == 'upstream_auth_or_transport_failed')
                legacy_fallback = (config and self.env.get('AGY_LLM_GATEWAY_MODELS') == 'gemini-4-hydrogen'
                                   and config.get('fallback_model') == 'gemini-3.8-flash-high'
                                   and (upstream_limit or upstream_timeout or message == 'vertex_timeout_before_headers'))
                if has_video and (monthly or legacy_fallback):
                    self._video_turn = True
                    return self._video_request(payload, timeout - (time.monotonic() - started))
                if not (monthly and config):
                    raise
                self.last_error = None
        if not config:
            raise RuntimeError('vertex_route_blocked')
        self._using_backup = True
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError('argon_response_timeout')
        return self._backup_request(payload, remaining, config, route.root)

    def _backup_request(self, payload, timeout, config, root):
        model = config['model']
        started = time.monotonic()
        try:
            return self._provider_request(payload, timeout, config, root, model)
        except NoAvailableAccounts:
            if not getattr(self, '_video_profile', None) or config.get('fallback_model') != 'gemini-3.8-flash-high':
                raise
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('argon_response_timeout') from None
            # Pin the current turn after an explicit pre-response rejection.
            self._video_turn = True
            return self._video_request(payload, remaining)

    def _provider_request(self, payload, timeout, config, root, model):
        return chat_request(self, payload, timeout, config, root, model)


def _append_usage(path, record):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(record) + '\n')
        stream.flush()
        if record.get('event') == 'reserved':
            os.fsync(stream.fileno())


def chat_tools(payload):
    declarations = [d for tool in payload.get('tools', []) for d in tool.get('functionDeclarations', [])]
    name_map = {wire_name(d['name']): d['name'] for d in declarations}
    if len(name_map) != len(declarations):
        raise RuntimeError('argon_tool_name_collision')
    tools = [{'type': 'function', 'function': {'name': wire_name(d['name']),
              'description': d.get('description') or d['name'],
              'parameters': d.get('parametersJsonSchema', d.get('parameters', {}))}}
             for d in declarations]
    return tools, name_map


def chat_request(session, payload, timeout, config, root, model):
    from hakimi.gateway import openai_compat
    provider = config.get('provider_name', 'argon_backup')
    profile = config.get('settings') or json.loads(Path(config['profile']).read_text())
    if profile.get('activated') is not True or profile.get('provider') != 'openai_compatible':
        raise OSError('argon_profile_inactive')
    origin = profile['base_url'].rstrip('/')
    if not origin.startswith('https://') and not origin.startswith(('http://127.0.0.1', 'http://localhost')):
        raise RuntimeError('argon_profile_invalid')
    key = str(profile['api_key']).strip() if profile.get('api_key') else Path(profile['api_key_file']).read_text().strip()
    limit_name = profile.get('output_limit_parameter', 'max_tokens')
    if limit_name not in {'max_tokens', 'max_completion_tokens'}: raise RuntimeError('argon_profile_invalid')
    body = {'model': model, 'messages': messages(payload),
            'stream': True, 'stream_options': {'include_usage': True}, limit_name: 8192}
    effort = profile.get('reasoning_effort', 'low')
    if effort in {'off', 'low', 'medium', 'high'}:
        body['reasoning_effort'] = effort
    tools, name_map = chat_tools(payload)
    if tools:
        body['tools'] = tools
    wire = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    chat_url = origin + '/chat/completions' if provider == 'argon_backup' else openai_compat.openai_chat_url(origin)
    request = Request(chat_url, data=wire, headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json'})
    failure = provider + '_failed'
    session.last_error = {'provider': provider, 'model': model, 'message': failure}
    request_id = uuid.uuid4().hex
    usage_path = Path(root)/'state'/(provider + '_usage.jsonl')
    started = time.monotonic(); text = []; calls = {}; finish = None; usage = {}; events = 0; error_code = None
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=min(timeout, 90)) as response:
            for raw in response:
                if time.monotonic()-started >= timeout: raise TimeoutError('argon_response_timeout')
                if len(raw) > 2*1024*1024: raise RuntimeError('argon_stream_event_too_large')
                line = raw.decode().strip()
                if not line.startswith('data:'): continue
                data = line[5:].strip()
                if data == '[DONE]': break
                event = json.loads(data); events += 1
                if event.get('error'): raise RuntimeError('argon_stream_error')
                if event.get('usage'): usage = event['usage']
                for choice in event.get('choices', [])[:1]:
                    delta = choice.get('delta', {})
                    if delta.get('content'): text.append(delta['content'])
                    for call in delta.get('tool_calls', []):
                        target = calls.setdefault(call['index'], {'id': '', 'name': '', 'arguments': ''})
                        if call.get('id'): target['id'] = call['id']
                        for name, value in call.get('function', {}).items():
                            if name in {'name', 'arguments'}: target[name] += value
                    if delta.get('content') or delta.get('tool_calls') or delta.get('reasoning_content'):
                        if session._progress: session._progress()
                    finish = choice.get('finish_reason') or finish
        if finish not in {'stop', 'tool_calls'}: raise RuntimeError('argon_incomplete_stream')
        parts = [{'text': ''.join(text)}] if text else []
        for c in calls.values():
            parts.append({'functionCall': {'id': c['id'], 'name': name_map[c['name']], 'args': json.loads(c['arguments'] or '{}')}})
        reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
        output = usage.get('completion_tokens', 0)
        counts = {'promptTokenCount': usage.get('prompt_tokens', 0), 'candidatesTokenCount': max(0, output-reasoning),
                  'thoughtsTokenCount': reasoning, 'totalTokenCount': usage.get('total_tokens', 0),
                  'cachedContentTokenCount': (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0)}
        session.last_error = None
        return {'modelVersion': model, '_provider': provider, '_provider_request_id': request_id,
                'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP'}],
                'usageMetadata': counts}
    except HTTPError as exc:
        # OSError bypasses the inherited RuntimeError retry policy, preserving HTTP status.
        error_code = failure
        try:
            detail = json.loads(exc.read(65537))
            message = detail.get('error', {}).get('message', '')
            if isinstance(message, str) and 'no available accounts' in message.lower():
                error_code = 'no_available_accounts'
        except (ValueError, OSError, AttributeError, TypeError):
            pass
        finally:
            exc.close()
        session.last_error = {'provider': provider, 'model': model,
                           'http_status': exc.code, 'message': error_code}
        if error_code == 'no_available_accounts':
            raise NoAvailableAccounts(error_code) from None
        raise OSError(error_code) from None
    finally:
        # Append-only provider usage, separate from both Vertex and video budgets.
        record = {'at': time.time(), 'model': model, 'reported_model': None,
                  'request_id': request_id, 'error_code': error_code or (failure if session.last_error else None),
                  'usage': usage, 'events': events, 'finish_reason': finish}
        _append_usage(usage_path, record)
