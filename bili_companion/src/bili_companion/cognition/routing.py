"""Media and Claude routes with a Hydrogen backup and independent accounting.

Each engine owns its transport; concurrent QQ workers never swap shared clients,
models or price resolvers. Unknown primary charges remain reserved on fallback.
"""
import hmac
import json
import math
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from bili_companion.policy.budget import AUTONOMOUS_VIDEO_ONLY

from .gateway import CognitionUnavailable, GatewayClient, HYDROGEN, NATIVE_VIDEO_MODEL, hydrogen_mp4
from .live import CognitionEngine, BudgetPaused, prices, _json_response
from .openai_provider import OpenAICompatibleClient, reported_model_matches
from .profile import load_flash_profile, load_profile, load_video_selection_profile
from .video_duty import VideoDuty

PRIMARY = 'gemini-4-hydrogen-high'

TEXT_MODELS = {'fast': 'claude-haiku-5-5', 'reasoning': 'claude-sonnet-5-5'}
FAST_PURPOSES = {'video-selection', 'comment-reply', 'live-decision',
                 'video-child-expression', 'live-child-expression'}
VIDEO_PROFILES = {'video-selection': 'video_selection_profile', 'video-observe': 'video_hydrogen_profile'}
NATIVE_VIDEO_ROUTE = 'video-native-observe'
OBSERVATION_PURPOSES = {'video-observe', 'live-observe'}


def flash_attempt(config, payload, validator, ledger, spend, *, audit_costs,
                  clock=time.time, timeout=90, request_limit=None, budget_lanes=None, **options):
    from .flash_duty import FLASH_MODEL

    profile = load_flash_profile(config, clock=clock)
    maximum = profile.raw['max_output_tokens']
    long_summary = options.get('purpose') == 'video-qq-summary' and not _has_media(payload)
    flash_payload = dict(payload, generationConfig=dict(
        payload.get('generationConfig', {}), maxOutputTokens=8192 if long_summary else maximum))
    client = _profile_client(profile, require_usage=True, timeout=timeout,
                             receipt_cost=True, long_text=long_summary)
    if request_limit is None:
        request_limit = Decimal(str(profile.raw.get('per_request_usd_limit', '.04')))
    engine = CognitionEngine(client, ledger, None, observer_model=FLASH_MODEL,
        decider_model=FLASH_MODEL, price_resolver=profile.prices, max_output_tokens=maximum,
        clock=clock, request_usd_limit=request_limit, audit_costs=audit_costs, spend_ledger=spend,
        call_provider=urlsplit(profile.base_url).hostname, budget_lanes=budget_lanes)
    response, identity, counts = engine._call(FLASH_MODEL, flash_payload, **options)
    reported = response.get('_provider_response_metadata', {}).get('reported_model')
    code = None
    if not reported_model_matches(FLASH_MODEL, reported):
        code = 'flash_model_mismatch'
    else:
        try:
            validator(engine, response)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            code = getattr(error, 'code', 'flash_content_invalid')
    if code is not None:
        failure = CognitionUnavailable(code)
        failure.request_ids = (identity,)
        failure.validation_error = code
        raise failure
    return dict(response, _accepted_flash_model=FLASH_MODEL), identity, counts


