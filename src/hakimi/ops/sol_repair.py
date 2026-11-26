"""On-demand Sol code repair, reused by the existing five-minute supervisor tick."""
from hakimi import persona
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import hashlib,json,os,shutil,sqlite3,subprocess,sys,time
from hakimi import paths
from hakimi.features.codex_help_runner import run_codex_structured

MODEL='gpt-6.1-sol'
ALLOWED=('official_transport.py','official_routes.py','official_api.py','official_base.py','official_files.py','official_lane.py','delivery_guard.py',
         'cloud_service.py','vertex_session.py','scoped_session.py','provider_health.py')
SCHEMA={'type':'object','additionalProperties':False,'properties':{
    'status':{'type':'string','enum':['patch','report']},'summary':{'type':'string','maxLength':500},
    'edits':{'type':'array','maxItems':4,'items':{'type':'object','additionalProperties':False,'properties':{
        'path':{'type':'string','enum':list(ALLOWED)},'before':{'type':'string','maxLength':6000},
        'after':{'type':'string','maxLength':6000}},'required':['path','before','after']}}},'required':['status','summary','edits']}

def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {} if default is None else default

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def src(name):
    """Live source of an ALLOWED module (historical flat name -> package path)."""
    path=paths.source_file(name)
    if path is None:raise ValueError('repair_edit_scope')
    return path

def checkout():
    root=paths.checkout_root()
    if root is None:raise ValueError('repair_requires_source_checkout')
    return root

def report(root):
    value=read(Path(root)/'state/sol_repair.json');result=value.get('last_result') or {}
    return {'status':result.get('status','idle'),'summary':str(result.get('summary',''))[:500],
            'calls_today':value.get('calls',0),'model':MODEL}

def changed_sources(root,edits):
    changed={}
    for edit in edits:
        name=edit['path']
        if name not in ALLOWED or not edit['before']:raise ValueError('repair_edit_scope')
        text=changed.get(name,src(name).read_text(encoding='utf-8'))
        if text.count(edit['before'])!=1:raise ValueError('repair_source_mismatch')
        changed[name]=text.replace(edit['before'],edit['after'],1)
    if not changed or all(text==src(name).read_text(encoding='utf-8') for name,text in changed.items()):raise ValueError('repair_empty_patch')
    for name,text in changed.items():compile(text,name,'exec')
    return changed

