"""Incremental notebook-to-event judgments; the chat models' prompts stay intact."""
import json,os,re,time
from pathlib import Path

SCHEMA='''CREATE TABLE IF NOT EXISTS impression_jobs(
 original_id INTEGER PRIMARY KEY,record_id INTEGER,session TEXT,target TEXT,noted_at REAL,
 status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,
 event_id INTEGER,verdict TEXT DEFAULT '',reason TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS impression_effects(
 scope TEXT,target TEXT,event_id INTEGER,affinity_delta INTEGER,closeness_delta INTEGER,
 original_id INTEGER,verdict TEXT,reason TEXT,updated REAL,PRIMARY KEY(scope,target,event_id));
 CREATE TABLE IF NOT EXISTS impression_scores(
 scope TEXT,target TEXT,affinity INTEGER,closeness INTEGER,confidence INTEGER,
 PRIMARY KEY(scope,target));'''
CURSOR='impression_events_v1'
LIVE={('gemini','gemini_manual'),('dsh','dsh_assisted')}

def initialize(base):
    """Start with future notebook operations; never rescore historical notes."""
    from hakimi.memory.memory_store import store
    with store(str(Path(base).resolve())).db() as db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO cursors(name,identity,offset) SELECT ?,?,COALESCE(MAX(id),0) FROM memory_originals",
                   (CURSOR,'notebook_events'))

def _scope(session):return 'groups' if session.startswith('group:') else session

def _recent_negative_count(db, scope, target, event_id, now, window_seconds=7 * 86400) -> int:
    row = db.execute(
        "SELECT COUNT(DISTINCT event_id) FROM impression_effects "
        "WHERE scope=? AND target=? AND affinity_delta < 0 AND event_id != ? "
        "AND updated BETWEEN ? AND ?",
        (scope, str(target), int(event_id), now - window_seconds, now - 120.0),
    ).fetchone()
    return int(row[0]) if row else 0

def _own(row):return (row['provider'],row['provenance']) in LIVE and row['subject_id']=='hakimi' and not row['seed_id']

def discover(db,now):
    db.execute("""UPDATE impression_jobs SET status='pending' WHERE status='awaiting_projection'
        AND EXISTS(SELECT 1 FROM social_assessments s WHERE s.original_record_id=impression_jobs.record_id
                   AND s.subject_id='hakimi' AND s.seed_id='')""")
    db.execute("""UPDATE impression_jobs SET status='other_relation' WHERE status='awaiting_projection'
        AND EXISTS(SELECT 1 FROM social_assessments s WHERE s.original_record_id=impression_jobs.record_id
                   AND (s.subject_id!='hakimi' OR s.seed_id!=''))""")
    cursor=db.execute('SELECT offset FROM cursors WHERE name=?',(CURSOR,)).fetchone()[0]
    rows=db.execute('SELECT * FROM memory_originals WHERE id>? AND created_at<=? ORDER BY id LIMIT 100',
                    (cursor,now-20)).fetchall()
    for row in rows:
        if row['kind']=='person' and row['provider'] in {'gemini','dsh'} and not row['legacy']:
            projection=db.execute('SELECT * FROM social_assessments WHERE original_record_id=? ORDER BY id DESC LIMIT 1',
                                  (row['record_id'],)).fetchone()
            # The social writer saves the original before projecting its direction.
            if projection is None or _own(projection):
                status='awaiting_projection' if row['item_key'].startswith('关系印象·') and projection is None else 'pending'
                db.execute('INSERT OR IGNORE INTO impression_jobs(original_id,record_id,session,target,noted_at,status) VALUES (?,?,?,?,?,?)',
                           (row['id'],row['record_id'],row['session'],row['user_id'],row['created_at'],status))
        db.execute('UPDATE cursors SET offset=? WHERE name=?',(row['id'],CURSOR))

def _split(previous,current):
    prefix=len(os.path.commonprefix([previous,current]));a=previous[prefix:];b=current[prefix:]
    suffix=len(os.path.commonprefix([a[::-1],b[::-1]]))
    if suffix:a=a[:-suffix];b=b[:-suffix]
    return a,b

def _difference(previous,current):
    a,b=_split(previous,current)
    return ('删去：'+a[:400]+'\n写入：'+b[:1000]).strip()

def _without(text,part,label):
    """Show a note excerpt once: a span already given in full by note_change becomes a pointer."""
    return text.replace(part,label,1) if len(part)>len(label)+4 and part in text else text

def _clock(value):
    return time.strftime('%m-%d %H:%M',time.localtime(float(value)))

