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
    settings=config(base)
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
