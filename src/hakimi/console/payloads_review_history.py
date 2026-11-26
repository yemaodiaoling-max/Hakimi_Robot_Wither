"""Paged score history with bounded source excerpts for recovered review results."""
import json
import math
import re
import time

from hakimi.console.errors import ApiError
from hakimi.console.console_db import SESSIONS, _name_map, _page, _paginate, db_conn


def _timestamp(values,key):
    value=values.get(key,'')
    if value in ('',None):
        return None
    try:
        result=float(value)
    except (TypeError,ValueError):
        raise ApiError(400,'invalid_time','时间筛选无效')
    if not math.isfinite(result) or not 0<=result<=1e12:
        raise ApiError(400,'invalid_time','时间筛选无效')
    return result


_REVIEW_ERRORS = {
    'REVIEW_INVALID_RELATIONSHIP_SOURCE': '事件编号不匹配',
    'REVIEW_INVALID_RELATIONSHIP_EVENT': '事件分类格式待重试',
    'REVIEW_QUEUE_FULL': '等待处理较多',
    'REVIEW_FAILED': '本次处理未完成',
    'REVIEW_TIMEOUT': '处理超时',
    'REVIEW_ERROR': '处理失败，等待重试',
}


def _review_health(db, now):
    """Global metadata only, independent of every history/person filter."""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('relationship_review_audit','reply_review_windows')")}
    summary={'audit_total':0,'no_change_or_unscored':0,'errors':0,
             'last_attempt_at':None,'pending_windows':0,'unresolved_jobs':0,'recovered_jobs':0,'source_unavailable_jobs':0,'pending_recovery_jobs':0}
    codes=[]
    if 'relationship_review_audit' in tables:
        row=db.execute("""SELECT COUNT(*),
            COALESCE(SUM(CASE WHEN outcome NOT IN ('applied','revised','error') AND COALESCE(error_code,'')='' THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN outcome='error' OR COALESCE(error_code,'')!='' THEN 1 ELSE 0 END),0)
            FROM relationship_review_audit WHERE at>=? AND at<=?""",(now-3600,now)).fetchone()
        summary.update(audit_total=row[0],no_change_or_unscored=row[1],errors=row[2])
        summary['last_attempt_at']=db.execute('SELECT MAX(at) FROM relationship_review_audit WHERE at<=?',(now,)).fetchone()[0]
        counts={}
        for code,count in db.execute("SELECT error_code,COUNT(*) FROM relationship_review_audit WHERE at>=? AND at<=? AND (outcome='error' OR COALESCE(error_code,'')!='') GROUP BY error_code",(now-3600,now)):
            safe=code if code in _REVIEW_ERRORS else 'REVIEW_ERROR'
            counts[safe]=counts.get(safe,0)+count
        codes=[{'code':code,'label':_REVIEW_ERRORS[code],'count':count} for code,count in sorted(counts.items())]
    if 'reply_review_windows' in tables:
        summary['pending_windows']=db.execute("SELECT COUNT(*) FROM reply_review_windows WHERE status IN ('open','closing','ready','retry','processing')").fetchone()[0]
        last=db.execute('SELECT MAX(updated_at) FROM reply_review_windows WHERE attempts>0 AND updated_at<=?',(now,)).fetchone()[0]
        if last is not None:
            summary['last_attempt_at']=max(summary['last_attempt_at'] or 0,last)
    all_tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('reply_review_windows','impression_jobs','daily_event_jobs'):
        if table in all_tables:
            summary['unresolved_jobs']+=db.execute("SELECT COUNT(*) FROM "+table+" WHERE status='error'").fetchone()[0]
    if 'review_recovery_jobs' in all_tables:
        summary['pending_recovery_jobs']=db.execute("SELECT COUNT(*) FROM review_recovery_jobs WHERE status='pending'").fetchone()[0]
        summary['recovered_jobs']=db.execute("SELECT COUNT(*) FROM review_recovery_jobs WHERE status IN ('done','already_reviewed')").fetchone()[0]
        summary['source_unavailable_jobs']=db.execute("SELECT COUNT(*) FROM review_recovery_jobs WHERE status='source_unavailable'").fetchone()[0]
    return {'scope':'global','window_seconds':3600,'summary':summary,'error_codes':codes}