def _packet(db,job):
    from hakimi.social.social_base import _evidence_involves
    from hakimi.social.social_behavior import _evidence_survives
    row=db.execute('SELECT o.*,r.state,r.supersedes_id FROM memory_originals o JOIN records r ON r.id=o.record_id WHERE o.id=?',
                   (job['original_id'],)).fetchone()
    if not row or row['state'] in {'deleted','retracted'}:return None
    meta=json.loads(row['metadata']);previous=''
    prior=db.execute('SELECT text FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1',(row['supersedes_id'],)).fetchone()
    if prior:previous=prior['text']
    evidence={}
    def add(r):
        if r and r['kind']=='chat' and r['session']==job['session'] and r['state'] not in {'deleted','retracted','archived'} and r['text'].strip():
            evidence[r['id']]=r
    for mid in meta.get('related_memory_ids',[])[:8]:
        value=str(mid).removeprefix('M')
        if value.isdigit():add(db.execute('SELECT * FROM records WHERE id=?',(int(value),)).fetchone())
    origin=str(meta.get('origin_message_id',''))
    if origin:add(db.execute("SELECT * FROM records WHERE session=? AND kind='chat' AND origin=? ORDER BY at DESC LIMIT 1",
                             (job['session'],origin)).fetchone())
    preferred=set(evidence)
    recent=db.execute("SELECT * FROM records WHERE session=? AND kind='chat' AND at BETWEEN ? AND ? AND state!='deleted' ORDER BY at DESC LIMIT 80",
                      (job['session'],job['noted_at']-900,job['noted_at'])).fetchall()
    for r in recent:
        if _evidence_involves(r,job['target'],db):add(r)
        if len(evidence)>=8:break
    anchors=[r for r in evidence.values() if _evidence_survives(r,'group' if job['session'].startswith('group:') else 'private',job['session'],job['target'],job['noted_at'])]
    if not anchors:return None
    anchors=sorted(anchors,key=lambda r:(r['id'] in preferred,r['at']),reverse=True)[:4]
    # Include the nearby conversation, but score only an actual target-linked event.
    for r in recent:
        if any(abs(r['at']-a['at'])<=45 for a in anchors):add(r)
        if len(evidence)>=10:break
    rows=sorted({r['id']:r for r in [*sorted(evidence.values(),key=lambda r:r['at'])[-6:],*anchors]}.values(),key=lambda r:r['at'])
    events=[]
    for r in rows:
        try:mentions=json.loads(r['direct_targets'] or '[]')
        except (ValueError,TypeError):mentions=[]
        mentions=mentions if isinstance(mentions,list) else []
        if not r['direct_targets']:mentions=re.findall(r'\[CQ:at,qq=(\d+)\]',r['text'])
        reply=db.execute("SELECT user_id FROM records WHERE kind='chat' AND session=? AND origin=? LIMIT 1",
                         (r['session'],r['reply_to'])).fetchone() if r['reply_to'] else None
        from hakimi.memory.relationship_events import reaction_sources
        reactions = reaction_sources(db, r)
        # The constant self_id is dropped; its occurrences become SELF, the name the review prompt uses.
        me=str(r['self_id'] or '')
        who=lambda value:'SELF' if me and str(value)==me else value
        event={'id':r['id'],'at':_clock(r['at']),'author':who(r['user_id']),'by_target':str(r['user_id'])==job['target'],
               'mentions':[who(v) for v in mentions],'reply_to':r['reply_to'],'reply_author':who(reply[0]) if reply else '',
               'reaction_replies':[text[:240] for text in reactions['replies']],'reaction_notes':[('〔见current_note〕' if len(text[-360:])>12 and text[-360:] in row['text'][-1000:] else text[-360:]) for text in reactions['notes']],
               'text':r['text'][:200]}
        events.append({k:v for k,v in event.items() if v or k in {'id','by_target'}})
    deleted,written=_split(previous,row['text'])
    note_tail=_without(row['text'][-1000:],written,'〔同写入〕') if len(written)<=1000 else row['text'][-1000:]
    old_tail=_without(previous[-300:],deleted,'〔同删去〕') if len(deleted)<=400 else previous[-300:]
    return {'id':job['original_id'],'written_at':_clock(job['noted_at']),
            'previous_note':old_tail,'note_change':_difference(previous,row['text']),
            'current_note':note_tail,'event_choices':[r['id'] for r in anchors],
            'events':events}

