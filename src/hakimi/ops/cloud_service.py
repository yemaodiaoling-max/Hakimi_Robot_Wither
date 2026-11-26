"""Linux entrypoints reusing the existing lane, coordinator and model tools."""
from hakimi import paths
import argparse
import atexit
import json
import os
import subprocess
from pathlib import Path
import sys
import time

ROOT=paths.root()
RUNTIME=ROOT/'official_test_runtime'
_SESSION=None
_SESSION_SIGNATURE=None
INTERNAL_REVIEW_TIMEOUT=180  # Bounded native tool round-trips; still <=3 minutes.


def environment():
    env=dict(os.environ)
    # The gateway bearer belongs only to the Agy child built by vertex_route.
    # Workers, dsh and helper processes must never inherit it.
    for name in list(env):
        if name.startswith('AGY_LLM_GATEWAY_'):
            env.pop(name,None)
    uid=getattr(os,'getuid',lambda:0)()
    env.update(TZ='America/New_York',PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',
               XDG_RUNTIME_DIR=f'/run/user/{uid}',
               DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{uid}/bus',
               AGY_CLI_DISABLE_AUTO_UPDATE='true',HAKIMI_OFFICIAL_ONLY='1')
    env['PATH']=f'{paths.venv_bin()}:{paths.dsh_bin()}:{paths.codex_executable().parent}:'+str(Path.home()/'.local/bin')+':'+env['PATH']
    return env


def log(**value):
    print(json.dumps(dict(at=time.time(),**value)),flush=True)


def _close_session(status='closed'):
    global _SESSION,_SESSION_SIGNATURE
    if _SESSION is not None and hasattr(_SESSION,'close'):
        _SESSION.close(status)
    _SESSION=None;_SESSION_SIGNATURE=None


def _failure_reason(result,vertex):
    """Never report the original account's quota when Vertex failed."""
    error=str(result.get('error','')).lower()
    if vertex:
        if 'vertex_monthly_budget_exhausted' in error or 'budget' in error:
            return 'vertex_budget'
        return 'gateway_unavailable'
    return 'auth' if any(w in error for w in ['auth','login','sign in']) else (
        'quota' if any(w in error for w in ['quota','credit','exhaust','resource_exhausted']) else 'model_error')


def generate(prompt,coordinator,claim):
    """Reuse a bounded conversation per QQ session; one native process stays live."""
    global _SESSION,_SESSION_SIGNATURE
    from hakimi.models import vertex_route
    route=vertex_route.load(RUNTIME)
    from hakimi.models import antigravity_tasks
    routing = claim.get('task_model_routing')
    if not routing or 'model' not in routing:
        routing = antigravity_tasks.routing(ROOT, claim.get('payload', {}), route)
        if routing:
            claim = dict(claim, task_model_routing=routing)
    if routing and routing.get('provider') == 'antigravity':
        return antigravity_tasks.run_chat(prompt, coordinator, claim, RUNTIME,
                                          environment(), INTERNAL_REVIEW_TIMEOUT)
    from hakimi.models.scoped_session import ScopedSession
    if route.enabled:
        vertex_route.enforce_primary(RUNTIME,route=route)
        selected_model = routing['model']
        args=[str(Path.home()/'.local/bin/agy'),'--input-format','stream-json',
              '--output-format','stream-json','--model',selected_model,'--effort','low',
              '--disable-slash-commands','--print-timeout','420s']
        env=route.client_environment(environment())
        env['AGY_LLM_GATEWAY_MODELS']=selected_model
        from hakimi.models.argon_session import ArgonSession
        factory=ArgonSession
        if selected_model=='gemini-4-carbon':
            from hakimi.models.carbon_session import CarbonSession
            factory=CarbonSession
        state_dir=ROOT/('state/gemini_vertex_direct_3_5_lite_v1' if route.model=='gemini-3.5-flash-lite' else 'state/gemini_vertex_direct_v1')
        signature=('vertex_direct',str(route.gateway_directory),selected_model,str(state_dir))
    else:
        model=os.environ.get('HAKIMI_GEMINI_MODEL','gemini-3.8-flash-low')
        args=[str(Path.home()/'.local/bin/agy'),'--input-format','stream-json',
              '--output-format','stream-json','--model',model,
              '--disable-slash-commands','--print-timeout','420s']
        env=environment();state_dir=ROOT/'state/gemini_sessions_v2'
        signature=('legacy',model)
        factory=None
    review=isinstance(claim.get('payload'),dict) and isinstance(claim['payload'].get('internal_review'),dict)
    if review:
        # Reflection uses only the supplied bounded evidence, never a resumed
        # group's accumulated native history. The same trusted Vertex route and
        # actual Gemini tool executor still apply; ordinary chat stays intact.
        from hakimi.models.agy_session import AgySession
        reflection=(factory or AgySession)(args,RUNTIME,env,
            state_path=ROOT/'state/gemini_reflection/last.json',resume=False)
        try:
            return reflection.generate(prompt,coordinator,claim,timeout=INTERNAL_REVIEW_TIMEOUT)
        finally:
            reflection.close('reflection_finished')
    # An externally injected session (older tests provide one) is reused only
    # in legacy mode.  A real Vertex switch never reuses an unchecked session.
    if (_SESSION is None
            or (route.enabled and _SESSION_SIGNATURE!=signature)
            or (not route.enabled and _SESSION_SIGNATURE is not None
                and _SESSION_SIGNATURE!=signature)):
        _close_session('route_changed')
        _SESSION=ScopedSession(args,RUNTIME,env,state_dir,**({'factory':factory} if factory else {}))
        _SESSION_SIGNATURE=signature
        atexit.register(_SESSION.close)
    boundary='这是当前QQ会话的新一轮事件：使用本轮令牌。自动上下文为本群精简；需要别群记忆可用memory.search --scope all_groups，再memory.read核对来源。\n'
    return _SESSION.generate(prompt if route.enabled else boundary+prompt,coordinator,claim)


