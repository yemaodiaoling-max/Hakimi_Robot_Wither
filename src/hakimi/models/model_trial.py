"""A bounded 24-hour model trial, reviewed by the existing Terra cycle."""
import hashlib,json,statistics,time
from hakimi.models.model_trial_state import read, root, state, write  # noqa: F401 (re-export)

LITE='gemini-3.5-flash-lite';BASELINE='gemini-3.8-flash'
AXES=('persona','naturalness','factuality','tools','length')
COMPARISON_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 'verdict':{'type':'string','enum':['continue','restore','collecting']},
 'summary':{'type':'string','maxLength':500},
 'scores':{'type':'object','additionalProperties':False,'properties':{axis:{'type':'object','additionalProperties':False,'properties':{'baseline':{'type':['integer','null'],'minimum':1,'maximum':5},'trial':{'type':['integer','null'],'minimum':1,'maximum':5},'evidence_count':{'type':'integer','minimum':0}},'required':['baseline','trial','evidence_count']} for axis in AXES},'required':list(AXES)},
 'recommendations':{'type':'array','maxItems':3,'items':{'type':'string','maxLength':250}}},'required':['verdict','summary','scores','recommendations']}


def start(base,now=None,hours=24):
    now=time.time() if now is None else now;r=root(base)
    current=state(base)
    if current.get('phase')=='active':return current
    route=read(r/'state/vertex_route.json')
    if not route.get('enabled') or route.get('model')!=BASELINE:raise ValueError('trial_start_route_mismatch')
    identifier='flash-lite-'+str(int(now))
    trial={'id':identifier,'phase':'active','baseline_model':BASELINE,'model':LITE,'started_at':now,'ends_at':now+hours*3600,
           'prompt_revision':'positive-v1','baseline_file':'state/flash_lite_trial/baseline_3_8.jsonl','hours':hours}
    write(r/'state/model_trial.json',trial)
    route['model']=LITE;write(r/'state/vertex_route.json',route)
    (r/'state/gemini_style_feedback.md').write_text('',encoding='utf-8')
    return trial

def delivered(base,trial):
    from hakimi.core.jsonio import tail_jsonl
    folder=root(base)/'official_test_runtime/state'
    outbox=tail_jsonl(folder/'outbox.jsonl',count=5000,max_bytes=4*1024*1024)
    models={row['trigger_id']:row.get('model') for row in outbox if row.get('trigger_id')}
    rows=tail_jsonl(folder/'sent_messages.jsonl',count=5000,max_bytes=4*1024*1024)
    return [dict(row,model=models.get(row.get('trigger_id'))) for row in rows if row.get('provider')=='gemini' and row.get('text','').strip()
            and trial['started_at']<=row.get('at',0)<=trial['ends_at'] and models.get(row.get('trigger_id'))==LITE]

def packet(base,now=None):
    now=time.time() if now is None else now;t=state(base)
    if t.get('phase') not in {'active','completed'} or t.get('phase')=='completed' and not t.get('final_pending'):return None
    rows=delivered(base,t)
    if not rows and now<t['started_at']+3600 and not t.get('final_pending'):return None
    signature=hashlib.sha256(','.join(str(row.get('message_id')) for row in rows[-8:]).encode()).hexdigest()
    if signature==t.get('review_signature') and not t.get('final_pending'):return None
    from hakimi.core.jsonio import tail_jsonl
    baseline=tail_jsonl(root(base)/'state/flash_lite_trial/baseline_3_8.jsonl',count=200,max_bytes=262144)
    chosen=rows[-4:];sessions={row['session_key'] for row in chosen}
    old=[row for row in baseline if row['session_key'] in sessions][-4:] or baseline[-4:]
    def compact(row):return {'message_id':str(row.get('message_id')),'session':row.get('session_key'),'text':row['text'][:650]}
    def stats(items):
        lengths=[len(row['text']) for row in items]
        return {'observed_messages':len(items),'sessions':len({row['session_key'] for row in items}),'median_chars':statistics.median(lengths) if lengths else 0}
    return {'trial_id':t['id'],'model':LITE,'baseline_model':BASELINE,'final':bool(t.get('final_pending')),'signature':signature,
      'current':[compact(row) for row in chosen],'baseline':[compact(row) for row in old],
      'statistics':{'baseline':stats(baseline),'trial':stats(rows)},
      'test_conditions':'本轮同时采用正向任务说明；模型与提示词表达共同构成本轮运行条件。'}

def save_review(base,packet,comparison,now):
    if not isinstance(comparison,dict) or comparison.get('verdict') not in {'continue','restore','collecting'}:raise ValueError('trial_comparison_invalid')
    from hakimi.prompt.prompt_language import instructions
    t=state(base);summary=str(comparison.get('summary',''))[:500]
    recommendations=[instructions(value)[:250] for value in comparison.get('recommendations',[])[:3]]
    result={'at':now,'trial_id':t['id'],'model':LITE,'baseline_model':BASELINE,'final':packet['final'],'statistics':packet['statistics'],
            'verdict':comparison['verdict'],'summary':summary,'scores':comparison.get('scores',{}),'recommendations':recommendations}
    folder=root(base)/'state/flash_lite_trial';folder.mkdir(exist_ok=True)
    with (folder/'reviews.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(result,ensure_ascii=False)+'\n')
    t.update(review_signature=packet['signature'],last_review=result,final_pending=False if packet['final'] else t.get('final_pending',False))
    if packet['final']:t['final_review_at']=now
    write(root(base)/'state/model_trial.json',t)
    style_recs=[r for r in recommendations if r and not any(w in r for w in ('扩大试用样本','记录各回复','程序回执','收集同群'))]
    if t['phase']=='active' and style_recs:(root(base)/'state/gemini_style_feedback.md').write_text('\n'.join(style_recs),encoding='utf-8')
    write(folder/'latest_review.json',result)
    return result

def tick(base,now=None):
    now=time.time() if now is None else now;t=state(base)
    if t.get('phase')!='active' or now<t['ends_at']:return None
    from hakimi.bridge.turn_coordinator import TurnCoordinator
    runtime=root(base)/'official_test_runtime'
    if TurnCoordinator(runtime).snapshot().get('status') in {'primary_active','fallback_active'}:return {'status':'finishing_active_turn'}
    path=root(base)/'state/vertex_route.json';route=read(path)
    if route.get('model')==LITE:route['model']=BASELINE;write(path,route)
    t.update(phase='completed',restored_at=now,final_pending=True);write(root(base)/'state/model_trial.json',t)
    (root(base)/'state/gemini_style_feedback.md').write_text('',encoding='utf-8')
    return {'status':'completed','model':BASELINE}

def notice(base):
    t=state(base);review=t.get('last_review') or {}
    if t.get('phase')=='completed':
        summary='3.5 Flash-Lite 的24小时试运行已结束，现已恢复3.8 Flash。'
        if review:summary+='Terra评价：'+str(review.get('summary',''))[:280]
        return {'notice_id':'trial-completed-'+t['id']+str(bool(t.get('final_review_at'))),'summary':summary,'phase':'completed'}
    if review:return {'notice_id':'trial-review-'+t['id']+'-'+str(int(review['at'])),'summary':'3.5 Flash-Lite试运行：'+review['summary'][:400],'phase':'reviewed'}
    return {}