def classify(packets,base,*,daily=()):
    from hakimi.curation.review_runtime import classify as run_review
    from hakimi.memory.daily_events import INSTRUCTION
    from hakimi.memory.relationship_policy import CLASSIFICATION_PROTOCOL
    task=(
        'Return one JSON object directly: {"items":[{"id":note id,"event_id":source id,"kind":"relationship",'
        '"directed_to_hakimi":true,"event_type":"类型","evidence":"原文片段",'
        '"reaction_type":"反应类型","reaction_source":"note","reaction_evidence":"笔记或回复片段","reason":"简述"}]}. '
        '按人物笔记变更对应的真实聊天事件定性，previous_note为背景，note_change为本次新增记录。'
        'event_id从event_choices选取；结合mentions、reply_author与邻近SELF发言核对作者和对话对象。'
        '目标本人与哈基米的互动标kind=relationship，单纯个人事实或群友互聊标kind=fact。'
    ) + CLASSIFICATION_PROTOCOL
    if not packets:
        task='Return one JSON object directly with items:[], 评估daily_messages，顶层包含daily_events与relationship_events。'+CLASSIFICATION_PROTOCOL
    if daily:task+=INSTRUCTION
    return run_review(task+'\n'+json.dumps({'notes':packets,'daily_messages':list(daily)},
                        ensure_ascii=False,separators=(',',':')),base)

def _baseline(db,scope,target):
    saved=db.execute('SELECT * FROM impression_scores WHERE scope=? AND target=?',(scope,target)).fetchone()
    if saved:return dict(saved)
    sql="session LIKE 'group:%'" if scope=='groups' else 'session=?'
    args=[target]+([] if scope=='groups' else [scope])
    old=db.execute("SELECT * FROM social_assessments WHERE subject_id='hakimi' AND target_id=? AND "+sql+
                   " AND state!='deleted' AND ((provider='gemini' AND provenance='gemini_manual') OR (provider='dsh' AND provenance='dsh_assisted')) AND seed_id='' ORDER BY created_at DESC,id DESC LIMIT 1",args).fetchone()
    return {k:old[k] if old else None for k in ('affinity','closeness','confidence')}

def _projection(db,job,now):
    """Generic person notes gain a social view of the same immutable original."""
    existing=db.execute('SELECT id FROM social_assessments WHERE original_record_id=?',(job['record_id'],)).fetchone()
    if existing:return
    row=db.execute('SELECT * FROM memory_originals WHERE id=?',(job['original_id'],)).fetchone()
    provenance='gemini_manual' if row['provider']=='gemini' else 'dsh_assisted'
    old=db.execute("SELECT * FROM social_assessments WHERE subject_id='hakimi' AND target_id=? AND session=? AND relationship_type='人物印象' AND provider=? AND state='active' ORDER BY created_at DESC,id DESC LIMIT 1",
                   (job['target'],job['session'],row['provider'])).fetchone()
    cur=db.execute('''INSERT INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,
        impression,reason,relationship_type,original_record_id,created_at,session,version,supersedes_id)
        VALUES (?,'hakimi',?,?,?,?,?,'','人物印象',?,?,?,?,?)''',
        ('impression-event:'+str(row['id']),job['target'],row['provider'],row['provider'],provenance,row['text'],
         job['record_id'],job['noted_at'],job['session'],int(old['version'])+1 if old else 1,old['id'] if old else None))
    if old:db.execute("UPDATE social_assessments SET state='superseded',superseded_by=? WHERE id=?",(cur.lastrowid,old['id']))