def _finish_review(coordinator,claim,base,*,success):
    """Release a reflection honestly and return its completion status.

    A native SUCCESS is not a completed review.  While the token is active the
    turn only counts when a real memory.social receipt exists; otherwise the
    model may simply have ended without the required reply.skip.  A real skip
    already completed the token and cleared its pending review, so never
    overwrite that result.  The success argument remains for call compatibility
    with the transport-failure branches.
    """
    review_id = ""
    payload = claim.get("payload") if isinstance(claim, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get("internal_review"), dict):
        review_id = str(payload["internal_review"].get("review_id", ""))
    written = False
    try:
        from hakimi.memory.review_state import _load
        pending = _load(base).get("pending") or {}
        written = bool(review_id and pending.get("review_id") == review_id
                       and pending.get("written"))
    except Exception:
        written = False
    if not coordinator.check_token(claim['token'],'gemini'):
        completion = 'already_completed'
    elif written:
        completion = 'written'
        coordinator.complete(claim['token'],'written')
    else:
        completion = 'review_failed'
        coordinator.complete(claim['token'],'review_failed')
    try:
        from hakimi.memory.personal_review import reconcile
        reconcile(base,time.time())
    except Exception:
        pass
    return completion


_REVIEW_IDLE_CHECK_AT = 0.0

def _idle_review_tick(base):
    """Cheap idle wake-up; the scheduler still owns all model/evidence quotas."""
    global _REVIEW_IDLE_CHECK_AT
    now=time.monotonic()
    if now<_REVIEW_IDLE_CHECK_AT:
        return None
    _REVIEW_IDLE_CHECK_AT=now+15
    try:
        from hakimi.memory.personal_review import reconcile,tick
        reconcile(base)
        return tick(base)
    except Exception as exc:
        log(component='gemini_review_schedule',error=type(exc).__name__)
        return None


