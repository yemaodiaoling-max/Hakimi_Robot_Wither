"""One scheduled writing pass; image generation belongs to the on-duty Terra automation."""
from hakimi import persona
from hakimi import paths
from datetime import datetime
import json,os,re,tempfile,time
from pathlib import Path
from hakimi.features.diary_store import ZONE,folder,read_entry,save,entries,load

ROOT=paths.root()

class Progress:
    def record_progress(self,token):pass

def parse(raw):
    if not isinstance(raw,str):raise ValueError('invalid_diary')
    raw=raw.strip()
    if raw.startswith('```'):raw=raw.split('\n',1)[1].rsplit('```',1)[0]
    data=json.loads(raw)
    if not isinstance(data,dict) or not all(isinstance(data.get(k),str) and data[k].strip() for k in ('title','text','image_prompt')):
        raise ValueError('invalid_diary')
    if len(data['title'])>80 or len(data['text'])>6000:raise ValueError('invalid_diary_length')
    # Keep the requested one/two-sentence image brief; never truncate the diary itself.
    brief=' '.join(data['image_prompt'].split())
    data['image_prompt']=''.join(re.split(r'(?<=[。！？!?])',brief)[:2])[:300]
    return data

def daily(now=None):
    now=now or datetime.now(ZONE);day=now.date().isoformat()
    if (folder(ROOT)/day/'entry.json').exists():return {'status':'already_written','day':day}
    if not (now.hour==22 and now.minute>=30 or now.hour==23 and now.minute<20):return {'status':'outside_window'}
    import fcntl
    root=folder(ROOT);root.mkdir(parents=True,exist_ok=True)
    with (root/'write.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'writing'}
        if (root/day/'entry.json').exists():return {'status':'already_written','day':day}
        return generate(day)

def diary_material(day):
    from hakimi.features.diary_material import material
    return material(ROOT,day)

def recent_diary_contrasts(day):
    """Prompt-only excerpts for repetition awareness; archived originals stay intact."""
    return [{'day':r['day'],'title':r['title'],'excerpt':r['text'][:90]+' … '+r['text'][-70:]}
            for r in entries(ROOT,until=day)[-7:] if r['day']<day]

def generate(day):
    from hakimi.ops.cloud_service import environment
    from hakimi.models.provider_health import availability
    from hakimi.models.agy_session import AgySession
    samples=[{'day':r['day'],'text':r['text']} for r in entries(ROOT)[-3:]]
    if not samples:
        from hakimi.core.jsonio import tail_jsonl
        samples=[{'text':r.get('text','')} for r in tail_jsonl(ROOT/'state/qzone_posts.jsonl',count=3)]
    style=paths.persona('style_guide.md',ROOT).read_text(encoding='utf-8').split('## 工具与数据边界')[0][:4200]
    prompt=(persona.text("diary_writer.txt")+day+'\n人设：'+style+'\n既往日记：\n'+json.dumps(samples,ensure_ascii=False)[:5000])
    prompt += '\n近七篇首尾摘录（仅用于检查主题重复，不是完整原文）：\n' + json.dumps(recent_diary_contrasts(day), ensure_ascii=False)
    prompt += '\n以下是有时间与原文出处的真实材料，优先用材料里的昵称、外号或熟悉称呼（避免动辄写“一个人”“另一个人”），区分当日与往日回想；勿写QQ号或真实姓名等身份隐私，勿编造事件。无适合材料时明确写猫系想象。参考最近日记，避免重复主题、意象与套话：\n' + json.dumps(diary_material(day), ensure_ascii=False)
    health=availability(ROOT);dsh=health.get('mode')=='dsh' or health.get('fast') and health.get('mode')!='gemini'
    provider='gemini';raw=None
    from hakimi.models import antigravity_tasks
    model = antigravity_tasks.settings(ROOT).get('diary')
    if model and health.get('mode') != 'dsh' and not health.get('user_paused'):
        try:
            raw = antigravity_tasks.diary_text(model, prompt, 105)
            parse(raw)
            provider = 'antigravity/' + model
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            from hakimi.ops.cloud_service import log
            log(component="diary_primary", provider="antigravity", model=model, error=type(exc).__name__)
            raw = None
    elif not model and not dsh:
        folder(ROOT).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='diary_write_',dir=folder(ROOT)) as work:
            args=[str(Path.home()/'.local/bin/agy'),'--input-format','stream-json','--output-format','stream-json',
                '--model',os.environ.get('HAKIMI_GEMINI_MODEL','gemini-3.8-flash-low'),'--disable-slash-commands','--print-timeout','90s']
            session=AgySession(args,work,environment(),timeout=105)
            try:
                from hakimi.models.vertex_route import load,enforce_primary
                route=load(ROOT)
                if route.enabled:
                    enforce_primary(ROOT,route=route)
                    from hakimi.models.vertex_session import VertexSession
                    direct=VertexSession([],ROOT,route.client_environment(environment()))
                    value=direct._request({'contents':[{'role':'user','parts':[{'text':prompt}]}],
                        'generationConfig':{'thinkingConfig':{'thinkingLevel':'LOW'},'responseMimeType':'application/json'}},90)
                    parts=(value.get('candidates') or [{}])[0].get('content',{}).get('parts',[])
                    result={'status':'SUCCESS','response':''.join(item.get('text','') for item in parts if not item.get('thought'))}
                else:result=session.generate(prompt,Progress(),{'token':'diary-writing'})
                if result.get('status')=='SUCCESS':
                    raw=result.get('response');parse(raw)
            except (OSError,RuntimeError,TimeoutError,ValueError):raw=None
            finally:session.close()
    if raw is None:
        from hakimi.models import aux_config
        if not aux_config.diary_fallback_enabled(ROOT):
            raise RuntimeError('diary_primary_unavailable')
        fb_cfg=aux_config.fallback_config(ROOT)
        diary_prompt='本轮写日记，把所需JSON序列化放入标准reply的text字符串。\n'+prompt
        if fb_cfg.get('backend')=='http_api':
            from hakimi.models.fallback_http import run_http_fallback
            provider=fb_cfg.get('model') or 'http_fallback'
            result=run_http_fallback(diary_prompt,ROOT/'official_test_runtime',timeout_seconds=90,cfg=fb_cfg)
        else:
            from hakimi.models.fallback_runner import run_fallback
            provider='dsh'
            result=run_fallback(diary_prompt,ROOT/'official_test_runtime',timeout_seconds=90)
        raw=result['text']
    data=parse(raw)
    row=save(ROOT,day,data['title'],data['text'],data['image_prompt'],provider)
    (folder(ROOT)/'last_error.json').unlink(missing_ok=True)
    return {'status':row['status'],'day':day,'provider':provider}

if __name__=='__main__':
    os.umask(0o077)
    try:print(json.dumps(daily(),ensure_ascii=False))
    except Exception as exc:
        from hakimi.features.diary_store import write
        write(ROOT/'state/diary/last_error.json',{'at':time.time(),'type':type(exc).__name__})
        raise SystemExit('Diary writing failed: '+type(exc).__name__)