def apply(db,job,packet,item,now, *, direction=None, allow_revision=False):
    from hakimi.memory.review_audit import record as audit
    from hakimi.memory import review_recovery
    execution_now=now
    scope=_scope(job['session']);target=job['target'];event=int(item['event_id'])
    recovery_kind='note' if job['record_id'] else job.get('review_source','daily')
    recovery_job=job.get('review_job',job.get('original_id') or event)
    recovery=review_recovery.load_metadata(db,recovery_kind,recovery_job)
    now=review_recovery.review_clock(db,recovery_kind,recovery_job,now)
    def reviewed(outcome, reason=''):
        from hakimi.memory.review_results import decision_details
        details=decision_details(job,item,outcome,reason)
        effect=db.execute('SELECT affinity_delta,closeness_delta,updated FROM impression_effects WHERE scope=? AND target=? AND event_id=?',(scope,target,event)).fetchone()
        if effect and effect['updated']==now and outcome in {'applied','revised','no_change'}:
            details['settled_effect']={'affinity':effect['affinity_delta'],'closeness':effect['closeness_delta']}
        audit(db,now,job=job.get('review_job',job.get('original_id') or ''),source=job.get('review_source','note' if job['record_id'] else 'daily'),event_id=event,message_id=event_row['origin'] if event_row else '',item=item,outcome=outcome,reason=reason,details=details)
        if recovery and recovery_kind in {'note','daily'}:
            status='source_unavailable' if reason=='source_unavailable' else 'already_reviewed' if reason=='recovery_already_settled' else 'done'
            explanation='原文证据已清理，本次补记未计分' if status=='source_unavailable' else '原事件已结算，本次仅补记零变化' if status=='already_reviewed' else '已按原复盘时间补记完成，未额外重复计分'
            review_recovery.complete_metadata(db,recovery_kind,recovery_job,execution_now,status,explanation)
        return outcome
    event_row=db.execute('SELECT * FROM records WHERE id=?',(event,)).fetchone()
    from hakimi.memory.relationship_policy import normalize_event, score_change
    from hakimi.memory import relationship_events
    direction=direction or (relationship_events.packet(db,event_row) if event_row else {})
    reactions = relationship_events.reaction_sources(db, event_row, direction.get('context', [])) if event_row else {'replies': [], 'notes': []}
    if job.get('original_id'):
        own_note = db.execute('SELECT text FROM memory_originals WHERE id=?', (job['original_id'],)).fetchone()
        if own_note and own_note['text'] and own_note['text'] not in reactions['notes']:
            reactions['notes'] = [own_note['text'], *reactions['notes']]
    item = normalize_event(item, event_row['text'] if event_row else '',
                           reply_texts=reactions['replies'], note_texts=reactions['notes'])
    note_row=db.execute('SELECT state FROM records WHERE id=?',(job['record_id'],)).fetchone() if job['record_id'] else {'state':'active'}
    if (not event_row or event_row['state'] in {'deleted','retracted','archived'} or not event_row['text'].strip()
            or not note_row or note_row['state'] in {'deleted','retracted'}):
        db.execute("UPDATE impression_jobs SET status='no_event',reason='原文在分类期间已删除或撤回' WHERE original_id=?",(job['original_id'],))
        return reviewed('skip','source_unavailable')
    from hakimi.memory import relationship_events
    direction=direction or relationship_events.packet(db,event_row)
    if recovery and not allow_revision and not review_recovery.eligible_event(db,event,recovery_kind,recovery_job):
        if job['original_id']:
            db.execute("UPDATE impression_jobs SET status='done',reason='already_reviewed' WHERE original_id=?",(job['original_id'],))
        return reviewed('duplicate','recovery_already_settled')
    old=db.execute('SELECT * FROM impression_effects WHERE scope=? AND target=? AND event_id=?',(scope,target,event)).fetchone()
    note=bool(job['record_id'] and job['original_id'])
    current=_baseline(db,scope,target)
    if job['record_id']:
        _projection(db,job,now)
        db.execute("UPDATE social_assessments SET affinity=?,closeness=?,confidence=? WHERE original_record_id=? AND subject_id='hakimi' AND seed_id=''",
                   (current['affinity'],current['closeness'],current['confidence'],job['record_id']))
    def finished(outcome, reason=''):
        db.execute("UPDATE impression_jobs SET status='done',event_id=?,verdict=?,reason=? WHERE original_id=?",
                   (event,item['valence'],str(reason or item['reason'])[:240],job['original_id']))
        return reviewed(outcome,reason)
    # Only this exact event can be revised; unrelated subsequent notes do not
    # change an earlier effect, and stale/replayed note operations are inert.
    unverified_cleared = bool(
        old
        and not old['original_id']
        and old['affinity_delta'] < 0
        and item.get('host_reaction_verified') is True
        and item.get('host_changes', (0, 0))[0] >= 0
    )
    if old:
        if not note and not unverified_cleared and (not allow_revision or old['original_id']):
            return finished('duplicate', 'note_priority' if old['original_id'] else 'already_settled')
        if old['original_id'] and note and not allow_revision:
            prior = db.execute('SELECT created_at FROM memory_originals WHERE id=?', (old['original_id'],)).fetchone()
            old_order = (prior[0] if prior else 0, int(old['original_id']))
            if int(job['original_id']) == int(old['original_id']) or (job['noted_at'], int(job['original_id'])) <= old_order:
                return finished('duplicate', 'note_already_reviewed_or_older')
    if old and not item['evidence']:
        return finished('no_change', 'missing_event_evidence_keep_prior')
    attributed = item.get('directed_to_hakimi') is True and str(event_row['user_id']) == str(target) and relationship_events.directed(direction)
    if not attributed:
        if not old and item.get('kind') == 'fact' and item['valence'] == 'neutral' and item['closeness'] == 'unchanged':
            return finished('no_change')
        if not (allow_revision and old and item.get('kind') == 'fact'):
            # Missing/ambiguous addressee never retracts a settled contribution.
            return finished('other_addressee', 'unconfirmed_addressee_keep_prior')
    if item.get('kind') != 'relationship':
        item = normalize_event(dict(item, event_type='personal_fact'), event_row['text']); changing = False
    from hakimi.memory.relationship_policy import escalate_negative_changes
    if item.get("host_changes", (0, 0))[0] < 0:
        if allow_revision and "repeat_negative_count" in item:
            repeat_neg = int(item["repeat_negative_count"])
        else:
            repeat_neg = max(int(item.get("repeat_negative_count") or 0),
                             _recent_negative_count(db, scope, target, event, now))
        item = dict(item, repeat_negative_count=repeat_neg)
        from hakimi.memory.relationship_policy import _event_policy
        base_changes, base_impact = _event_policy(
            item.get("event_type", "unknown"),
            item.get("host_evidence_verified") is True,
            item.get("reaction_type"),
            item.get("host_reaction_verified") is True,
            malicious=item.get("malicious") is True,
        )
        new_changes, new_impact = escalate_negative_changes(
            item.get("event_type", "unknown"),
            base_changes,
            base_impact,
            repeat_negative_count=repeat_neg,
            malicious=item.get("malicious") is True,
        )
        item["host_changes"] = new_changes
        item["host_impact"] = new_impact
        item["valence"] = "positive" if new_changes[0] > 0 else "negative" if new_changes[0] < 0 else "neutral"
        item["intensity"] = "strong" if new_impact == "major" else "ordinary" if any(new_changes) else "light"
        item["closeness"] = "closer" if new_changes[1] > 0 else "further" if new_changes[1] < 0 else "unchanged"
    changing = item['valence'] != 'neutral' or item['closeness'] != 'unchanged'
    if not old and changing:
        # Compare the movement the host can actually apply, including boundary
        # damping; a blocked closeness decrease must not defeat episode dedup.
        movement = [score_change(field, current[field] if current[field] is not None else anchor, item)
                    for field, anchor in (('affinity', 50), ('closeness', 0))]
        comparable = dict(item, valence='positive' if movement[0] > 0 else 'negative' if movement[0] < 0 else 'neutral',
                          closeness='closer' if movement[1] > 0 else 'further' if movement[1] < 0 else 'unchanged')
        if relationship_events.duplicate(db, scope, target, event_row, comparable):
            return finished('duplicate')
    if not changing and not note and not ((allow_revision or unverified_cleared) and old):
        return finished('no_change')
    desired = item['host_changes']
    scores={};deltas=[]
    has_log_table = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='relationship_change_log'").fetchone())
    for field,anchor,change,oldfield in zip(('affinity','closeness'),(50,0),desired,('affinity_delta','closeness_delta')):
        before=current[field];start=max(0,min(100,(before if before is not None else anchor)-(old[oldfield] if old else 0)))
        change = score_change(field, start, item)
        value=max(0,min(100,start+change))
        prior_before = before
        if old and allow_revision and has_log_table:
            log = db.execute(f"SELECT {field}_before FROM relationship_change_log WHERE scope=? AND target=? AND event_id=? AND revision=0", (scope, str(target), event)).fetchone()
            if log is not None:
                prior_before = log[0]
        scores[field]=value if (prior_before if allow_revision else before) is not None or change or any(desired) else None
        deltas.append(value-start)
    confidence=None if (scores['affinity'] is None and scores['closeness'] is None and not any(deltas)) else (max(current['confidence'] or 0,80) if any(deltas) else current['confidence'])
    db.execute('INSERT OR REPLACE INTO impression_scores VALUES (?,?,?,?,?)',(scope,target,scores['affinity'],scores['closeness'],confidence))
    db.execute('INSERT OR REPLACE INTO impression_effects VALUES (?,?,?,?,?,?,?,?,?)',
               (scope,target,event,*deltas,job['original_id'],item['valence'],item['reason'],now))
    from hakimi.memory.relationship_history import record as record_change
    record_change(db,job,item,current,scores,now,revision=bool(old))
    if not job['record_id']:
        db.execute("""INSERT OR IGNORE INTO social_assessments(request_id,subject_id,target_id,author,provider,provenance,impression,reason,relationship_type,created_at,session)
            SELECT ?,'hakimi',?,'dsh','dsh','dsh_assisted',?,?,'relationship_event',?,? WHERE NOT EXISTS(
            SELECT 1 FROM social_assessments WHERE subject_id='hakimi' AND target_id=? AND session=? AND state='active' AND seed_id='' AND ((provider='gemini' AND provenance='gemini_manual') OR (provider='dsh' AND provenance='dsh_assisted')))""",
            ('relationship-event:'+str(event),target,item['reason'],item['reason'],now,job['session'],target,job['session']))
    policy={'mode':'notebook_event' if job['record_id'] else 'relationship_event','evaluator':'program_event_policy_v3','classifier':job.get('reviewer','dsh'),'event_type':item['event_type'],'evidence':item['evidence'],'reaction_type':item['reaction_type'],'reaction_source':item['reaction_source'],'reaction_evidence':item['reaction_evidence'],'reaction_verified':item['host_reaction_verified'],'impact':item['host_impact'],'note_record_id':job['record_id'],'operation_at':job['noted_at'],
            'event_id':'M'+str(event),'valence':item['valence'],'intensity':item['intensity'],
            'closeness_tendency':item['closeness'],'event_effect':dict(zip(('affinity','closeness'),deltas)),
            'previous':current['affinity'],'applied':scores['affinity'],
            'delta':(scores['affinity'] if scores['affinity'] is not None else 50)-(current['affinity'] if current['affinity'] is not None else 50),
            'reason':item['reason']}
    for key in ('review_model', 'review_reasoning_effort'):
        if key in job:policy[key]=job[key]
    sql="session LIKE 'group:%'" if scope=='groups' else 'session=?'
    args=[target]+([] if scope=='groups' else [scope])
    rows=db.execute("SELECT * FROM social_assessments WHERE subject_id='hakimi' AND target_id=? AND "+sql+
                    " AND state='active' AND seed_id=''",args).fetchall()
    for r in rows:
        if not _own(r):continue
        from hakimi.social.social_behavior import _evidence_survives
        ids=[event]
        for old_id in json.loads(r['evidence_record_ids']):
            if old_id in ids:continue
            old_row=db.execute('SELECT * FROM records WHERE id=?',(old_id,)).fetchone()
            if _evidence_survives(old_row,'group' if scope=='groups' else 'private',job['session'],target,now):ids.append(old_id)
            if len(ids)>=8:break
        stamps=[v[0] for v in db.execute('SELECT at FROM records WHERE id IN ('+','.join('?' for _ in ids)+')',ids)]
        dates=sorted({time.strftime('%Y-%m-%d',time.gmtime(v)) for v in stamps})
        db.execute('UPDATE social_assessments SET affinity=?,closeness=?,confidence=?,affinity_policy=?,evidence_record_ids=?,evidence_times=?,evidence_dates=? WHERE id=?',
                   (scores['affinity'],scores['closeness'],confidence,json.dumps(policy,ensure_ascii=False),json.dumps(ids),json.dumps(stamps),json.dumps(dates),r['id']))
    db.execute("UPDATE impression_jobs SET status='done',event_id=?,verdict=?,reason=? WHERE original_id=?",
               (event,item['valence'],item['reason'],job['original_id']))
    return reviewed('revised' if old else 'applied' if any(deltas) else 'no_change', 'score_curve_no_movement' if changing and not any(deltas) else '')