def stage_patch(root,job,changed):
    """Copy the source checkout, apply the edits, run the fixed regression subset."""
    base=checkout();stage=job/'stage';stage.mkdir()
    for name in ('src','tests'):
        shutil.copytree(base/name,stage/name,ignore=shutil.ignore_patterns('__pycache__','tmp*','*.egg-info'))
    shutil.copy2(base/'pyproject.toml',stage/'pyproject.toml')
    for name,text in changed.items():(stage/src(name).relative_to(base)).write_text(text,encoding='utf-8')
    suites=('test_image_send_recovery','test_official_adapter','test_official_private','test_file_delivery',
            'test_network_delivery_recovery','test_delivery_guard','test_primary_progress','test_provider_health',
            'test_cache_prefix','test_genshin_version','test_group_reply_overrides','test_flash_lite_trial')
    suites=[n for n in suites if (stage/'tests'/(n+'.py')).exists()]
    if not suites:raise ValueError('repair_tests_missing')
    # run_tests applies the known-stale registry, so outdated expectations do not block a fix.
    result=subprocess.run([sys.executable,'-B',str(stage/'tests/run_tests.py'),*suites],cwd=stage,
        env=dict(os.environ,PYTHONPATH=str(stage/'src')+':'+str(stage/'tests'),PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True,text=True,timeout=120)
    (job/'validation.txt').write_text(result.stdout+result.stderr)
    if result.returncode:raise ValueError('repair_tests_failed')
    return stage

def busy(root):
    from hakimi.chat.turn_lanes import LANES
    state_dir=root/'official_test_runtime/state'
    turns=[read(state_dir/('turn_lanes/'+lane+'.json' if lane else 'turn_state.json')) for lane in LANES]
    delivery=read(root/'official_test_runtime/state/delivery_state.json')
    now=time.time();active=any(turn.get('status') in {'queued','primary_active','fallback_active'} and turn.get('expires_at',now+1)>now for turn in turns)
    attempts=delivery.get('attempts') or [];last=(attempts[-1].get('at',now) if attempts else now)
    return active or bool((delivery.get('pending') or {}).get('inflight') and now-last<240)

def activate(root,pending,check):
    if busy(root):return {'status':'deferred','summary':'Sol修复已通过测试，等待当前回复结束后上线。'}
    if any(digest(src(name))!=value for name,value in pending['hashes'].items()):raise ValueError('repair_concurrent_change')
    job=Path(pending['job']);base=checkout();backup=job/'backup';backup.mkdir(exist_ok=True)
    targets=[src(name) for name in pending['names']]
    for i,target in enumerate(targets):shutil.copy2(target,backup/str(i))
    (backup/'files.json').write_text(json.dumps({str(p):str(backup/str(i)) for i,p in enumerate(targets)}))
    stopped=False
    try:
        subprocess.run(['systemctl','--user','stop','hakimi-agent','hakimi-worker'],check=True,timeout=30);stopped=True
        if busy(root):return {'status':'deferred','summary':'等待新一轮回复结束后上线Sol修复。'}
        for target in targets:shutil.copy2(job/'stage'/target.relative_to(base),target)
        subprocess.run(['systemctl','--user','start','hakimi-worker','hakimi-agent'],check=True,timeout=30)
        for _ in range(10):
            time.sleep(1)
            if not check():break
        else:raise ValueError('repair_health_not_restored')
    except BaseException:
        if stopped:subprocess.run(['systemctl','--user','stop','hakimi-agent','hakimi-worker'],check=False,timeout=30)
        for i,target in enumerate(targets):shutil.copy2(backup/str(i),target)
        raise
    finally:
        if stopped:subprocess.run(['systemctl','--user','start','hakimi-worker','hakimi-agent'],check=False,timeout=30)
    try:
        subprocess.run(['git','-C',str(base),'commit','-q','-m','Sol repair: '+', '.join(pending['names']),'--',*map(str,targets)],
                       check=False,timeout=30,capture_output=True)
    except (OSError,subprocess.TimeoutExpired):pass
    return {'status':'applied','summary':'Sol已完成最小代码修复，回归与重启检查通过。','files':pending['names']}

def run_cycle(root,faults,evidence,now,*,budget,check,runner=None):
    root=Path(root);path=root/'state/sol_repair.json';state=read(path)
    if not faults:return {'status':'healthy','summary':'当前无需Sol修复。','model_called':False}
    error={}
    try:
        with sqlite3.connect('file:'+str(root/'official_test_runtime/state/official_routes.sqlite3')+'?mode=ro',uri=True) as db:
            row=db.execute("SELECT value FROM settings WHERE key='last_send_error'").fetchone()
        if row:error={k:v for k,v in json.loads(row[0]).items() if k in {'code','posting','ambiguous'}}
    except (OSError,ValueError,sqlite3.Error):pass
    sig=hashlib.sha256(json.dumps([sorted(faults),error],sort_keys=True).encode()).hexdigest()[:20]
    day=datetime.fromtimestamp(now,ZoneInfo('America/New_York')).date().isoformat()
    if state.get('day')!=day:state.update(day=day,calls=0)
    if state.get('signature')!=sig:state.update(signature=sig,seen_at=now,pending=None)
    def finish(result):
        state['last_result']=dict(result,at=now);write(path,state);return result
    pending=state.get('pending')
    if pending:
        try:result=activate(root,pending,check)
        except Exception as exc:state['pending']=None;return finish({'status':'failed','summary':str(exc)[:120],'model_called':False})
        if result['status']=='applied':state['pending']=None
        return finish(dict(result,model_called=False))
    if now-state.get('seen_at',now)<240:return finish({'status':'observing','summary':'持续故障已登记，下次巡查确认后启动Sol。','model_called':False})
    if state.get('calls',0)>=2 or state.get('last_attempt') and now-state['last_attempt']<1800:
        return finish({'status':'cooldown','summary':'同类故障已合并，等待修复额度或冷却窗口。','model_called':False})
    from hakimi.ops.terra_budget import BudgetExhausted
    try:receipt=budget.reserve('health',now,estimate=32000)
    except BudgetExhausted:return finish({'status':'budget','summary':'Sol自动修复已达到当前共用额度。','model_called':False})
    state.update(calls=state.get('calls',0)+1,last_attempt=now);write(path,state)
    wanted=['official_transport.py','official_files.py','delivery_guard.py'] if any('delivery' in f for f in faults) else ['cloud_service.py','vertex_session.py','official_lane.py']
    wanted=[frame['file'] for frame in evidence.get('frames',[]) if frame.get('file') in ALLOWED]+wanted
    sources={}
    for name in dict.fromkeys(wanted):
        if name in ALLOWED and src(name).is_file():
            text=src(name).read_text(encoding='utf-8')
            if sum(map(len,sources.values()))+len(text)<=50000:sources[name]=text
        if len(sources)>=3:break
    prompt=(persona.text("ops/sol_repair.txt")+
            json.dumps({'faults':faults,'send_error':error,'diagnostic':evidence,'sources':sources},ensure_ascii=False))
    job=root/'state/sol_repair_jobs'/(str(int(now))+'-'+sig);job.mkdir(parents=True,exist_ok=True)
    called=True
    try:
        def _default_runner(p, r, schema, *, model=MODEL, timeout=90, max_prompt_chars=80000):
            from hakimi.models import aux_config
            if aux_config.has_config(r) and aux_config.ops_config(r).get('backend') in ('main_gateway', 'custom_api'):
                from hakimi.models import gateway_text
                return gateway_text.complete_structured_ops(r, p[:max_prompt_chars], schema, model=model, timeout=timeout)
            return run_codex_structured(p, r, schema, model=model, timeout=timeout, max_prompt_chars=max_prompt_chars)
        result,usage=(runner or _default_runner)(prompt,root,SCHEMA,model=MODEL,timeout=90,max_prompt_chars=80000)
        budget.settle(receipt,usage,success=True,now=now)
    except Exception as exc:
        budget.settle(receipt,None,success=False,now=now)
        return finish({'status':'failed','summary':'Sol调用失败：'+type(exc).__name__,'model_called':True})
    write(job/'proposal.json',result)
    if result['status']!='patch':return finish({'status':'report','summary':result['summary'][:500],'model_called':True})
    try:
        changed=changed_sources(root,result['edits'])
        if any(name not in sources for name in changed):raise ValueError('repair_source_not_provided')
        hashes={name:digest(src(name)) for name in sources}
        stage_patch(root,job,changed)
        pending={'job':str(job),'names':list(changed),'hashes':hashes};state['pending']=pending;write(path,state)
        outcome=activate(root,pending,check)
        if outcome['status']=='applied':state['pending']=None
        return finish(dict(outcome,model_called=True))
    except Exception as exc:
        state['pending']=None
        return finish({'status':'failed','summary':'Sol候选修复未通过：'+str(exc)[:100],'model_called':True})