def review_history_payload(base,values):
    view=str(values.get('view','changes'))
    person=str(values.get('person','')).strip()
    group=str(values.get('group','')).strip()
    source=str(values.get('source','')).strip()
    if view not in {'changes','daily'}:
        raise ApiError(400,'invalid_view','视图筛选无效')
    if person and not re.fullmatch(r'[1-9][0-9]{0,19}',person):
        raise ApiError(400,'invalid_person','人物ID格式无效')
    if group and not SESSIONS.fullmatch(group):
        raise ApiError(400,'invalid_group','群筛选格式无效')
    if source not in {'','note','window','daily','legacy'}:
        raise ApiError(400,'invalid_source','来源筛选无效')
    since=_timestamp(values,'since');until=_timestamp(values,'until')
    if since is not None and until is not None and since>until:
        raise ApiError(400,'invalid_time','开始时间晚于结束时间')
    page,size=_page(values,1,100)
    params=[];where=[]
    if group:
        where.append('session=?');params.append(group)
    else:
        where.append("(session LIKE 'group:%' OR (session='' AND scope='groups'))")
    for field,value in (('target',person),('source',source)):
        if value:
            where.append(field+'=?');params.append(value)
    if since is not None:where.append('at>=?');params.append(since)
    if until is not None:where.append('at<=?');params.append(until)
    with db_conn(base) as db:
        health=_review_health(db,time.time())
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('relationship_change_log','relationship_change_daily','impression_effects','impression_scores','review_recovery_jobs')")}
        available=('relationship_change_daily' if view=='daily' else 'impression_effects') in tables
        rows=[];total=0
        if available:
            query_args=[]
            if view=='daily':
                query='SELECT rowid AS id,*,0 AS legacy FROM relationship_change_daily'
            else:
                detailed="SELECT *,0 AS legacy,0 AS recovery,'' AS time_basis,0 AS recovery_id FROM relationship_change_log" if 'relationship_change_log' in tables else ''
                if detailed and 'review_recovery_jobs' in tables:
                    detailed+=" WHERE NOT EXISTS(SELECT 1 FROM review_recovery_jobs r WHERE r.status!='pending' AND r.kind=relationship_change_log.source AND r.session=relationship_change_log.session AND r.target=relationship_change_log.target AND r.original_review_at=relationship_change_log.at)"
                # A legacy effect lacks actual before/after snapshots: leave them
                # unknown. The durable ledger is never used to invent history.
                legacy="SELECT -e.effect_rowid AS id,e.updated AS at,e.scope,e.target,COALESCE(r.session,CASE WHEN e.scope!='groups' THEN e.scope ELSE '' END) AS session,e.event_id,e.original_id,CASE WHEN e.original_id IS NOT NULL THEN 'note' ELSE 'legacy' END AS source,0 AS revision,NULL AS affinity_before,NULL AS affinity_after,e.affinity_delta,NULL AS closeness_before,NULL AS closeness_after,e.closeness_delta,substr(e.reason,1,160) AS reason,1 AS legacy,0 AS recovery,'' AS time_basis,0 AS recovery_id FROM (SELECT rowid AS effect_rowid,* FROM impression_effects WHERE updated>=? ORDER BY updated DESC LIMIT 1000) e LEFT JOIN records r ON r.id=e.event_id"
                query_args.append(time.time()-30*86400)
                if detailed:
                    legacy+=' WHERE NOT EXISTS(SELECT 1 FROM relationship_change_log l WHERE l.scope=e.scope AND l.target=e.target AND l.event_id=e.event_id)'
                query=detailed+' UNION ALL '+legacy if detailed else legacy
                if 'review_recovery_jobs' in tables:
                    query+=""" UNION ALL SELECT -1000000000-rowid AS id,original_review_at AS at,
                        CASE WHEN session LIKE 'group:%' THEN 'groups' ELSE session END AS scope,
                        target,session,NULL AS event_id,CASE WHEN kind='note' THEN CAST(job AS INTEGER) ELSE NULL END AS original_id,
                        kind AS source,0 AS revision,NULL AS affinity_before,NULL AS affinity_after,0 AS affinity_delta,
                        NULL AS closeness_before,NULL AS closeness_after,0 AS closeness_delta,
                        reason,0 AS legacy,1 AS recovery,time_basis,id AS recovery_id FROM review_recovery_jobs
                        WHERE status IN ('done','already_reviewed','source_unavailable')"""

            scoped='WITH history AS ('+query+') SELECT * FROM history WHERE '+' AND '.join(where)
            params=[*query_args,*params]
            total=db.execute('SELECT COUNT(*) FROM ('+scoped+')',params).fetchone()[0]
            rows=db.execute(scoped+' ORDER BY at DESC,id DESC LIMIT ? OFFSET ?',(*params,size,(page-1)*size)).fetchall()
        rows=[dict(row) for row in rows]
        for row in rows:
            if not row.get('recovery'):continue
            metadata=dict(db.execute('SELECT * FROM review_recovery_jobs WHERE id=?',(row['recovery_id'],)).fetchone())
            try:verdict=json.loads(metadata.get('result') or '{}')
            except (TypeError,ValueError):verdict={}
            if not verdict.get('version'):
                from hakimi.memory.review_results import build_result
                verdict=build_result(db,metadata)
            row['review_result']=verdict
            row['reason']=verdict['reason']
            scored=[e for e in verdict['events'] if e.get('scores')]
            for field in ('affinity','closeness'):
                row[field+'_delta']=verdict['deltas'][field]
                row[field+'_before']=scored[0]['scores'][field+'_before'] if len(verdict['events'])==1 and scored else None
                row[field+'_after']=scored[0]['scores'][field+'_after'] if len(verdict['events'])==1 and scored else None
            if len(verdict['events'])==1:row['event_id']=verdict['events'][0]['event_id']
        names=_name_map(db,[r['target'] for r in rows],group or None)
        scores=[]
        if person and 'impression_scores' in tables:
            scope='groups' if not group or group.startswith('group:') else group
            scores=[dict(r) for r in db.execute('SELECT scope,target,affinity,closeness,confidence FROM impression_scores WHERE scope=? AND target=?',(scope,person))]
    result=[]
    for row in rows:
        item=dict(row)
        item['name']=names.get(item['target'],'用户'+item['target'])[:80]
        item['reason']=str(item.get('reason') or '')[:160]
        item['legacy']=bool(item['legacy'])
        item['revision']=bool(item.get('revision') or item.get('revisions'))
        if view=='daily':
            item['event_id']=None
            item['summary_events']=int(item['events'])
        result.append(item)
    return {'available':available,'view':view,'items':result,'scores':scores,'health':health,
            'retention':{'detail_days':30,'detail_limit':1000,'daily_days':365,'daily_limit':3000},
            **_paginate(total,page,size)}
