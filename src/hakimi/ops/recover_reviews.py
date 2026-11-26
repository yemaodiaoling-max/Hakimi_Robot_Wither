"""Explicit operator recovery of failed reviews; plan is read-only by default."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from hakimi.memory.review_recovery import (ensure_metadata,load_metadata,complete_metadata,
                                           eligible_event)


def _connect(base,readonly=False):
    path=Path(base)/'state/memory.sqlite3'
    db=sqlite3.connect('file:'+str(path)+('?mode=ro' if readonly else '?mode=rw'),uri=True)
    db.row_factory=sqlite3.Row
    return db


def _journal(base,name):
    path=Path(base)/'state'/name
    if not path.exists():return []
    rows=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        try:row=json.loads(line)
        except ValueError:continue
        if isinstance(row,dict):rows.append(row)
    return rows


def _original_time(db,kind,job,fallback,event_id=None):
    source='window' if kind=='window' else kind
    at=db.execute('SELECT MIN(at) FROM relationship_review_audit WHERE source=? AND job=?',
                  (source,str(job))).fetchone()[0]
    if at is None and event_id is not None:
        at=db.execute('SELECT MIN(at) FROM relationship_review_audit WHERE event_id=?',(event_id,)).fetchone()[0]
    return (at,'first_matching_audit') if at is not None else (fallback,'original_job_time')


def _window_context(db,window,outbox,sent):
    matches=[]
    for out in outbox:
        if out.get('session_key')!=window['session']:continue
        for i,segment in enumerate(out.get('segments',[])):
            receipt=str(out.get('reply_id',''))+':'+str(i)
            if hashlib.sha256((window['session']+':'+receipt).encode()).hexdigest()[:24]==window['id']:
                matches.append((out,i))
    if len(matches)!=1:return None
    first=db.execute("SELECT MIN(at) FROM relationship_review_audit WHERE source='window' AND job=?",(window['id'],)).fetchone()[0]
    following=db.execute('SELECT MIN(created_at) FROM reply_review_windows WHERE session=? AND created_at>?',
                         (window['session'],window['created_at'])).fetchone()[0]
    ends=[at for at in (first,following) if at is not None]
    if not ends:return None
    end=min(ends)
    confirmed=[s for s in sent if s.get('session_key')==window['session']
               and window['created_at']-.05<=s.get('at',0)<end]
    replies=[];humans=[];used=set()
    for s in sorted(confirmed,key=lambda r:r['at']):
        candidates=[]
        for out in outbox:
            if out.get('session_key')!=window['session'] or out.get('trigger_id')!=s.get('trigger_id'):continue
            for i,segment in enumerate(out.get('segments',[])):
                receipt=str(out['reply_id'])+':'+str(i)
                if isinstance(segment,str) and segment==s.get('text') and receipt not in used:
                    candidates.append((out,i,receipt))
        if not candidates:
            owners=[out for out in outbox if out.get('session_key')==window['session'] and out.get('trigger_id')==s.get('trigger_id')]
            all_sent=sorted([row for row in sent if row.get('session_key')==window['session'] and row.get('trigger_id')==s.get('trigger_id')],key=lambda row:row['at'])
            if len(owners)==1 and len(all_sent)==len(owners[0].get('segments',[])):
                # Transport can remove CQ mention markup; confirmed send order
                # binds the receipt while actual text still comes only from sent.
                index=next((i for i,row in enumerate(all_sent) if row is s),None)
                if index is not None:
                    out=owners[0];receipt=str(out['reply_id'])+':'+str(index)
                    if receipt not in used:candidates=[(out,index,receipt)]
        if len(candidates)!=1:
            # Repeated equal bubbles of one reply are resolved by segment order.
            if not candidates or len({c[0]['reply_id'] for c in candidates})!=1:continue
            candidates.sort(key=lambda c:c[1])
        out,i,receipt=candidates[0];used.add(receipt)
        replies.append({'receipt':receipt,'message_id':str(s.get('message_id','')),
                        'speaker':'SELF','at':s['at'],'text':str(s.get('text',''))[:240],
                        'reply_to':[str(out['reply_target_id'])] if out.get('reply_target_id') else []})
        target=out.get('reply_target_id')
        if target:
            row=db.execute("SELECT id FROM records WHERE kind='chat' AND session=? AND origin=? AND state='active' AND text!='' ORDER BY at DESC LIMIT 1",
                           (window['session'],str(target))).fetchone()
            if row and row['id'] not in humans:humans.append(row['id'])
    first_receipt=matches[0][0]['reply_id']+':'+str(matches[0][1])
    if first_receipt not in used or not humans:return None
    return {'human_ids':humans,'replies':replies[-8:]}


def plan_recovery(base):
    """Inspect existing failures without creating tables or changing statuses."""
    from hakimi.curation.impression_events import _packet
    outbox=_journal(base,'outbox.jsonl');sent=_journal(base,'sent_messages.jsonl')
    items=[]
    with _connect(base,True) as db:
        for raw in db.execute("SELECT * FROM reply_review_windows WHERE status='error' ORDER BY created_at"):
            row=dict(raw);at,basis=_original_time(db,'window',row['id'],row['created_at'])
            context=_window_context(db,row,outbox,sent)
            target=''
            if context:
                first=db.execute('SELECT user_id FROM records WHERE id=?',(context['human_ids'][0],)).fetchone()
                target=str(first[0]) if first else ''
                context['human_ids']=[eid for eid in context['human_ids'] if eligible_event(db,eid,'window',row['id'])]
            action='pending' if context and context['human_ids'] else 'already_reviewed' if context else 'source_unavailable'
            items.append(dict(kind='window',job=row['id'],session=row['session'],target=target,
                original_review_at=at,time_basis=basis,status=action,
                reason='已恢复实际发送回复及原始提问，等待复盘' if action=='pending' else '原事件已结算，本次仅补记零变化' if action=='already_reviewed' else '原始来源不足，本次补记未计分',
                context=context))
        for raw in db.execute("SELECT * FROM impression_jobs WHERE status='error' ORDER BY original_id"):
            row=dict(raw);at,basis=_original_time(db,'note',row['original_id'],row['noted_at'])
            try:packet=_packet(db,row)
            except (ValueError,KeyError,TypeError):packet=None
            available=bool(packet and any(eligible_event(db,eid,'note',row['original_id']) for eid in packet['event_choices']))
            status='pending' if available else 'already_reviewed' if packet else 'source_unavailable'
            items.append(dict(kind='note',job=str(row['original_id']),session=row['session'],target=row['target'],
                original_review_at=at,time_basis=basis,status=status,
                reason='原始笔记和事件证据仍在，等待复盘' if available else '原事件已结算，本次仅补记零变化' if packet else '原文证据已清理，本次补记未计分'))
        for raw in db.execute("SELECT j.*,r.session,r.user_id,r.text,r.state FROM daily_event_jobs j LEFT JOIN records r ON r.id=j.source_id WHERE j.status='error' ORDER BY j.created_at,j.source_id"):
            row=dict(raw);at,basis=_original_time(db,'daily',row['source_id'],row['created_at'],row['source_id'])
            available=bool(row['text'] and row['state']=='active')
            status='pending' if available and eligible_event(db,row['source_id'],'daily',row['source_id']) else 'already_reviewed' if available else 'source_unavailable'
            items.append(dict(kind='daily',job=str(row['source_id']),session=row['session'] or '',target=row['user_id'] or '',
                original_review_at=at,time_basis=basis,status=status,
                reason='原始日常消息仍在，等待复盘' if status=='pending' else '原事件已结算，本次仅补记零变化' if status=='already_reviewed' else '原文证据已清理，本次补记未计分'))
    return sorted(items,key=lambda r:(r['original_review_at'],r['kind'],r['job']))


def prepare_recovery(base,plan=None,now=None,ready_at=None):
    """Explicit mutation: register original times and restore only failed jobs."""
    now=time.time() if now is None else now
    ready_at=now+60 if ready_at is None else ready_at
    plan=plan_recovery(base) if plan is None else plan
    prepared=[]
    with _connect(base) as db:
        db.execute('BEGIN IMMEDIATE');ensure_metadata(db)
        for item in plan:
            kind,job=item['kind'],str(item['job'])
            if load_metadata(db,kind,job):continue
            table,key=('reply_review_windows','id') if kind=='window' else ('impression_jobs','original_id') if kind=='note' else ('daily_event_jobs','source_id')
            current=db.execute('SELECT status FROM '+table+' WHERE '+key+'=?',(job,)).fetchone()
            if not current or current[0]!='error':continue
            status=item['status']
            db.execute('INSERT INTO review_recovery_jobs(kind,job,session,target,original_review_at,time_basis,status,reason,prepared_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (kind,job,item['session'],item['target'],item['original_review_at'],item['time_basis'],status,item['reason'],now,None if status=='pending' else now))
            if status=='pending':
                if kind=='window':
                    context=item['context']
                    humans=[eid for eid in context['human_ids'] if eligible_event(db,eid,kind,job)]
                    if not humans:
                        complete_metadata(db,kind,job,now,'already_reviewed','原事件已结算，本次仅补记零变化')
                        db.execute("UPDATE reply_review_windows SET status='done',error_code='' WHERE id=?",(job,));continue
                    db.execute("UPDATE reply_review_windows SET status='ready',attempts=0,next_attempt=?,human_ids=?,replies=?,error_code='' WHERE id=?",(ready_at,json.dumps([str(db.execute('SELECT origin FROM records WHERE id=?',(eid,)).fetchone()[0]) for eid in humans]),json.dumps(context['replies'],ensure_ascii=False),job))
                elif kind=='note':
                    db.execute("UPDATE impression_jobs SET status='pending',attempts=0,next_attempt=?,reason='' WHERE original_id=?",(ready_at,job))
                else:
                    # Original admission/review time is preserved above. This is
                    # the renewed retry's bounded grace period, not history time.
                    db.execute("UPDATE daily_event_jobs SET status='pending',attempts=0,next_attempt=?,created_at=? WHERE source_id=?",(ready_at,now,job))
            else:
                db.execute('UPDATE '+table+" SET status='done' WHERE "+key+'=?',(job,))
                if kind=='note':db.execute('UPDATE impression_jobs SET reason=? WHERE original_id=?',(item['reason'],job))
                if kind=='window':db.execute("UPDATE reply_review_windows SET error_code='' WHERE id=?",(job,))
            if status!='pending':complete_metadata(db,kind,job,now,status,item['reason'])
            prepared.append({'kind':kind,'job':job,'status':status})
    return prepared


def main():
    parser=argparse.ArgumentParser();parser.add_argument('base');parser.add_argument('action',choices=['plan','prepare'],nargs='?',default='plan')
    args=parser.parse_args()
    if args.action=='prepare':result=prepare_recovery(args.base)
    else:
        result=[{k:v for k,v in item.items() if k!='context'} for item in plan_recovery(args.base)]
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