class HydrogenRouteEngine(CognitionEngine):
    def __init__(self, *args, fallback=None, text_engine=None, claude_video_engine=None, flash_config=None,
                 flash_spend=None, video_routes=None, legacy_engine=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fallback = fallback
        self.text_engine = text_engine
        self.claude_video_engine = claude_video_engine
        self.flash_config = flash_config
        self.flash_spend = flash_spend
        self.video_routes = video_routes or {}
        self.call_deadline = None
        self.legacy_engine = legacy_engine
        self.media_only = 'terra' in self.observer_model.lower()
        with self.ledger.database.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cognition_routes (request_id TEXT PRIMARY KEY, prior_ids TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS cognition_route_errors (request_id TEXT PRIMARY KEY, code TEXT)')

    def call_validated(self, model, payload, *, response_validator, **kwargs):
        return self._call(model,payload,response_validator=response_validator,**kwargs)

    def _call_for_duty(self, model, payload, validator, **kwargs):
        return self._call(model, payload, flash_validator=validator, **kwargs)

    def _call(self, model, payload, *, response_validator=None, flash_validator=None, **kwargs):
        purpose = kwargs.get('purpose')
        if purpose in self.video_routes or (purpose == 'video-observe' and NATIVE_VIDEO_ROUTE in self.video_routes):
            media = _has_media(payload)
            if purpose == 'video-observe' and not media:
                raise CognitionUnavailable('video_observation_media_required')
            if purpose == 'video-selection' and media:
                raise CognitionUnavailable('video_selection_text_required')
            validator = flash_validator or response_validator
            if purpose == 'video-selection' and validator is None:
                validator = _selection_validator(payload)
            return self._call_video_route(payload, validator or _json_response, **kwargs)
        return self._call_legacy(payload, legacy_model=model, response_validator=response_validator,
                                 flash_validator=flash_validator, **kwargs)

    def _video_route_keys(self, purpose):
        if purpose != 'video-observe':
            return (purpose,)
        relay = self.video_routes.get(purpose)
        if relay and hydrogen_mp4(relay[0].observer_model, getattr(relay[0].client, 'media_mode', None)):
            return purpose, NATIVE_VIDEO_ROUTE
        return NATIVE_VIDEO_ROUTE, purpose

    def request_limit_for(self, purpose):
        for key in self._video_route_keys(purpose):
            route = self.video_routes.get(key)
            if route:
                engine, duty = route
                if not duty.on_standby(self.clock()):
                    return engine.request_limit
        if purpose in OBSERVATION_PURPOSES and self.claude_video_engine is not None:
            return self.claude_video_engine.request_limit
        return self.request_limit

    def set_call_timeout(self, seconds):
        """Bound successive attempts in a per-request QQ worker."""
        self.call_deadline = time.monotonic() + seconds

    def _apply_call_timeout(self, engine):
        if self.call_deadline is not None:
            remaining = self.call_deadline - time.monotonic()
            if remaining < 1:
                raise TimeoutError('viewing_deadline')
            if hasattr(engine.client, 'timeout'):
                engine.client.timeout = min(90, remaining)

    def _call_video_route(self, payload, validator, **kwargs):
        purpose = kwargs['purpose']
        prior = ()
        for key in self._video_route_keys(purpose):
            if key not in self.video_routes:
                continue
            adjusted = _native_video_payload(payload) if key == NATIVE_VIDEO_ROUTE else payload
            try:
                result = self._attempt_video_route(key, adjusted, validator, **kwargs)
            except CognitionUnavailable as error:
                if error.code == 'stale_child_epoch':
                    if prior:
                        error.request_ids = prior + tuple(getattr(error, 'request_ids', ()))
                    raise
                prior += tuple(getattr(error, 'request_ids', ()))
            except Exception as error:
                if prior:
                    error.request_ids = prior + tuple(getattr(error, 'request_ids', ()))
                raise
            else:
                if result is not None:
                    self._record_prior(result[1], prior)
                    return result
        try:
            response, identity, counts = self._call_legacy(payload, response_validator=validator, **kwargs)
        except Exception as error:
            if prior:
                error.request_ids = prior + tuple(getattr(error, 'request_ids', ()))
            raise
        self._record_prior(identity, prior)
        return response, identity, counts

    def _attempt_video_route(self, key, payload, validator, **kwargs):
        engine, duty = self.video_routes[key]
        ticket = duty.admit(self.clock())
        if ticket is None:
            return None
        adjusted = dict(payload, generationConfig=dict(
            payload.get('generationConfig', {}), maxOutputTokens=engine.max_output))
        model = engine.observer_model
        try:
            self._apply_call_timeout(engine)
            response, identity, counts = engine._call(model, adjusted, **kwargs)
            if key == NATIVE_VIDEO_ROUTE:
                # The fixed Gateway URL already pins the requested model.
                # Some responses omit modelVersion; reject a conflicting report.
                reported = response.get('modelVersion')
                if reported is not None and not reported_model_matches(model, reported):
                    self._reject_response(identity, 'video_native_model_mismatch')
                self._validate_content(response, identity, 'video_native', validator)
            else:
                self._validate_primary(response, identity, model, 'video_primary', validator)
        except Exception as error:
            duty.finish(ticket, self.clock(), error)
            raise
        duty.finish(ticket, self.clock())
        return dict(response, _accepted_observation_model=model), identity, counts

    def _call_legacy(self, payload, *, response_validator=None, flash_validator=None, legacy_model=None, **kwargs):
        if kwargs.get('purpose') in OBSERVATION_PURPOSES and self.claude_video_engine is not None:
            response_validator = response_validator or flash_validator
        if self.legacy_engine is not None:
            self._apply_call_timeout(self.legacy_engine)
            model = legacy_model or self.legacy_engine.observer_model
            response, identity, counts = self.legacy_engine._call(model, payload, **kwargs)
            if response_validator is not None:
                self._validate_content(response, identity, 'video_backup', response_validator)
            return dict(response, _accepted_observation_model=model), identity, counts
        if self.flash_config is None:
            return self._call_hybrid(payload, response_validator=response_validator, **kwargs)

        from .flash_duty import run_flash

        purpose = kwargs.get('purpose')
        has_media = _has_media(payload)
        lane = 'video' if purpose == 'video-qq-summary' or has_media else 'review'
        route = self.text_engine if purpose == 'video-qq-summary' and self.text_engine else self
        request_limit = route.request_limit if lane == 'video' else None
        validator = flash_validator or response_validator
        if validator is None and purpose == 'video-selection':
            validator = _selection_validator(payload)
        validator = validator or _json_response

        def attempt(*, recovery=False):
            # Recovery accounts directly to the durable budget store and does
            # not inherit the ownership or cancellation of a finished request.
            owner = self.flash_spend if recovery else self.ledger
            options = dict(kwargs)
            timeout = 90
            if recovery:
                options.pop('epoch_guard', None)
                options.pop('parent_id', None)
            else:
                self._apply_call_timeout(route)
                timeout = getattr(route.client, 'timeout', timeout)
            try:
                return flash_attempt(self.flash_config, payload, lambda engine, response: validator(response),
                    owner, self.flash_spend, audit_costs=recovery or self.audit_costs,
                    clock=self.clock, timeout=timeout, request_limit=request_limit,
                    budget_lanes=self.budget_lanes, **options)
            except CognitionUnavailable as failure:
                if not recovery and getattr(failure, 'validation_error', None):
                    with owner.database.connect(write=True) as db:
                        db.execute('INSERT OR REPLACE INTO cognition_route_errors VALUES (?,?)',
                                   (failure.request_ids[0], failure.validation_error))
                raise

        def hybrid(error):
            prior = tuple(getattr(error, 'request_ids', ()))
            try:
                response, identity, counts = self._call_hybrid(
                    payload, response_validator=response_validator, **kwargs)
            except Exception as failure:
                if prior:
                    failure.request_ids = prior + tuple(getattr(failure, 'request_ids', ()))
                raise
            self._record_prior(identity, prior)
            return response, identity, counts

        if not self.audit_costs and purpose == 'video-observe' and has_media:
            from .flash_video_recovery import launch
            return run_flash(self.flash_config, lane, attempt, hybrid,
                             launch_recovery=lambda generation, lease: launch(
                                 self.flash_config, payload, generation, lease, request_limit))
        recovery = None if lane == 'video' and not has_media else lambda: attempt(recovery=True)
        return run_flash(self.flash_config, lane, attempt, hybrid, recovery=recovery)

    def _record_prior(self, identity, prior):
        if prior:
            with self.ledger.database.connect(write=True) as db:
                row = db.execute('SELECT prior_ids FROM cognition_routes WHERE request_id=?', (identity,)).fetchone()
                if row:
                    prior += tuple(json.loads(row[0]))
                db.execute('INSERT OR REPLACE INTO cognition_routes VALUES (?,?)', (identity, json.dumps(prior)))

    def _call_hybrid(self, payload, *, response_validator=None, **kwargs):
        # No mutable per-request routing state: engines can be shared by threads.
        purpose = kwargs.get('purpose')
        has_media = _has_media(payload)
        text = (self.text_engine is not None or self.media_only) and not has_media
        if text and self.text_engine is None:
            if self.fallback is None:
                raise CognitionUnavailable('claude_text_profile_required')
            return self._call_backup(payload, response_validator, (), **kwargs)

        primary_model = self.observer_model
        primary_call = super()._call
        primary_engine = self
        error_prefix = 'hydrogen_primary'
        if text:
            primary_model = (self.text_engine.decider_model if purpose in FAST_PURPOSES
                             else self.text_engine.observer_model)
            primary_call = self.text_engine._call
            primary_engine = self.text_engine
            error_prefix = 'claude_primary'
        elif has_media and purpose in OBSERVATION_PURPOSES and self.claude_video_engine is not None:
            primary_engine = self.claude_video_engine
            primary_model = primary_engine.observer_model
            primary_call = primary_engine._call
            error_prefix = 'claude_video'
            payload = dict(payload, generationConfig=dict(
                payload.get('generationConfig', {}), maxOutputTokens=primary_engine.max_output))

        primary_payload = payload
        long_summary = (self.text_engine.client.usage_panel_actual_cost if text else
                        primary_model == PRIMARY or getattr(self.client, 'media_mode', None) == 'frames_transcript')
        if purpose == 'video-qq-summary' and long_summary:
            primary_payload = deepcopy(payload)
            primary_payload.setdefault('generationConfig', {})['maxOutputTokens'] = 8192
        try:
            self._apply_call_timeout(primary_engine)
            response, identity, counts = primary_call(primary_model, primary_payload, **kwargs)
            self._validate_primary(response, identity, primary_model, error_prefix, response_validator or _json_response)
            return dict(response, _accepted_observation_model=primary_model), identity, counts
        except BudgetPaused:
            # Budget denial is never permission to route around a hard limit.
            raise
        except CognitionUnavailable as primary_error:
            if self.fallback is None:
                raise
            prior = tuple(getattr(primary_error, 'request_ids', ()))
            return self._call_backup(payload, response_validator, prior, **kwargs)

    def _validate_primary(self, response, identity, model, prefix, validator):
        # A paid response can fail model identity or content validation.
        reported = response.get('_provider_response_metadata', {}).get('reported_model')
        matches = reported == PRIMARY if model == PRIMARY else reported_model_matches(model, reported)
        if not matches:
            self._reject_response(identity, f'{prefix}_model_mismatch')
        self._validate_content(response, identity, prefix, validator)

    def _validate_content(self, response, identity, prefix, validator):
        try:
            validator(response)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self._reject_response(identity, getattr(error, 'code', f'{prefix}_content_invalid'))

    def _reject_response(self, identity, code):
        with self.ledger.database.connect(write=True) as db:
            db.execute('INSERT OR REPLACE INTO cognition_route_errors VALUES (?,?)', (identity, code))
        failure = CognitionUnavailable(code)
        failure.request_ids = (identity,)
        raise failure from None

    def _call_backup(self, payload, response_validator, prior, **kwargs):
        fallback_payload = deepcopy(payload)
        generation = fallback_payload.setdefault('generationConfig', {})
        generation['maxOutputTokens'] = min(generation.get('maxOutputTokens', 1536), 1536)
        try:
            self._apply_call_timeout(self.fallback)
            response, identity, counts = self.fallback._call(HYDROGEN, fallback_payload, **kwargs)
            self._validate_content(response, identity, 'hydrogen_backup', response_validator or _json_response)
        except Exception as fallback_error:
            fallback_error.request_ids = prior + tuple(getattr(fallback_error, 'request_ids', ()))
            raise
        self._record_prior(identity, prior)
        return dict(response, _accepted_observation_model=HYDROGEN), identity, counts

    def role_events_for(self, request_ids):
        identities = []
        with self.ledger.database.connect() as db:
            for identity in request_ids:
                row = db.execute('SELECT prior_ids FROM cognition_routes WHERE request_id=?', (identity,)).fetchone()
                if row:
                    identities.extend(json.loads(row[0]))
                identities.append(identity)
        events=super().role_events_for(tuple(dict.fromkeys(identities)))
        with self.ledger.database.connect() as db:
            for event in events:
                identity=event.get('request_id',event.get('id'))
                row=db.execute('SELECT code FROM cognition_route_errors WHERE request_id=?',(identity,)).fetchone()
                if row:event['validation_error']=row[0]
        return events


def _has_media(payload):
    return any('inlineData' in part or 'fileData' in part
               for content in payload.get('contents', ()) for part in content.get('parts', ()))


def _native_video_payload(payload):
    # The MP4 retains its soundtrack. Ask for roughly eight visual samples over
    # a normal bounded segment, with a 2 fps ceiling for very short clips.
    adjusted = deepcopy(payload)
    for content in adjusted.get('contents', []):
        duration = None
        for part in content.get('parts', []):
            if isinstance(part.get('text'), str):
                try:
                    duration = json.loads(part['text']).get('duration')
                except (ValueError, AttributeError):
                    pass
        fps = 1.
        if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
            fps = min(2., max(.25, 8 / duration))
        for part in content.get('parts', []):
            if part.get('inlineData', {}).get('mimeType') == 'video/mp4':
                part['videoMetadata'] = {'fps': fps}
    return adjusted


def _selection_validator(payload):
    data = json.loads(payload['contents'][0]['parts'][0]['text'])
    choices = {item['bvid'] for item in data['candidates']}

    def validate(response):
        value = _json_response(response)
        if (set(value) != {'bvid', 'reason'}
                or value['bvid'] is not None and value['bvid'] not in choices
                or type(value['reason']) is not str or len(value['reason']) > 120):
            raise ValueError('invalid selection')
    return validate


def _profile_client(profile, *, require_usage, timeout=90, receipt_cost=False, long_text=False,
                    retain_frames_without_speech=False):
    raw = profile.raw
    return OpenAICompatibleClient(profile.base_url, api_key_file=profile.api_key_file,
        input_token_upper_bound=raw['input_token_upper_bound'], media_mode=raw['media_mode'],
        output_limit_parameter=raw['output_limit_parameter'], reasoning_effort=raw.get('reasoning_effort'),
        require_usage=require_usage,
        usage_panel_actual_cost=receipt_cost or (raw.get('usage_panel_actual_cost', False) and require_usage),
        payload_token_envelope=receipt_cost or long_text or raw.get('payload_token_envelope', False),
        cached_input_rate=raw.get('cached_input_rate'),
        send_json_mode=raw.get('send_json_mode', True), wire_protocol=raw.get('wire_protocol', 'openai'),
        timeout=timeout, allow_long_text_output=long_text,
        retain_frames_without_speech=retain_frames_without_speech)


def _video_engine(profile, ledger, memory, options, *, timeout=90, long_text=False):
    model = profile.observer_model
    raw = profile.raw
    if not raw.get('single_pass'):
        raise CognitionUnavailable('video_single_pass_required')
    client = _profile_client(profile, require_usage=True, timeout=timeout, receipt_cost=True, long_text=long_text,
                             retain_frames_without_speech=raw['media_mode']=='frames_transcript')
    return CognitionEngine(client, ledger, memory, observer_model=model, decider_model=model,
        price_resolver=profile.prices, call_provider=urlsplit(profile.base_url).hostname,
        **dict(options, max_output_tokens=min(raw['max_output_tokens'], 4096 if hydrogen_mp4(model, raw['media_mode']) else 2048),
               request_usd_limit=raw.get('per_request_usd_limit', '.04')))


def build_qq_feedback_engine(config, ledger, memory, *, timeout=90):
    from .video_budget import video_budget

    profile = load_video_selection_profile(config)
    return _video_engine(profile, ledger, memory, dict(
        audit_costs=False, spend_ledger=video_budget(config, lane='qq_video'), single_pass=True),
        timeout=timeout, long_text=True)


def has_video_profiles(config):
    return any(config.get(key) for key in (*VIDEO_PROFILES.values(), 'video_native_vertex_model'))


def _same_relay_account(first_url, first_key, second_url, second_key):
    if first_url.rstrip('/') != second_url.rstrip('/'):
        return False
    first, second = Path(first_key).resolve(), Path(second_key).resolve()
    first_value = first.read_bytes().strip()
    return bool(first_value) and (first == second or hmac.compare_digest(first_value, second.read_bytes().strip()))


def claude_video_profile(config, text_settings, active_profile):
    path = config.get('claude_video_profile')
    if not path or text_settings is None:
        return None
    try:
        profile = load_profile(path)
        if not (profile.raw['media_mode'] == 'frames_transcript' and profile.raw['single_pass']):
            return None
        account = (text_settings['base_url'], text_settings['api_key_file'])
        if (_same_relay_account(profile.base_url, profile.api_key_file, *account)
                and _same_relay_account(active_profile.base_url, active_profile.api_key_file, *account)):
            return profile
    except (CognitionUnavailable, OSError, ValueError, TypeError, KeyError, RuntimeError):
        # Standby may no longer be usable after an operator changes accounts.
        return None
    return None


def _build_video_routes(config, ledger, memory, options):
    routes = {}
    for purpose, key in VIDEO_PROFILES.items():
        path = config.get(key)
        if not path:
            continue
        profile = load_video_selection_profile(config) if purpose == 'video-selection' else load_profile(path)
        engine = _video_engine(profile, ledger, memory, options)
        routes[purpose] = engine, VideoDuty(engine.spend_ledger.database, purpose, path)
    model = config.get('video_native_vertex_model')
    if model is not None:
        if model != NATIVE_VIDEO_MODEL:
            raise CognitionUnavailable('video_native_model_unverified')
        engine = CognitionEngine(GatewayClient.for_hydrogen(config), ledger, memory,
            observer_model=model, decider_model=model, price_resolver=prices, call_provider='vertex',
            **dict(options, max_output_tokens=4096, request_usd_limit='.04'))
        routes[NATIVE_VIDEO_ROUTE] = engine, VideoDuty(engine.spend_ledger.database, NATIVE_VIDEO_ROUTE, model)
    return routes


def build_video_overlay(config, legacy_engine):
    """Keep explicitly configured duties when restoring a pre-Hydrogen standby."""
    if not has_video_profiles(config):
        return legacy_engine
    engine = legacy_engine
    options = dict(persona_loader=engine.persona, bridge_store=engine.bridge,
        clock=engine.clock, max_output_tokens=engine.max_output, decision_interval=engine.decision_interval,
        request_usd_limit=engine.request_limit, observer_model=engine.observer_model,
        decider_model=engine.decider_model, price_resolver=engine.price_resolver,
        single_pass=engine.single_pass, child_roles_enabled=engine.child_roles_enabled,
        audit_costs=engine.audit_costs, spend_ledger=engine.spend_ledger, budget_lanes=engine.budget_lanes)
    routes = _build_video_routes(config, engine.ledger, engine.memory,
        dict(audit_costs=engine.audit_costs, spend_ledger=engine.spend_ledger,
             budget_lanes=engine.budget_lanes, single_pass=True))
    return HydrogenRouteEngine(engine.client, engine.ledger, engine.memory,
        video_routes=routes, legacy_engine=engine, **options)


def build_route(config, ledger, memory, *, persona_loader=None, bridge_store=None, audit_costs=True,
                budget_lanes=None):
    route = config['hydrogen_route']
    if type(route) is not dict or set(route) - {'primary_profile', 'primary_model', 'fallback_enabled'}:
        raise ValueError('invalid Hydrogen route configuration')
    fallback_enabled = route.get('fallback_enabled', True)
    if type(fallback_enabled) is not bool:
        raise ValueError('fallback_enabled must be boolean')
    primary = route.get('primary_model', PRIMARY)
    replacement = 'primary_model' in route
    scoped = config.get('budget_scope') == AUTONOMOUS_VIDEO_ONLY
    profile = load_profile(route.get('primary_profile') or config['provider_profile'],
                           audit_costs=audit_costs or replacement or scoped)
    if profile.observer_model != primary or profile.decider_model != primary:
        raise CognitionUnavailable('hydrogen_primary_model_required')
    if not profile.raw.get('single_pass'):
        raise CognitionUnavailable('hydrogen_single_pass_required')
    text_settings = None
    if 'text_provider_profile' in config or 'text_models' in profile.raw:
        from .companion_text import claude_sonnet_effort, rates_for, text_profile
        text_settings = text_profile(config)
        if text_settings is None:
            text_settings = profile.raw
        models = text_settings.get('text_models')
        haiku_for_both = config.get('text_provider_profile') and models == dict.fromkeys(TEXT_MODELS, TEXT_MODELS['fast'])
        if models != TEXT_MODELS and not haiku_for_both:
            raise CognitionUnavailable('claude_text_models_invalid')
    flash_enabled = config.get('flash_duty_profile') is not None
    use_video_profiles = has_video_profiles(config)
    client = _profile_client(profile, require_usage=audit_costs or replacement or flash_enabled or scoped)
    common = dict(persona_loader=persona_loader, bridge_store=bridge_store, single_pass=True,
        child_roles_enabled=False, audit_costs=audit_costs, max_output_tokens=profile.raw['max_output_tokens'],
        decision_interval=max(90, config.get('cooldown_seconds', 90)),
        request_usd_limit=profile.raw.get('per_request_usd_limit', '.04'), budget_lanes=budget_lanes)
    paid = common
    if (replacement or text_settings is not None or flash_enabled or use_video_profiles or scoped
            or config.get('claude_video_profile')) and not audit_costs:
        # Keep QQ turn ownership separate from the monetary ledger.
        from .video_budget import video_budget
        paid = dict(common, spend_ledger=video_budget(config, lane='qq_video'))
    fallback = None
    if fallback_enabled:
        fallback = CognitionEngine(GatewayClient.for_hydrogen(config), ledger, memory,
            observer_model=HYDROGEN, decider_model=HYDROGEN, price_resolver=prices,
            call_provider='vertex' if use_video_profiles or scoped else None,
            **dict(paid,max_output_tokens=1536,request_usd_limit='.04'))
    text_engine = None
    if text_settings is not None:
        settings = text_settings['text_models']
        panel_cost = text_settings.get('usage_panel_actual_cost', False)
        standalone_text_profile = bool(config.get('text_provider_profile'))
        text_client = OpenAICompatibleClient(
            text_settings.get('base_url', profile.base_url),
            api_key_file=text_settings.get('api_key_file', profile.api_key_file),
            input_token_upper_bound=text_settings.get('input_token_upper_bound', profile.raw['input_token_upper_bound']),
            media_mode='frames_transcript', output_limit_parameter='max_tokens',
            wire_protocol=text_settings['wire_protocol'] if standalone_text_profile else 'openai',
            reasoning_effort=claude_sonnet_effort(text_settings, settings['reasoning']) if standalone_text_profile else None,
            reasoning_effort_model=settings['reasoning'],
            usage_panel_actual_cost=panel_cost, payload_token_envelope=True)
        text_engine = CognitionEngine(text_client, ledger, memory,
            observer_model=settings['reasoning'], decider_model=settings['fast'],
            price_resolver=lambda model, now: rates_for(model),
            call_provider=urlsplit(text_settings.get('base_url', profile.base_url)).hostname,
            **dict(paid, request_usd_limit='.20' if panel_cost else common['request_usd_limit']))
    video_routes = _build_video_routes(config, ledger, memory, paid)
    claude_video = claude_video_profile(config, text_settings, profile)
    claude_video_engine = _video_engine(claude_video, ledger, memory, paid) if claude_video else None
    return HydrogenRouteEngine(client, ledger, memory, observer_model=primary,
        decider_model=text_engine.decider_model if text_engine is not None else primary,
        price_resolver=profile.prices, fallback=fallback, text_engine=text_engine,
        claude_video_engine=claude_video_engine,
        call_provider=urlsplit(profile.base_url).hostname,
        flash_config=config if flash_enabled else None,
        flash_spend=(ledger if audit_costs else paid['spend_ledger']) if flash_enabled else None,
        video_routes=video_routes,
        **(paid if replacement or flash_enabled or video_routes or scoped else common))
