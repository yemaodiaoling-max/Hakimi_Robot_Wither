"""Pre-search once per user turn, shared across primary and fallback."""
import json
import re
import time

from hakimi.models.search_policy import config,current_text,decide,network_allowed,compact_query
from hakimi.models import web_sources


def prepare(payload,base,*,timeout=12,judge=None):
    try:return _prepare(payload,base,timeout=timeout,judge=judge)
    except Exception as exc:return {'error':'presearch_unavailable','error_type':type(exc).__name__,'network_used':False}


def _prepare(payload,base,*,timeout=12,judge=None):
    text=current_text(payload,base=base)
    if re.search(r'来源|出处|哪儿查|哪里查|参考链接|source|citation',text,re.I):
        trace=web_sources.lookup(base,payload)
        if trace.get('sources'):return {'source_trace':trace,'network_used':False}
    if not network_allowed(payload,base):return {'network_allowed':False,'network_used':False}
    from hakimi.models import aux_config
    settings=config(base)
    aux_search = aux_config.search_routing_config(base) if aux_config.has_config(base) else {}
    if aux_search:
        settings = {**settings, **aux_search}
    if settings.get('enabled') is not True:return None
    session,trigger=web_sources.identity(payload)
    if not session or not trigger:return None
    with web_sources.database(base) as db:
        previous=db.execute('SELECT presearch FROM turns WHERE session=? AND trigger=?',(session,trigger)).fetchone()
        if previous and previous['presearch']:
            value=json.loads(previous['presearch'])
            return None if value.get('pending') else value
        db.execute('INSERT OR IGNORE INTO turns VALUES (?,?,0,?,NULL)',(session,trigger,time.time()+min(60,float(settings.get("total_web_seconds",40)))))
        db.execute('UPDATE turns SET presearch=? WHERE session=? AND trigger=?',(json.dumps({'pending':True}),session,trigger))
    started=time.monotonic()
    if judge is None:
        judge_mode = aux_search.get('judge_mode', 'dsh') if aux_search else 'dsh'
        if judge_mode == 'local_rules':
            judge = None
        elif judge_mode == 'main_gateway':
            def judge(q_text, b, timeout_seconds=3):
                from hakimi.models import gateway_text, search_judge
                task = search_judge.POLICY + json.dumps(q_text[:2000], ensure_ascii=False)
                res = gateway_text.complete_via_gateway(
                    b, task, tier=aux_search.get('model') or 'gemini-4-hydrogen',
                    json_mode=True, max_tokens=128, timeout=timeout_seconds,
                )
                return dict(search_judge.parse(res['text']), model=res['tier'])
        elif judge_mode == 'custom_api':
            def judge(q_text, b, timeout_seconds=3):
                from hakimi.models import gateway_text, search_judge
                task = search_judge.POLICY + json.dumps(q_text[:2000], ensure_ascii=False)
                res = gateway_text.complete_via_http_api(
                    aux_search, [{'role': 'user', 'content': task}],
                    json_mode=True, max_tokens=128, timeout=timeout_seconds,
                )
                return dict(search_judge.parse(res['text']), model=res['model'])
        else:
            from hakimi.models.search_judge import classify
            judge=classify
    decision=decide(payload,base,judge=judge,timeout=min(3,max(.1,timeout)))
    result={'decision':decision,'network_used':False}
    if decision['search'] and timeout-(time.monotonic()-started)>.2:
        from hakimi.models.web_access import run
        query=compact_query(text)
        result['search']=run('web.search',{'query':query},timeout-(time.monotonic()-started),base,payload)
        result['network_used']=bool(result['search'].get('results'))
    with web_sources.database(base) as db:
        db.execute('UPDATE turns SET presearch=? WHERE session=? AND trigger=?',(json.dumps(result,ensure_ascii=False),session,trigger))
    return result
