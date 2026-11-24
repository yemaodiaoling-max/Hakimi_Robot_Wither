"""Small durable review explanations, independent of live scores and raw expiry."""
import json


DECISIONS = {
    'source_unavailable': '原始事件正文缺失，未能重新评定，本次未计分。',
    'recovery_already_settled': '该事件此前已经结算，本次不重复计分。',
    'already_settled': '该事件此前已经结算，本次不重复计分。',
    'note_priority': '该事件已由人物笔记结算，本次不重复计分。',
    'note_already_reviewed_or_older': '同一笔记或较新的笔记已处理，本次不重复计分。',
    'episode_duplicate': '同一用户在同一群的短时同方向互动已计分，本轮不重复计分。',
    'missing_event_evidence_keep_prior': '没有匹配到事件原文依据，保留此前结果。',
    'missing_event_evidence': '没有匹配到事件原文依据，本次未计分。',
    'unconfirmed_addressee_keep_prior': '未确认这次互动是对哈基米说的，未新增计分。',
    'fact_only': '本次只记录事实，没有计入关系分数。',
    'positive_reaction_unconfirmed': '玩笑、撒娇或角色互动未核验到哈基米积极开心的回应或对应笔记，本次未加分。',
    'engaged_reaction_unconfirmed': '闲聊未核验到哈基米积极或投入的回应，本次未增加亲近值。',
    'confiding_reaction_unconfirmed': '倾诉或求助未核验到哈基米积极或投入的回应，本次未增加关系分数。',
    'negative_reaction_mitigated': '调侃或催促因哈基米实际接梗或平淡接话化解，本次未扣减好感。',
    'positive_event_rejected': '正向互动因哈基米明确拒绝或不悦，本次未加分。',
    'score_curve_no_movement': '事件符合计分方向，但经现有分数曲线和边界计算后，两项实际变化均为 0。',
    'event_not_counted': '该事件类型在现有规则下不产生关系分数变化。',
}


def decision_details(job,item,outcome,reason):
    """Record program decisions after normalization, including explicit repair provenance."""
    code=reason if reason in DECISIONS else ''
    if outcome=='duplicate' and not code:code='episode_duplicate'
    if outcome=='no_change' and not code:
        if item.get('kind')=='fact':code='fact_only'
        elif not item.get('host_evidence_verified'):code='missing_event_evidence'
        elif item.get('event_type') in {'banter','roleplay','affection'} and (not item.get('host_reaction_verified') or item.get('reaction_type')!='positive'):
            code='positive_reaction_unconfirmed'
        elif item.get('event_type') in {'ordinary_chat','routine_question','personal_fact'} and (not item.get('host_reaction_verified') or item.get('reaction_type') not in {'positive','engaged'}):
            code='engaged_reaction_unconfirmed'
        elif item.get('event_type')=='confiding_or_seeking_help' and (not item.get('host_reaction_verified') or item.get('reaction_type') not in {'positive','engaged'}):
            code='confiding_reaction_unconfirmed'
        elif item.get('event_type') in {'teasing_or_bossing','dismissive_or_nitpick','pestering_or_spam'} and item.get('host_reaction_verified'):
            code='negative_reaction_mitigated'
        elif item.get('host_reaction_verified') and item.get('reaction_type') in {'annoyed','negative','rejected'}:
            code='positive_event_rejected'
        else:code='event_not_counted'
    result={'decision_code':code,'event_type':item.get('event_type','unknown'),
            'evidence':str(item.get('evidence',''))[:240],
            'reaction_type':item.get('reaction_type','unclear'),
            'reaction_verified':bool(item.get('host_reaction_verified')),
            'reaction_evidence':str(item.get('reaction_evidence',''))[:240],
            'reviewer':job.get('reviewer','dsh')}
    for key in ('review_model', 'review_reasoning_effort', 'review_reasoning_override_verified',
                'review_reported_model', 'review_effort_echo', 'review_usage', 'review_account',
                'review_request_id', 'review_billing_state', 'review_usage_status'):
        if key in job:result[key]=job[key]
    if job.get('previous_decision'):result['previous_decision']=str(job['previous_decision'])[:240]
    if job.get('correction_note'):result['correction_note']=str(job['correction_note'])[:240]
    if job.get('reviewed_at'):result['reviewed_at']=job['reviewed_at']
    return result


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json(value):
    try:return json.loads(value or '{}')
    except (TypeError, ValueError):return {}


def _event(db, event_id, tables):
    if event_id is None or 'records' not in tables:return None
    row=db.execute('SELECT id,user_id,session,text,at FROM records WHERE id=?',(event_id,)).fetchone()
    return dict(row) if row else None


def _fallback_event(db, metadata, tables):
    if metadata['kind']=='daily':
        return int(metadata['job']) if str(metadata['job']).isdigit() else None
    if metadata['kind']=='note' and 'impression_jobs' in tables:
        row=db.execute('SELECT event_id FROM impression_jobs WHERE original_id=?',(metadata['job'],)).fetchone()
        return row[0] if row else None
    return None