def _agent_lane():
    # Every module imported below acts on the bot lane folder (formerly: its code copy).
    os.environ['HAKIMI_BASE']=str(RUNTIME)
    from hakimi.models.fallback_supervisor import coordinator_for
    from hakimi.prompt.prompt_context import activate_context,render_prompt
    from hakimi.core.runtime_io import read_json_cached
    from hakimi.models import vertex_route
    command=f'{sys.executable} {RUNTIME}/qq_cli.py'
    consecutive_failures = 0
    while True:
        health=read_json_cached(RUNTIME/'state/official_worker.json',{})
        if not health.get('online') or time.time()-health.get('at',0)>15:
            time.sleep(2);continue
        coordinator=coordinator_for(RUNTIME,read_json_cached(RUNTIME/'config.json',{}))
        if os.environ.get('HAKIMI_TURN_LANE'):
            hint = coordinator._poll_hint()
            if hint.get('status') != 'queued' and not coordinator._needs_recovery(hint):
                time.sleep(1);continue
        route=vertex_route.load(RUNTIME)
        vertex=route.enabled
        from hakimi.models import antigravity_tasks
        options = antigravity_tasks.settings(ROOT)
        hint = coordinator._poll_hint() if options else {}
        selection = (antigravity_tasks.routing(ROOT, hint['payload'], route)
                     if hint.get('status') == 'queued' else None)
        claim_target = None
        if selection and selection.get('provider') == 'antigravity':
            from hakimi.models.provider_health import availability
            control = availability(RUNTIME)
            if control.get('user_paused') or control.get('mode') == 'dsh':
                time.sleep(1);continue
            # Bind the budget-independent claim to the exact classified event.
            claim_target = hint['trigger_id']
        else:
            try:
                status = route.check()
            except Exception:
                status = {'blocked': vertex}
            if vertex and status.get('blocked'):
                if (options.get('reflection') and not os.environ.get('HAKIMI_TURN_LANE')
                        and not coordinator.any_lane_active()):
                    _idle_review_tick(RUNTIME)
                time.sleep(1);continue
        from hakimi.models.model_trial import tick as model_trial_tick
        main_idle = not os.environ.get('HAKIMI_TURN_LANE') and not coordinator.any_lane_active()
        if main_idle:
            model_trial_tick(RUNTIME)
        claim=coordinator.claim_primary(claim_target) if claim_target else coordinator.claim_primary()
        if not claim:
            if main_idle:
                _idle_review_tick(RUNTIME)
            time.sleep(1);continue
        backoff=0
        routing=None
        review=isinstance(claim.get('payload'),dict) and isinstance(claim['payload'].get('internal_review'),dict)
        try:
            routing = antigravity_tasks.routing(ROOT, claim.get('payload', {}), route)
            selected_model = routing['model'] if routing else None
            if routing:
                claim = dict(claim, task_model_routing=routing)
            from hakimi.models.search_routing import prepare as prepare_search
            claim['payload']['_web_context'] = prepare_search(claim['payload'],RUNTIME,timeout=min(12,max(.1,claim['deadline']-time.time()-10)))
            prompt=render_prompt(claim['payload'],RUNTIME,claim,cli_label=command,wait_after_turn=False,native=bool(routing),selected_model=selected_model)
            if not review and coordinator.was_superseded(claim['trigger_id']):
                continue
            activate_context(claim['payload'],RUNTIME,claim=claim)
            result=generate(prompt,coordinator,claim)
            if not review and coordinator.was_superseded(claim['trigger_id']):
                continue
            reason=_failure_reason(result,vertex)
            if review:
                # A reflection is silent background work.  Never leave it as
                # primary_active or poison fallback/provider state; release the
                # turn immediately and never apply a 60s failure backoff.
                completion=_finish_review(coordinator,claim,RUNTIME,
                                          success=result.get('status')=='SUCCESS')
                log(component='gemini_review',native_status=result.get('status','ERROR'),
                    completion=completion,
                    reason=reason if result.get('status')!='SUCCESS' else None,
                    conversation_id=result.get('conversation_id'),session_turn=result.get('num_turns'),
                    usage=result.get('usage',{}))
            else:
                if coordinator.check_token(claim['token'],'gemini'):
                    coordinator.report_failure(claim['token'],reason)
                    _close_session('submission_missing')
                log(component='gemini',status=result.get('status','ERROR'),reason=reason if result.get('status')!='SUCCESS' else None,
                    conversation_id=result.get('conversation_id'),session_turn=result.get('num_turns'),usage=result.get('usage',{}))
                if result.get('status')!='SUCCESS':backoff=60
                else:consecutive_failures = 0
        except vertex_route.VertexRouteError as exc:
            if not review and coordinator.was_superseded(claim['trigger_id']):
                continue
            reason='vertex_budget' if exc.code=='vertex_budget' else 'gateway_unavailable'
            if review:
                completion=_finish_review(coordinator,claim,RUNTIME,success=False)
                log(component='gemini_review',error=reason,completion=completion,usage=getattr(exc,'usage',{}))
            else:
                if coordinator.check_token(claim['token'],'gemini'):
                    coordinator.report_failure(claim['token'],reason)
                _close_session('submission_missing')
                log(component='gemini',error=reason,usage=getattr(exc,'usage',{}))
                backoff=60 if reason=='gateway_unavailable' else 10
        except Exception as exc:
            if not review and coordinator.was_superseded(claim['trigger_id']):
                continue
            detail = getattr(exc, 'carbon_failure', {})
            reason = ('antigravity_unavailable' if (routing or {}).get('provider') == 'antigravity'
                      else detail.get('code') or ('gateway_unavailable' if vertex else 'model_error'))
            if review:
                completion=_finish_review(coordinator,claim,RUNTIME,success=False)
                log(component='gemini_review',error=type(exc).__name__,completion=completion,usage=getattr(exc,'usage',{}))
            else:
                coordinator.report_failure(claim['token'],reason)
                _close_session('submission_missing')
                log(component='gemini',error=type(exc).__name__,failure=detail,usage=getattr(exc,'usage',{}))
                backoff=60
        if backoff and not coordinator.was_superseded(claim['trigger_id']):
            consecutive_failures += 1
            # One transient failure should not block the next user's turn for a minute.
            # Persistent failures still reach the existing 60-second retry ceiling.
            backoff = min(backoff, 5 * 2**min(consecutive_failures-1, 4))
            log(component='gemini_backoff', seconds=backoff, failures=consecutive_failures)
            time.sleep(backoff)