def _pending_recovery(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='review_recovery_jobs'").fetchone():return None
    row=db.execute("SELECT r.* FROM review_recovery_jobs r LEFT JOIN impression_jobs n ON r.kind='note' AND CAST(n.original_id AS TEXT)=r.job LEFT JOIN daily_event_jobs d ON r.kind='daily' AND CAST(d.source_id AS TEXT)=r.job WHERE r.status='pending' AND ((r.kind='note' AND n.status='pending') OR (r.kind='daily' AND d.status='pending')) ORDER BY r.original_review_at,r.id LIMIT 1").fetchone()
    return dict(row) if row else None


def _recovery_daily_packet(db,event_id):
    from hakimi.memory.relationship_events import packet
    row=db.execute("SELECT * FROM records WHERE id=? AND state='active' AND text!=''",(event_id,)).fetchone()
    if not row:return None
    value=packet(db,row)
    value.update(message_id=row['origin'],source='daily',reply_to=row['reply_to'],text=row['text'][:420])
    value['context']=[dict(r,text=r['text'][:140]) for r in value.get('context',[])]
    reply=db.execute("SELECT id,user_id,at,text,state FROM records WHERE kind='chat' AND session=? AND origin=? LIMIT 1",(row['session'],row['reply_to'])).fetchone() if row['reply_to'] else None
    if reply and reply['state']=='active' and reply['text']:
        value['reply_context']={'id':reply['id'],'at':reply['at'],'text':reply['text'][:140]}
    return value


def _current_choice(value,choices,packets=()):
    if isinstance(value,bool):return None
    if isinstance(value,int) and value in choices:return value
    if isinstance(value,str):
        clean=value.strip().removeprefix('M')
        if clean.isdigit() and int(clean) in choices:return int(clean)
        found=[p['id'] for p in packets if str(p.get('message_id',''))==value.strip() and p['id'] in choices]
        if len(found)==1:return found[0]
    return None

def tick(base,now=None,*,runner=None):
    """Invoked by the existing five-minute maintenance tick; no extra daemon."""
    from hakimi.memory.memory_store import store
    from hakimi.memory import daily_events, relationship_events
    from hakimi.curation import review_runtime
    if runner is None and not review_runtime.is_enabled(base):
        return {'status':'idle','classified':0,'reason':'review_disabled'}
    real_time=now is None
    now=time.time() if real_time else now;index=store(str(Path(base).resolve()))
    initialize(base)
    with index.db() as db:
        from hakimi.memory.relationship_history import maintain
        maintain(db,now)
        discover(db,now)
        from hakimi.memory import review_recovery
        recovery=_pending_recovery(db)
        if recovery:
            jobs=db.execute("SELECT * FROM impression_jobs WHERE original_id=? AND status='pending' AND next_attempt<=?",(recovery['job'],now)).fetchall() if recovery['kind']=='note' else []
        else:
            jobs=db.execute("SELECT * FROM impression_jobs WHERE status='pending' AND next_attempt<=? ORDER BY original_id LIMIT 4",(now,)).fetchall()
        packets=[];selected={}
        for row in jobs:
            job=dict(row)
            try:packet=_packet(db,job)
            except (ValueError,KeyError) as exc:
                db.execute("UPDATE impression_jobs SET status='error',reason=? WHERE original_id=?",(type(exc).__name__,job['original_id']))
                continue
            if not packet:
                db.execute("UPDATE impression_jobs SET status='no_event',reason='本次时间窗口没有相关可读事件' WHERE original_id=?",(job['original_id'],))
                if recovery:review_recovery.complete_metadata(db,'note',job['original_id'],now,'source_unavailable','原文证据已清理，本次补记未计分')
                continue
            if recovery:
                packet['event_choices']=[eid for eid in packet['event_choices'] if review_recovery.eligible_event(db,eid,'note',job['original_id'])]
                if not packet['event_choices']:
                    db.execute("UPDATE impression_jobs SET status='done',reason='already_reviewed' WHERE original_id=?",(job['original_id'],))
                    review_recovery.complete_metadata(db,'note',job['original_id'],now,'already_reviewed','原事件已结算，本次仅补记零变化')
                    continue
            if packets and len(json.dumps([*packets,packet],ensure_ascii=False))>8500:break
            packets.append(packet);selected[job['original_id']]=job
        if recovery:
            daily=[]
            if recovery['kind']=='daily':
                ready=db.execute("SELECT status,next_attempt FROM daily_event_jobs WHERE source_id=?",(recovery['job'],)).fetchone()
                if ready and ready['status']=='pending' and ready['next_attempt']<=now:
                    source=_recovery_daily_packet(db,int(recovery['job']))
                    if source and review_recovery.eligible_event(db,source['id'],'daily',recovery['job']):daily=[source]
                    else:
                        status='already_reviewed' if source else 'source_unavailable'
                        db.execute("UPDATE daily_event_jobs SET status='done' WHERE source_id=?",(recovery['job'],))
                        review_recovery.complete_metadata(db,'daily',recovery['job'],now,status,'原事件已结算，本次仅补记零变化' if source else '原文证据已清理，本次补记未计分')
        else:daily=daily_events.prepare(db,now,with_impressions=bool(packets))
    if not packets and not daily:return {'status':'idle','classified':0}
    try:
        result=runner(packets,base) if runner else classify(packets,base,daily=daily)
        from hakimi.curation.review_runtime import reviewer_metadata
        review_metadata=reviewer_metadata(result)
        if daily and (not isinstance(result,dict) or 'daily_events' not in result):raise ValueError('missing_daily_events')
        items=result.get('items',[]) if isinstance(result,dict) else result
        daily_items=daily_events.validate(result.get('daily_events',[]),daily) if isinstance(result,dict) else []
        relation_items=[];rejected=[];relation_seen=set()
        daily_sources=[p for p in daily if p.get('source')!='prune']
        daily_choices={p['id'] for p in daily_sources}
        reported=result.get('relationship_events',[]) if isinstance(result,dict) else []
        reported=reported if isinstance(reported,list) else []
        for raw_item in reported[:12]:
            event=_current_choice(raw_item.get('event_id'),daily_choices,daily_sources) if isinstance(raw_item,dict) else None
            if event is None or event in relation_seen:
                rejected.append('REVIEW_INVALID_RELATIONSHIP_SOURCE');continue
            try:relation_items.extend(relationship_events.validate([dict(raw_item,event_id=event)],daily_sources))
            except (ValueError,TypeError,KeyError):
                rejected.append('REVIEW_INVALID_RELATIONSHIP_EVENT');continue
            relation_seen.add(event)
        if not isinstance(items,list):raise ValueError('invalid_classification')
        mapped={}
        for item in items[:12]:
            key=_current_choice(item.get('id'),set(selected)) if isinstance(item,dict) else None
            packet=next((p for p in packets if p['id']==key),None)
            event=_current_choice(item.get('event_id'),set(packet['event_choices']),packet['events']) if packet else None
            if key is None or key in mapped or event is None:
                rejected.append('REVIEW_INVALID_RELATIONSHIP_SOURCE');continue
            mapped[key]=dict(item,id=key,event_id=event,reason=str(item.get('reason',''))[:240])
        with index.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for packet in packets:
                job=dict(selected[packet['id']],**review_metadata)
                state=db.execute('SELECT status FROM impression_jobs WHERE original_id=?',(job['original_id'],)).fetchone()
                if state and state[0]=='pending':
                    if packet['id'] in mapped:apply(db,job,packet,mapped[packet['id']],now)
                    else:
                        from hakimi.memory.review_audit import record as audit
                        review_at=review_recovery.review_clock(db,'note',job['original_id'],now)
                        audit(db,review_at,job=job['original_id'],source='note',outcome='skip',error_code='REVIEW_INVALID_RELATIONSHIP_SOURCE',reason='no matching event in current note choices',details=review_metadata)
                        db.execute("UPDATE impression_jobs SET status='done',reason='no_matching_event' WHERE original_id=?",(job['original_id'],))
                        review_recovery.complete_metadata(db,'note',job['original_id'],now,reason='原复盘输出未匹配当前事件，已补记未计分')
            for item in relation_items:
                source=next(p for p in daily if p['id']==item['event_id'])
                job={'session':source['session'],'target':str(source['author']),'record_id':None,'original_id':None,'noted_at':source['at'],'review_job':source['id'],'review_source':'daily'}
                job.update(review_metadata)
                apply(db,job,None,item,now)
            from hakimi.memory.review_audit import record as audit
            for code in rejected:
                failed_at=review_recovery.review_clock(db,recovery['kind'],recovery['job'],now) if recovery else now
                audit(db,failed_at,job=recovery['job'] if recovery else '',source=recovery['kind'] if recovery else 'maintenance',outcome='skip',error_code=code,details=review_metadata)
            reviewed_ids={i['event_id'] for i in relation_items}
            raw={event:i for i in reported if isinstance(i,dict) and (event:=_current_choice(i.get('event_id'),daily_choices,daily_sources)) is not None}
            for source in daily:
                if source['id'] not in reviewed_ids:
                    item=raw.get(source['id'],{})
                    if source.get('source')=='prune':
                        outcome='skip';reason='cleanup_only'
                    elif not item:
                        outcome='not_selected';reason='no representative relationship event selected'
                    elif item.get('kind')=='fact':
                        outcome='no_change';reason=item.get('reason','fact only')
                    elif item.get('directed_to_hakimi') is not True or not relationship_events.directed(source):
                        outcome='other_addressee';reason=item.get('reason','unconfirmed addressee')
                    else:
                        outcome='no_change';reason=item.get('reason','no relationship movement')
                    source_at=review_recovery.review_clock(db,'daily',source['id'],now)
                    audit(db,source_at,job=source['id'],source=source.get('source','daily'),event_id=source['id'],
                          message_id=source.get('message_id',''),item=item,outcome=outcome,reason=reason,details=review_metadata)
            if isinstance(result,dict):
                daily_at=review_recovery.review_clock(db,'daily',daily[0]['id'],now) if recovery and daily else now
                daily_events.apply(index,db,daily,daily_items,daily_at)
                if recovery:
                    db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('daily_events_last_attempt',?)",(str(now),))
                    for source in daily:
                        review_recovery.complete_metadata(db,'daily',source['id'],now,reason='已按原复盘时间补记完成，未额外重复计分')
        return {'status':'completed','classified':len(packets),'daily_events':len(daily_items),'relationship_events':len(relation_items)}
    except Exception as exc:
        now=time.time() if real_time else now
        from hakimi.curation.review_runtime import error_code
        from hakimi.memory.review_audit import record as audit
        code=error_code(exc)
        with index.db() as db:
            if recovery:
                failed_at=review_recovery.review_clock(db,recovery['kind'],recovery['job'],now)
                audit(db,failed_at,job=recovery['job'],source=recovery['kind'],outcome='error',error_code=code)
            else:audit(db,now,source='maintenance',outcome='error',error_code=code)
            for uid in selected:
                db.execute("UPDATE impression_jobs SET attempts=attempts+1,next_attempt=? + CASE attempts WHEN 0 THEN 30 WHEN 1 THEN 120 ELSE 300 END,status=CASE WHEN attempts>=3 THEN 'error' ELSE 'pending' END,reason=? WHERE original_id=? AND status='pending'",
                           (now,code,uid))
            daily_events.failed(db,daily,now)
        from hakimi.features.codex_help import report_fault
        report_fault(base,'impression_events',code=code)
        return {'status':'error','reason':code}