def build_result(db, metadata):
    """Read existing decisions; never classify or apply a score here."""
    tables=_tables(db);audits=[]
    if 'relationship_review_audit' in tables and metadata['status']=='done':
        # Append order, not wall-clock order: recovery deliberately backdates at.
        latest={}
        for row in db.execute("SELECT * FROM relationship_review_audit WHERE source=? AND job=? AND at=? AND outcome!='error' ORDER BY id",
                              (metadata['kind'],metadata['job'],metadata['original_review_at'])):
            latest[row['event_id']]=dict(row)
        audits=list(latest.values())[-12:]
    if not audits:
        fallback=_fallback_event(db,metadata,tables)
        audits=[{'event_id':fallback,'outcome':metadata['status'],'reason':'','details':'{}'}]
    events=[]
    for audit in audits:
        event_id=audit.get('event_id');source=_event(db,event_id,tables)
        outcome=audit['outcome'];details=_json(audit.get('details'))
        code=details.get('decision_code') or (audit.get('reason') if audit.get('reason') in DECISIONS else '')
        if outcome=='duplicate' and not code:code='episode_duplicate'
        explanation=DECISIONS.get(code,'')
        if not explanation:
            explanation={
                'not_selected':'本轮没有选中这条消息作为关系计分事件，未加分。',
                'no_change':'本次判定未加分；旧记录未保存具体哪项计分条件未通过。',
                'other_addressee':'未确认互动对象是哈基米，未新增计分。',
                'already_reviewed':'该事件此前已经结算，本次不重复计分。',
                'source_unavailable':DECISIONS['source_unavailable'],
                'skip':'该条事件未通过来源或格式核对，本次未计分。',
            }.get(outcome,'具体计分结果未留存，不能从当前累计分数推算。')
        description=str(audit.get('reason') or '')
        if description in DECISIONS or description=='no representative relationship event selected':description=''
        if not description:
            description='原消息未选作计分事件' if outcome=='not_selected' else '已结算事件' if outcome=='already_reviewed' else '原始事件待核对'
        change=None
        if event_id is not None and 'relationship_change_log' in tables:
            row=db.execute('SELECT * FROM relationship_change_log WHERE event_id=? AND source=? AND at=? AND session=? AND target=? ORDER BY id DESC LIMIT 1',
                (event_id,metadata['kind'],metadata['original_review_at'],metadata['session'],source['user_id'] if source else metadata['target'])).fetchone()
            if row:change=dict(row)
        if outcome in {'applied','revised'}:
            delta={field:change[field+'_delta'] if change else None for field in ('affinity','closeness')}
            if change:
                explanation='实际好感 {:+d}、亲近 {:+d}，已由程序结算；展示记录不会再次计分。'.format(delta['affinity'],delta['closeness'])
                if outcome=='revised' and details.get('settled_effect'):
                    settled=details['settled_effect']
                    explanation='该事件最终计分：好感 {:+d}、亲近 {:+d}；本次修正补差：好感 {:+d}、亲近 {:+d}。'.format(settled['affinity'],settled['closeness'],delta['affinity'],delta['closeness'])
                if details.get('correction_note'):explanation+=' '+details['correction_note']
        elif outcome in {'no_change','duplicate','not_selected','other_addressee','skip','already_reviewed','source_unavailable'}:
            delta={'affinity':0,'closeness':0}
        else:delta={'affinity':None,'closeness':None}
        events.append({'event_id':event_id,'author':source['user_id'] if source else metadata['target'],
            'source_at':source['at'] if source else None,'excerpt':source['text'][:240] if source else '',
            'description':description[:160],'outcome':outcome,'decision_reason':explanation,
            'decision_code':code,'details':details,'deltas':delta,
            'scores':{key:change[key] for key in ('affinity_before','affinity_after','closeness_before','closeness_after')} if change else None})
    delta={field:sum(e['deltas'][field] for e in events) if all(e['deltas'][field] is not None for e in events) else None for field in ('affinity','closeness')}
    assessed=metadata['status']!='source_unavailable'
    status='unknown' if any(v is None for v in delta.values()) else 'unassessed' if not assessed else 'changed' if any(delta.values()) else 'unchanged'
    labels={'changed':'已计分','unchanged':'本次未加分','unassessed':'原文缺失，未评定','unknown':'具体结果未留存'}
    manual=any(e['details'].get('reviewer')=='operator_context_review' for e in events)
    label='已修正旧计分' if manual and any(v is not None and v<0 for v in delta.values()) else labels[status]
    return {'version':1,'status':status,'label':('依据聊天补判 · '+label) if manual else label,'deltas':delta,'events':events,
            'reason':events[0]['decision_reason'] if len(events)==1 else '；'.join(e['decision_reason'] for e in events)[:240]}


def snapshot_result(db,kind,job):
    row=db.execute('SELECT * FROM review_recovery_jobs WHERE kind=? AND job=?',(kind,str(job))).fetchone()
    if row is None or row['status']=='pending':return None
    result=build_result(db,dict(row))
    previous=_json(dict(row).get('result'))
    if result['status']=='unknown' and previous.get('version'):return previous
    db.execute('UPDATE review_recovery_jobs SET result=? WHERE kind=? AND job=?',
               (json.dumps(result,ensure_ascii=False,separators=(',',':')),kind,str(job)))
    return result


def refresh_results(db):
    from hakimi.memory.review_recovery import ensure_metadata
    ensure_metadata(db)
    rows=db.execute("SELECT kind,job FROM review_recovery_jobs WHERE status!='pending'").fetchall()
    for row in rows:snapshot_result(db,row['kind'],row['job'])
    return len(rows)