def agent():
    """Keep each turn lane in its own persistent interpreter/native session."""
    if os.environ.get('HAKIMI_AGENT_LANE_CHILD') == '1':
        import signal
        def stop_lane(signum, frame):
            _close_session('service_stopped')
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop_lane)
        signal.signal(signal.SIGINT, stop_lane)
        return _agent_lane()
    from hakimi.models.fallback_supervisor import coordinator_for
    from hakimi.core.runtime_io import read_json_cached
    children = {}
    stopping = False

    def stop_children():
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 10
        for child in children.values():
            try:
                child.wait(timeout=max(0.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    import signal
    def stop(signum, frame):
        stop_children()
        raise SystemExit(0)

    atexit.register(stop_children)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            coordinator = coordinator_for(RUNTIME, read_json_cached(RUNTIME/'config.json', {}))
            for lane in coordinator.lane_ids():
                child = children.get(lane)
                if child is not None and child.poll() is None:
                    continue
                if child is not None:
                    log(component='gemini_lane_exit', lane=lane or 'main', returncode=child.returncode)
                env = environment()
                env.update(HAKIMI_ROOT=str(ROOT), HAKIMI_BASE=str(RUNTIME),
                           HAKIMI_TURN_LANE=lane, HAKIMI_AGENT_LANE_CHILD='1')
                children[lane] = subprocess.Popen(
                    [sys.executable, '-m', 'hakimi.ops.cloud_service', 'agent'], env=env)
                log(component='gemini_lane_start', lane=lane or 'main', pid=children[lane].pid)
            time.sleep(2)
    finally:
        stop_children()


def worker():
    from hakimi.core.host_secrets import crypt
    data=json.loads(crypt((Path.home()/'.config/hakimi/credentials.fernet').read_bytes(),False))
    env=environment()
    env.update(HAKIMI_OFFICIAL_APPSECRET=data['app_secret'],
        DEEPSEEK_API_KEY=data['provider']['api_key'],DEEPSEEK_BASE_URL=data['provider']['base_url'])
    os.chdir(RUNTIME)
    env.update(HAKIMI_ROOT=str(ROOT),HAKIMI_BASE=str(RUNTIME))
    os.execve(sys.executable,[sys.executable,'-m','hakimi.transport.official_lane'],env)


def diary_worker():
    os.chdir(ROOT)
    os.execve(sys.executable,[sys.executable,'-m','hakimi.features.diary_daily'],dict(environment(),HAKIMI_ROOT=str(ROOT),HAKIMI_BASE=str(ROOT)))


def help_worker():
    os.chdir(ROOT)
    os.execve(sys.executable,[sys.executable,'-m','hakimi.features.codex_help_service','serve'],dict(environment(),HAKIMI_ROOT=str(ROOT),HAKIMI_BASE=str(ROOT)))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('service',choices=['worker','agent','help','diary'])
    args=parser.parse_args()
    os.umask(0o077)
    {'worker':worker,'agent':agent,'help':help_worker,'diary':diary_worker}[args.service]()
