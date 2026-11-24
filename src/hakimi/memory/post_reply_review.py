"""Review confirmed outgoing speech once a reply window has actually ended.

Only bounded reply excerpts and incoming IDs survive until classification. No
network work occurs on receipt, scheduler or delivery paths.
"""
import hashlib
import json
from pathlib import Path
import time

from hakimi.memory import impression_events, relationship_events, review_audit, review_recovery


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reply_review_windows(
        id TEXT PRIMARY KEY,session TEXT,created_at REAL,updated_at REAL,
        status TEXT,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,
        human_ids TEXT DEFAULT '[]',replies TEXT DEFAULT '[]',error_code TEXT DEFAULT '')''')
    db.execute('CREATE INDEX IF NOT EXISTS reply_review_due ON reply_review_windows(status,next_attempt,updated_at)')
    review_audit.initialize(db)


def _db(base):
    from hakimi.memory.memory_store import store
    return store(str(Path(base).resolve())).db()


def _ids(rows):
    return [str(r.get('message_id')) for r in rows if r.get('message_id') and r.get('user_id') != 'SELF'][-12:]


def receipt(base, session, receipt_id, text, message_id, trigger, now, humans=(), target_ids=()):
    """Called only after OneBot acknowledged this particular bubble."""
    if not str(text).strip():
        return
    with _db(base) as db:
        initialize(db)
        row = db.execute("SELECT * FROM reply_review_windows WHERE session=? AND status='open' ORDER BY created_at DESC LIMIT 1", (session,)).fetchone()
        if row is None:
            closing = db.execute("SELECT * FROM reply_review_windows WHERE session=? AND status='closing' ORDER BY created_at DESC LIMIT 1", (session,)).fetchone()
            if closing and any(r['receipt'].rsplit(':',1)[0] == str(receipt_id).rsplit(':',1)[0] for r in json.loads(closing['replies'])):
                row=closing
        key = row['id'] if row else hashlib.sha256((session+':'+str(receipt_id)).encode()).hexdigest()[:24]
        replies = json.loads(row['replies']) if row else []
        if any(r['receipt'] == str(receipt_id) for r in replies):
            return
        replies.append({'receipt':str(receipt_id), 'message_id':str(message_id or ''),
                        'speaker':'SELF', 'at':now, 'text':str(text)[:240],
                        'reply_to':[str(i) for i in target_ids][-4:]})
        ids = list(dict.fromkeys((json.loads(row['human_ids']) if row else [])+_ids(humans)))[-12:]
        # Keep eight actual speech excerpts; never the unsent planned response.
        if row:
            db.execute('UPDATE reply_review_windows SET updated_at=?,human_ids=?,replies=? WHERE id=?',
                       (now,json.dumps(ids),json.dumps(replies[-8:],ensure_ascii=False),key))
        else:
            db.execute('INSERT OR IGNORE INTO reply_review_windows(id,session,created_at,updated_at,status,human_ids,replies) VALUES (?,?,?,?,?,?,?)',
                       (key,session,now,now,'open',json.dumps(ids),json.dumps(replies[-8:],ensure_ascii=False)))
        _prune(db,now)


def delivery_pending(base, session):
    path=Path(base)/'state/delivery_state.json'
    try:
        if not path.exists():
            return False
        pending=json.loads(path.read_text(encoding='utf-8')).get('pending')
        return bool(pending and pending.get('session_key')==session)
    except (OSError,ValueError,TypeError,AttributeError):
        # An unreadable delivery checkpoint cannot prove the last bubble ended.
        return True


def close(base, session, now, *, delivering=False, humans=()):
    delivering=delivering or delivery_pending(base,session)
    with _db(base) as db:
        initialize(db)
        rows = db.execute("SELECT * FROM reply_review_windows WHERE session=? AND status IN ('open','closing')", (session,)).fetchall()
        for row in rows:
            ids = list(dict.fromkeys(json.loads(row['human_ids'])+_ids(humans)))[-12:]
            db.execute('UPDATE reply_review_windows SET status=?,updated_at=?,next_attempt=?,human_ids=? WHERE id=?',
                       ('closing' if delivering else 'ready',now,now,json.dumps(ids),row['id']))


def delivery_finished(base, session, now, *, listening, humans=()):
    with _db(base) as db:
        initialize(db)
        closing = db.execute("SELECT 1 FROM reply_review_windows WHERE session=? AND status='closing'", (session,)).fetchone()
    if closing or not listening:
        close(base,session,now,humans=humans)


def recover(base, scheduler, now):
    """An active restored listening/delivery window remains open, never age-sealed."""
    with _db(base) as db:
        initialize(db)
        db.execute("UPDATE reply_review_windows SET status='retry',next_attempt=? WHERE status='processing'", (now,))
        rows = db.execute("SELECT DISTINCT session FROM reply_review_windows WHERE status IN ('open','closing')").fetchall()
    for row in rows:
        state = scheduler.groups.get(row['session'])
        if delivery_pending(base,row['session']) or (state and (state.listen_round or state.listen_delivery_pending)):
            continue
        close(base,row['session'],now)


def _prune(db, now):
    # Successful jobs retain metadata only; failed payloads remain bounded for recovery; audit is separately capped at 1000/7d.
    db.execute("UPDATE reply_review_windows SET human_ids='[]',replies='[]' WHERE status='done'")
    db.execute("DELETE FROM reply_review_windows WHERE status IN ('done','error') AND (updated_at<? OR id NOT IN (SELECT id FROM reply_review_windows ORDER BY updated_at DESC LIMIT 128))", (now-7*86400,))
    # Admission is bounded even if the remote provider remains unavailable.
    excess = db.execute("SELECT id FROM reply_review_windows WHERE status IN ('ready','retry') ORDER BY updated_at DESC LIMIT -1 OFFSET 128").fetchall()
    for row in excess:
        review_audit.record(db,now,job=row['id'],source='window',outcome='skip',reason='queue_capacity')
        db.execute("UPDATE reply_review_windows SET status='error',error_code='REVIEW_QUEUE_FULL',human_ids='[]',replies='[]' WHERE id=?", (row['id'],))


def next_due(base, now, *, maintenance_only=False, window_only=False):
    with _db(base) as db:
        initialize(db)
        row = db.execute("SELECT MIN(next_attempt) FROM reply_review_windows WHERE status IN ('ready','retry')").fetchone()
        # Maintenance retries use the same wakeup clock, independent of successful
        # daily batching; no idle high-frequency polling is necessary.
        values = [row[0]] if row[0] is not None and not maintenance_only else []
        for table in (() if window_only else ('daily_event_jobs','impression_jobs')):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                if table=='daily_event_jobs':
                    query="SELECT MIN(j.next_attempt) FROM daily_event_jobs j JOIN records r ON r.id=j.source_id WHERE j.status='pending' AND j.attempts>0 AND r.state='active' AND r.text!=''"
                else:
                    query="SELECT MIN(j.next_attempt) FROM impression_jobs j JOIN records r ON r.id=j.record_id WHERE j.status='pending' AND j.attempts>0 AND r.state NOT IN ('deleted','retracted')"
                value=db.execute(query).fetchone()[0]
                if value is not None:
                    values.append(value)
        if not window_only and db.execute("SELECT 1 FROM sqlite_master WHERE name='review_recovery_jobs'").fetchone():
            for kind,table,key in (('note','impression_jobs','original_id'),('daily','daily_event_jobs','source_id')):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():continue
                value=db.execute('SELECT MIN(j.next_attempt) FROM '+table+' j JOIN review_recovery_jobs r ON r.kind=? AND r.job=CAST(j.'+key+" AS TEXT) WHERE r.status='pending' AND j.status='pending'",(kind,)).fetchone()[0]
                if value is not None:values.append(value)
        return min(values) if values else None


def packets(db, window):
    replies = json.loads(window['replies'])
    sources = []
    for origin in json.loads(window['human_ids']):
        row = db.execute("SELECT * FROM records WHERE kind='chat' AND session=? AND origin=? AND state='active' AND text!='' ORDER BY id DESC LIMIT 1", (window['session'],origin)).fetchone()
        if not row or not str(row['user_id']).isdigit() or row['user_id'] == row['self_id']:
            continue
        p = relationship_events.packet(db,row)
        p['self_id'] = p['self_id'] or 'SELF'
        if row['reply_to'] and str(row['reply_to']) in {r['message_id'] for r in replies}:
            p['reply_author'] = p['self_id']
        p['text'] = row['text'][:240]
        p['message_id'] = origin
        # Confirmed speaker boundaries replace the ambiguous dialogue signature.
        p['context'] = [{'kind':'chat','user_id':p['self_id'],
                         'speaker':'SELF','text':r['text'][:160],'at':r['at'],
                         'message_id':r['message_id'], 'reply_to':r['reply_to']}
                        for r in replies if origin in r['reply_to'] or abs(r['at']-row['at']) <= 90][-2:]
        p['reaction_notes'] = [text[-360:] for text in relationship_events.reaction_sources(db, row, p['context'])['notes']]
        sources.append(p)
    result = {'window_id':window['id'],'messages':sources,'sent_replies':replies}
    # Hard packet budget; retain the latest human turns with verified IDs.
    while len(json.dumps(result,ensure_ascii=False,separators=(',',':'))) > 6500 and result['messages']:
        result['messages'].pop(0)
    while len(json.dumps(result,ensure_ascii=False,separators=(',',':'))) > 6500 and len(result['sent_replies']) > 1:
        result['sent_replies'].pop(0)
    return result


def classifier_packet(packet):
    """Use short source IDs and keep platform message IDs out of model output."""
    messages = packet['messages']
    human = {str(row['message_id']): 'e'+str(index+1) for index, row in enumerate(messages)}
    bot = {}
    for row in [*packet.get('sent_replies', []), *[item for row in messages for item in row.get('context', [])]]:
        mid = str(row.get('message_id') or '')
        if mid and mid not in bot:bot[mid] = 'b'+str(len(bot)+1)
    def reference(value):
        return human.get(str(value), bot.get(str(value), 'outside_window'))
    def reply(row):
        item = {key:value for key,value in row.items() if key not in {'receipt', 'message_id'}}
        mid = str(row.get('message_id') or '')
        if mid in bot:item['id'] = bot[mid]
        if isinstance(item.get('reply_to'), list):item['reply_to'] = [reference(value) for value in item['reply_to']]
        elif item.get('reply_to'):item['reply_to'] = reference(item['reply_to'])
        return item
    result = {'messages': [], 'sent_replies': [reply(row) for row in packet.get('sent_replies', [])]}
    for index, row in enumerate(messages):
        item = {key:value for key,value in row.items()
                if key not in {'message_id','context','reaction_replies'}
                and not (key in {'mentions','reply_to','reply_author','reaction_notes'} and not value)}
        item['id'] = 'e'+str(index+1)
        item['context'] = [reply(value) for value in row.get('context', [])]
        if item.get('reply_to'):item['reply_to'] = reference(item['reply_to'])
        result['messages'].append(item)
    return result


def source_id(value, messages):
    """Resolve only exact source aliases/IDs belonging to this one window."""
    if type(value) not in (str, int):return None
    key = str(value).strip().casefold()
    aliases = {}
    for index, row in enumerate(messages):
        for alias in ('e'+str(index+1), str(row['id']), 'M'+str(row['id']), str(row['message_id'])):
            aliases.setdefault(alias.casefold(), set()).add(row['id'])
    found = aliases.get(key, set())
    return next(iter(found)) if len(found)==1 else None


from hakimi.memory.relationship_policy import CLASSIFICATION_PROTOCOL

INSTRUCTION = (
    '复盘刚结束的对话窗口，依据messages与SELF已发送回复输出JSON：'
    '{"relationship_events":[{"event_id":"e1","kind":"relationship","directed_to_hakimi":true,'
    '"event_type":"类型","evidence":"人类消息原文片段","reaction_type":"反应类型",'
    '"reaction_source":"reply","reaction_evidence":"SELF回复或笔记原文片段","reason":"简述"}]}。'
    'event_id取自messages[].id(e1,e2...)，b开头id表示SELF回复；每轮最多选2条代表性事件且每人最多1条，无合适事件返回空列表。'
    '明确@优先于引用目标；隐式互动结合邻近SELF回复与上下文衔接判定。'
) + CLASSIFICATION_PROTOCOL


def tick(base, now=None, *, runner=None):
    from hakimi.memory.memory_store import store
    from hakimi.memory.review_runtime import classify, error_code, is_enabled
    if runner is None and not is_enabled(base):
        return {'status': 'idle', 'reason': 'review_disabled'}
    real_time = now is None
    now = time.time() if real_time else now
    index = store(str(Path(base).resolve()))
    impression_events.initialize(base)
    with index.db() as db:
        initialize(db)
        if not db.execute("SELECT 1 FROM reply_review_windows WHERE status IN ('ready','retry') AND next_attempt<=? LIMIT 1", (now,)).fetchone():
            return {'status':'idle'}
    # Import the ordinary inbox only when a ready window needs its source IDs.
    index.sync()
    with index.db() as db:
        initialize(db)
        window = db.execute("SELECT * FROM reply_review_windows WHERE status IN ('ready','retry') AND next_attempt<=? ORDER BY next_attempt,created_at LIMIT 1", (now,)).fetchone()
        if not window:
            return {'status':'idle'}
        review_recovery.ensure_metadata(db)
        recovery_window = db.execute("""SELECT w.* FROM reply_review_windows w JOIN review_recovery_jobs r
            ON r.kind='window' AND r.job=w.id WHERE r.status='pending'
            AND w.status IN ('ready','retry') AND w.next_attempt<=?
            ORDER BY r.original_review_at,r.id LIMIT 1""",(now,)).fetchone()
        window = dict(recovery_window or window)
        db.execute("UPDATE reply_review_windows SET status='processing' WHERE id=?", (window['id'],))
        packet = packets(db,window)
        recovery = review_recovery.load_metadata(db,'window',window['id'])
        review_at = review_recovery.review_clock(db,'window',window['id'],now)
        if recovery:
            packet['messages'] = [p for p in packet['messages'] if review_recovery.eligible_event(db,p['id'],'window',window['id'])]
        if not packet['messages']:
            review_audit.record(db,review_at,job=window['id'],source='window',outcome='skip',reason='already_reviewed' if recovery else 'no_readable_human_source')
            review_recovery.complete_metadata(db,'window',window['id'],now,status='already_reviewed' if recovery else 'done')
            db.execute("UPDATE reply_review_windows SET status='done' WHERE id=?", (window['id'],))
            _prune(db,now)
            return {'status':'completed','reviewed':0}
    try:
        result = runner(packet,base) if runner else classify(INSTRUCTION+'\n'+json.dumps(classifier_packet(packet),ensure_ascii=False,separators=(',',':')),base)
        from hakimi.memory.review_runtime import reviewer_metadata
        review_metadata=reviewer_metadata(result)
        raw = result.get('relationship_events')
        if not isinstance(raw,list):
            raise ValueError('invalid_relationship_events')
        allowed = {p['id']:p for p in packet['messages']}
        seen=set();accepted=[];rejected=[]
        for original in raw[:12]:
            if not isinstance(original,dict):
                rejected.append('invalid_relationship_source');continue
            event = source_id(original.get('event_id'), packet['messages'])
            if event is None or event in seen:
                rejected.append('invalid_relationship_source');continue
            item = dict(original,event_id=event)
            p=allowed[event]
            if item.get('kind') is None:item['kind']='relationship'
            directed=item.get('directed_to_hakimi')
            if isinstance(directed,str) and directed.casefold() in {'true','false'}:
                item['directed_to_hakimi']=directed.casefold()=='true'
            if item.get('kind') not in {'relationship','fact'} or type(item.get('directed_to_hakimi')) is not bool or not isinstance(item.get('event_type', 'unknown'), str):
                rejected.append('invalid_relationship_tendency');continue
            seen.add(event)
            item['reason']=str(item.get('reason',''))[:160]
            accepted.append(item)
        raw = accepted
        with index.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in raw:
                p=allowed[item['event_id']]
                job={'session':p['session'],'target':str(p['author']),'record_id':None,
                     'original_id':None,'noted_at':p['at'],'review_source':'window','review_job':window['id']}
                job.update(review_metadata)
                impression_events.apply(db,job,None,item,review_at,direction=p)
            for reason in rejected:
                review_audit.record(db,review_at,job=window['id'],source='window',outcome='skip',
                                    reason=reason,error_code=error_code(ValueError(reason)),details=review_metadata)
            for p in packet['messages']:
                if p['id'] not in seen:
                    review_audit.record(db,review_at,job=window['id'],source='window',event_id=p['id'],message_id=p['message_id'],outcome='not_selected',reason='no representative relationship event selected',details=review_metadata)
            db.execute("UPDATE reply_review_windows SET status='done',updated_at=? WHERE id=?", (now,window['id']))
            review_recovery.complete_metadata(db,'window',window['id'],now)
            _prune(db,now)
        return {'status':'completed','reviewed':len(raw),'ignored_sources':len(rejected),'window':window['id']}
    except Exception as exc:
        now=time.time() if real_time else now
        code=error_code(exc);attempt=window['attempts']
        with index.db() as db:
            review_audit.record(db,now,job=window['id'],source='window',outcome='error',error_code=code)
            db.execute('UPDATE reply_review_windows SET status=?,attempts=attempts+1,next_attempt=?,updated_at=?,error_code=? WHERE id=?',
                       ('error' if attempt>=3 else 'retry',now+(30,120,300,300)[min(attempt,3)],now,code,window['id']))
            _prune(db,now)
        return {'status':'error','reason':code,'window':window['id']}
