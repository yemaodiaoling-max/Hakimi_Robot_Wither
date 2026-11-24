"""Operator entry point on the video host. Requests and secrets arrive on SSH stdin."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from bili_companion.cognition.gateway import CognitionUnavailable, hydrogen_mp4
from bili_companion.cognition.live import BudgetPaused, CognitionEngine, _json_response
from bili_companion.cognition.openai_provider import OpenAICompatibleClient
from bili_companion.cognition.profile import amount, load_profile
from bili_companion.cognition.provider_probe import _endpoint
from bili_companion.cognition.routing import (
    NATIVE_VIDEO_ROUTE, PRIMARY, VIDEO_PROFILES, claude_video_profile, has_video_profiles,
    reported_model_matches,
)
from bili_companion.cognition.video_budget import video_budget
from .video_sample import make_clip, check_answer

ROOT=Path('/opt/hakimi/bili_companion')
CONFIG=ROOT/'deploy/watcher.json'
PRIVATE=ROOT/'state/private/provider/switch'
UNIT='hakimi-bilibili-watcher.service'
REVIEW_PROFILES={'primary':'qq_review_sonnet_profile','alternate':'qq_review_haiku_profile'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp=Path(handle.name)
        try:
            handle.write(data.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp,path)
    finally:
        temp.unlink(missing_ok=True)


def independent_duties(config):
    return has_video_profiles(config) or any(config.get(key) for key in REVIEW_PROFILES.values())


def revision(config, *, pre_activation_history=False):
    route=[config.get(k) for k in ('hydrogen_route','provider_profile','vertex_model')]
    if config.get('flash_duty_profile') and not pre_activation_history:
        route.append(config['flash_duty_profile'])
        if config.get('qq_hydrogen_profile') and not independent_duties(config):
            route.append(config['qq_hydrogen_profile'])
    return hashlib.sha256(json.dumps(route,sort_keys=True).encode()).hexdigest()[:24]


def matches_history(config, history):
    if history.get('active') == revision(config):
        return True
    # Older panel activations removed Flash and QQ Hydrogen before recording
    # the active revision. Independent duties may now restore those settings.
    return independent_duties(config) and history.get('active') == revision(config, pre_activation_history=True)


def service():
    proc=subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','SubState','-p','MainPID'],
                        capture_output=True,text=True,check=True,timeout=8)
    return dict(line.split('=',1) for line in proc.stdout.splitlines() if '=' in line)


def status():
    config=read(CONFIG)
    route=config.get('hydrogen_route')
    profile_path=(route.get('primary_profile') or config.get('provider_profile')) if isinstance(route,dict) else (
        None if config.get('vertex_model') else config.get('provider_profile'))
    profile=read(Path(profile_path)) if profile_path else {}
    from bili_companion.cognition.companion_text import review_standby_settings, text_profile
    from bili_companion.cognition.flash_duty import FLASH_MODEL, active_text_models, duty_modes, preferred_text_duty
    text_settings=text_profile(config)
    text_models=dict((text_settings or {}).get('text_models', {}))
    primary=profile.get('observer_model') or config.get('vertex_model') or '默认观察模型'
    host=urlsplit(profile.get('base_url','')).hostname or ('vertex' if config.get('vertex_model') else None)
    media_mode=profile.get('media_mode') or 'native_video'
    selection_backup=(text_models.get('fast', primary), host)
    standby={}
    text_fallbacks={}
    pinned=config.get('qq_flash_low_profile')
    preferred_qq=preferred_text_duty(config, 'qq')
    preferred_review=None if pinned else preferred_text_duty(config, 'review')
    preferred_review_haiku=preferred_text_duty(config, 'review_haiku') if not pinned and config.get(REVIEW_PROFILES['alternate']) else None
    configured_review=any(config.get(key) for key in REVIEW_PROFILES.values())
    review_route=None
    modes=duty_modes(config)
    active=active_text_models(config)
    claude_video=None
    if config.get('claude_video_profile') and isinstance(route,dict) and profile_path:
        try:
            claude_profile=claude_video_profile(config,text_settings,load_profile(profile_path))
        except CognitionUnavailable:
            claude_profile=None
        if claude_profile is not None:
            claude_video=dict(model=claude_profile.observer_model,
                provider=urlsplit(claude_profile.base_url).hostname,media_mode=claude_profile.raw['media_mode'])
    if claude_video is not None:
        primary,host=claude_video['model'],claude_video['provider']
        media_mode=claude_video['media_mode']
    if config.get('flash_duty_profile'):
        standby=dict(video=primary, **text_models)
        flash_profile=read(Path(config['flash_duty_profile']))
        flash_host=urlsplit(flash_profile['base_url']).hostname
        if route is not None and modes['video']=='flash':
            primary,host=FLASH_MODEL,flash_host
            media_mode=flash_profile['media_mode']
        if route is not None and modes['review']=='flash':
            selection_backup=FLASH_MODEL,flash_host
    for role,lane in (('fast','review'),('reasoning','qq')):
        if lane=='review' and configured_review:
            continue
        if lane in active:
            backup=text_models.get(role)
            text_models[role]=active[lane]
            backups=[FLASH_MODEL] if active[lane]!=FLASH_MODEL and modes.get(lane)=='flash' else []
            if backup and backup != active[lane]:
                backups.append(backup)
            text_fallbacks[role]=backups
    if pinned:
        from bili_companion.cognition.flash_duty import FLASH_LOW_MODEL
        preferred_review=(pinned, FLASH_LOW_MODEL)
        preferred_qq=preferred_qq or preferred_review
        pinned_profile=read(Path(pinned))
        review_route=dict(primary=dict(model=FLASH_LOW_MODEL, provider=urlsplit(pinned_profile['base_url']).hostname,
                                       verification='verified', reasoning_effort='low'),
                          alternate=None, standby=None, active_slot='primary')
        for role in ('fast', 'reasoning'):
            text_models[role]=active['review' if role=='fast' else 'qq']
            text_fallbacks[role]=[]
            standby.pop(role,None)
        if active['qq'] != FLASH_LOW_MODEL:
            text_fallbacks['reasoning']=[FLASH_LOW_MODEL]
            standby['reasoning']=FLASH_LOW_MODEL
    elif configured_review:
        review_route=dict(primary=None,alternate=None,standby=None,active_slot=None)
        for slot, preferred in (('primary',preferred_review),('alternate',preferred_review_haiku)):
            if not preferred:
                continue
            selected=read(Path(config[REVIEW_PROFILES[slot]]))
            model=preferred[1]
            verification=selected['verification'].get(model, {})
            review_route[slot]=dict(model=model,provider=urlsplit(selected['base_url']).hostname,
                                   verification=verification.get('status','verified'))
            if active.get('review')==model:
                review_route['active_slot']=slot
        try:
            settings=review_standby_settings(config)
        except CognitionUnavailable:
            pass
        else:
            review_route['standby']=dict(model=settings['model'],provider=urlsplit(settings['base_url']).hostname)
            if review_route['active_slot'] is None:
                review_route['active_slot']='standby'
        text_models.pop('fast',None)
        standby.pop('fast',None)
        chosen=review_route['active_slot']
        if chosen:
            slots=('primary','alternate','standby')
            text_models['fast']=review_route[chosen]['model']
            backups=[review_route[slot]['model'] for slot in slots[slots.index(chosen)+1:] if review_route[slot]]
            text_fallbacks['fast']=backups
            if backups:
                standby['fast']=backups[0]
    video_models={}
    if has_video_profiles(config):
        from bili_companion.cognition.video_duty import VideoDuty
        from bili_companion.runtime.storage import Database
        database=Database(Path(config['state_dir'])/'watcher.sqlite3')
        for purpose,field in VIDEO_PROFILES.items():
            if not config.get(field):
                continue
            selected=read(Path(config[field]))
            model=selected['observer_model']
            provider=urlsplit(selected['base_url']).hostname
            fallback_model,fallback_provider=selection_backup if purpose=='video-selection' else (primary,host)
            on_standby=VideoDuty.standby_for(database,purpose,config[field])
            video_models[purpose]=dict(preferred_model=model,preferred_provider=provider,
                fallback_model=fallback_model,fallback_provider=fallback_provider,on_standby=on_standby)
            if purpose=='video-observe':
                if claude_video is not None:
                    video_models[purpose]['claude_standby']=claude_video
                if not on_standby:
                    standby['video']=fallback_model
                    primary,host=model,provider
                    media_mode=selected['media_mode']
                video_models[purpose]['active_media_mode']=media_mode
        native_model = config.get('video_native_vertex_model')
        if native_model:
            on_standby = VideoDuty.standby_for(database, NATIVE_VIDEO_ROUTE, native_model)
            route_status = video_models.setdefault('video-observe', {})
            relay_first = (config.get('video_hydrogen_profile') and
                           hydrogen_mp4(route_status.get('preferred_model'),
                                        read(Path(config['video_hydrogen_profile']))['media_mode']))
            if relay_first:
                if not on_standby:
                    route_status.update(fallback_model=native_model, fallback_provider='vertex')
                    if route_status['on_standby']:
                        standby['video']=primary
                        primary,host,media_mode=native_model,'vertex','native_video'
                    else:
                        standby['video']=native_model
            else:
                route_status.update(preferred_model=native_model, preferred_provider='vertex',
                    fallback_model=primary, fallback_provider=host, on_standby=on_standby)
                if not on_standby:
                    standby['video'] = primary
                    primary, host, media_mode = native_model, 'vertex', 'native_video'
            route_status['active_media_mode'] = media_mode
    history=PRIVATE/'last.json'
    vertex_backup=(route.get('fallback_enabled',True) if isinstance(route,dict) else bool(config.get('vertex_model')))
    return dict(primary=primary,standby_models=standby,text_fallbacks=text_fallbacks,
                preferred_text_model=preferred_qq[1] if preferred_qq else None,
                preferred_review_model=(preferred_review or preferred_review_haiku or (None,None))[1],
                review_route=review_route,
                preferred_text_reasoning_effort='low' if preferred_qq else None,video_models=video_models,
                host=host,monthly=config['monthly_usd'],daily=config['daily_usd'],
                budget_scope=config.get('budget_scope'),
                backup=bool(config.get('video_hydrogen_profile') or vertex_backup),vertex_backup=vertex_backup,
                text_models=text_models,
                revision=revision(config),can_restore=history.is_file() and matches_history(config,read(history)),
                running=service().get('SubState')=='running')


def restart():
    subprocess.run(['systemctl','--user','restart',UNIT],capture_output=True,check=True,timeout=150)
    previous=None
    for _ in range(12):
        state=service()
        pid=state.get('MainPID')
        if state.get('SubState')=='running' and pid not in (None,'0') and pid==previous:return
        previous=pid if state.get('SubState')=='running' else None
        time.sleep(1)
    raise CognitionUnavailable('watcher_restart_failed')


def replace_route(expected, route, flash_profile=None, qq_hydrogen_profile=None):
    config=read(CONFIG)
    if revision(config)!=expected:raise CognitionUnavailable('route_changed')
    if route is None:config.pop('hydrogen_route',None)
    else:config['hydrogen_route']=route
    if flash_profile is None:config.pop('flash_duty_profile',None)
    else:config['flash_duty_profile']=flash_profile
    if not independent_duties(config):
        if qq_hydrogen_profile is None:config.pop('qq_hydrogen_profile',None)
        else:config['qq_hydrogen_profile']=qq_hydrogen_profile
    write(CONFIG,json.dumps(config,ensure_ascii=False,indent=2)+'\n')
    return revision(config)


def switch(config, route, remember, *, flash_profile=None, qq_hydrogen_profile=None):
    old=config.get('hydrogen_route')
    old_flash=config.get('flash_duty_profile')
    old_hydrogen=config.get('qq_hydrogen_profile')
    active=replace_route(revision(config),route,flash_profile,qq_hydrogen_profile)
    try:
        restart()
        if remember:
            write(PRIVATE/'last.json',json.dumps(dict(active=active,previous=old,
                previous_flash_profile=old_flash,previous_qq_hydrogen_profile=old_hydrogen)))
    except Exception:
        replace_route(active,old,old_flash,old_hydrogen)
        restart()
        raise CognitionUnavailable('activation_rolled_back') from None


def options(request):
    origin, prefix = _endpoint(request['base_url'],False)
    url=origin+(prefix or '/v1')
    model=request['model']
    if not isinstance(model,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/-]{0,158}',model):
        raise CognitionUnavailable('invalid_model')
    key=request['api_key']
    if not isinstance(key,str) or not 1<=len(key)<=4096 or any(not 33<=ord(c)<=126 for c in key) or (len(key)>=8 and (key in url or key in model)):
        raise CognitionUnavailable('invalid_key')
    incoming,outgoing=amount(request['input_price']),amount(request['output_price'])
    if incoming+outgoing<=0:raise CognitionUnavailable('invalid_price')
    cached=request.get('cached_price')
    cached=None if cached in (None,'') else amount(cached)
    if cached is not None and cached>incoming:raise CognitionUnavailable('invalid_price')
    return url,model,key,incoming,outgoing,cached


def prepare_profile(request, config):
    url,model,key,incoming,outgoing,cached=options(request)
    mode=request.get('media_mode','frames_transcript')
    if mode not in {'frames_transcript','native_video','mp4_image_url'}:raise CognitionUnavailable('invalid_media_mode')
    protocol=request.get('wire_protocol','openai')
    if protocol not in {'openai','anthropic','responses'} or (protocol in {'anthropic','responses'} and mode!='frames_transcript'):
        raise CognitionUnavailable('invalid_media_protocol')
    cap={'responses':'max_output_tokens','anthropic':'max_tokens'}.get(
        protocol,'max_completion_tokens' if mode=='frames_transcript' else 'max_tokens')
    effort='low' if 'terra' in model.lower() else None
    json_mode=True
    by_receipt=request.get('billing')=='provider_usage'
    bound=50000 if mode=='frames_transcript' else 120000
    if not by_receipt and config.get('text_provider_profile'):
        from bili_companion.cognition.companion_text import text_profile
        if text_profile(config)['base_url'].rstrip('/') == url:
            counter=OpenAICompatibleClient(url,api_key=key,input_token_upper_bound=bound)
            by_receipt=counter.usage_snapshot(model) is not None
    budget=video_budget(config,lane='other')
    # Exercise the production serializer, accounting and actual visual + spoken content before changing any routing.
    with tempfile.TemporaryDirectory(prefix='video-switch-') as temp:
        payload,expected=make_clip(Path(temp),3 if mode=='frames_transcript' else 6)
        tried_caps={cap}
        while True:
            client=OpenAICompatibleClient(url,api_key=key,input_token_upper_bound=bound,media_mode=mode,
                output_limit_parameter=cap,reasoning_effort=effort,payload_token_envelope=True,
                cached_input_rate=cached,send_json_mode=json_mode,wire_protocol=protocol,usage_panel_actual_cost=by_receipt)
            engine=CognitionEngine(client,budget,None,observer_model=model,decider_model=model,
                price_resolver=lambda _model,_now:(incoming,outgoing),max_output_tokens=1536,request_usd_limit='.10')
            try:
                result,identity,counts=engine._call(model,payload)
                break
            except CognitionUnavailable as exc:
                # Only retry an explicitly rejected parameter. Lost responses retain their reservation.
                if protocol!='openai' or getattr(exc,'http_status',None) not in (400,422):raise
                fields=set(getattr(exc,'error_features',()))
                other='max_tokens' if cap=='max_completion_tokens' else 'max_completion_tokens'
                if (cap in fields or other in fields) and other not in tried_caps:
                    cap=other;tried_caps.add(cap)
                elif effort and 'reasoning_effort' in fields:effort=None
                elif json_mode and 'response_format' in fields:json_mode=False
                else:raise
    reported=result.get('_provider_response_metadata',{}).get('reported_model')
    if not reported_model_matches(model,reported):raise CognitionUnavailable('different_model_returned')
    try:
        answer=_json_response(result)
    except (ValueError,TypeError,AttributeError):
        raise CognitionUnavailable('invalid_sample_reply') from None
    if not check_answer(answer,expected):raise CognitionUnavailable('sample_content_mismatch')
    proof=dict(video=True,structured_output=True,input_bound=True,output_token_cap=True,usage=True,
               base_url=url,output_limit_parameter=cap,media_mode=mode,max_output_tokens=1536,
               verified_at=time.time(),request_id=identity,reported_model=reported,
               usage_counts=counts,sample={field:answer[field] for field in ('first','last','start_side','end_side','speech')})
    proof['speech_transcript' if mode=='frames_transcript' else 'audio']=True
    folder=PRIVATE/uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    profile=dict(version=1,activated=True,provider='openai_compatible',api_key_file=str(folder/'api_key'),base_url=url,
        observer_model=model,supervisor_model=model,decider_model=model,media_mode=mode,
        input_token_upper_bound=bound,output_limit_parameter=cap,max_output_tokens=1536,reasoning_effort=effort,
        ledger_currency='USD',per_request_usd_limit='.10',single_pass=True,child_roles_enabled=False,
        payload_token_envelope=True,cached_input_rate=str(cached) if cached is not None else None,
        send_json_mode=json_mode,price_source='operator_tariff',wire_protocol=protocol,usage_panel_actual_cost=by_receipt,
        prices={model:dict(input_usd_per_million=str(incoming),output_usd_per_million=str(outgoing),
            billing_currency='USD',source='reference rates for reservation; actual USD from provider usage' if by_receipt else 'operator-confirmed USD tariff',
            input_modalities=['text','image'] if mode=='frames_transcript' else ['text','image','audio','video'],
            source_price=dict(input_per_million=str(incoming),output_per_million=str(outgoing)))},verification={model:proof})
    path=folder/'profile.json'
    try:
        write(folder/'api_key',key)
        write(path,json.dumps(profile))
        load_profile(path)
    except Exception:
        path.unlink(missing_ok=True)
        (folder/'api_key').unlink(missing_ok=True)
        folder.rmdir()
        raise
    return path


def activate(request, config):
    path=prepare_profile(request,config)
    previous=config.get('hydrogen_route') or {}
    route=dict(primary_profile=str(path),primary_model=request['model'],fallback_enabled=previous.get('fallback_enabled',True))
    flash_profile=config.get('flash_duty_profile') if independent_duties(config) else None
    try:switch(config,route,remember=True,flash_profile=flash_profile)
    except Exception:
        if (read(CONFIG).get('hydrogen_route') or {}).get('primary_profile')!=str(path):
            path.unlink()
            (path.parent/'api_key').unlink()
            path.parent.rmdir()
        raise
    return status()


def handle(request):
    if request.get('action')=='status':return status()
    PRIVATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (PRIVATE/'switch.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise CognitionUnavailable('switch_in_progress') from None
        config=read(CONFIG)
        if revision(config)!=request.get('revision'):raise CognitionUnavailable('route_changed')
        if request.get('action')=='activate':return activate(request,config)
        if request.get('action')=='restore':
            history=read(PRIVATE/'last.json')
            if not matches_history(config,history):raise CognitionUnavailable('route_changed')
            switch(config,history['previous'],remember=False,flash_profile=history.get('previous_flash_profile'),
                   qq_hydrogen_profile=history.get('previous_qq_hydrogen_profile'))
            return status()
        raise CognitionUnavailable('unknown_action')


if __name__=='__main__':
    try:
        result={'ok':True,'route':handle(json.loads(sys.stdin.buffer.read(20001)))}
    except BudgetPaused as exc:
        result={'ok':False,'error':str(exc)}
    except CognitionUnavailable as exc:
        result={'ok':False,'error':getattr(exc,'cause_code',exc.code)}
    except Exception:
        result={'ok':False,'error':'switch_unavailable'}
    print(json.dumps(result))
