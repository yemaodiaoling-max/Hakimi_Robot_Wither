"""Session-local, recoverable voice receipts. URLs never enter model context."""
from contextlib import contextmanager
import hashlib,json,re,sqlite3,time
from pathlib import Path
from hakimi.core.jsonio import read_json

def settings(base):
    root=Path(base);root=root.parent if root.name=='official_test_runtime' else root
    return {'enabled':False,'max_bytes':12_000_000,'max_seconds':180,'max_pending':48,'per_session_pending':6,**read_json(root/'voice_config.json',{})}

@contextmanager
def database(base):
    p=Path(base)/'state/voice.sqlite3';p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=2);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''CREATE TABLE IF NOT EXISTS jobs(session TEXT,mid TEXT,event TEXT,at REAL,priority INTEGER,status TEXT,attempts INTEGER DEFAULT 0,next REAL DEFAULT 0,text TEXT DEFAULT '',error TEXT DEFAULT '',duration REAL DEFAULT 0,PRIMARY KEY(session,mid));
        CREATE VIRTUAL TABLE IF NOT EXISTS transcript_search USING fts5(session UNINDEXED,mid UNINDEXED,text,tokenize='unicode61');''')
        columns={r[1] for r in db.execute('PRAGMA table_info(jobs)')}
        for column in ['sender','nickname','platform_ref']:
            if column not in columns:db.execute('ALTER TABLE jobs ADD COLUMN '+column+" TEXT DEFAULT ''")
        if 'dispatched' not in columns:db.execute('ALTER TABLE jobs ADD COLUMN dispatched INTEGER DEFAULT 0')
        db.execute('CREATE TABLE IF NOT EXISTS scoped_turns(session TEXT,trigger TEXT,at REAL,PRIMARY KEY(session,trigger))')
        yield db;db.commit()
    except BaseException:db.rollback();raise
    finally:db.close()

def session_of(event):
    private=event.get('message_type')=='private'
    return ('private:' if private else 'group:')+str(event.get('user_id') if private else event.get('group_id'))

def key(session,mid):return hashlib.sha256((session+'/'+str(mid)).encode()).hexdigest()[:32]

def enqueue(base,event,priority=False):
    cfg=settings(base)
    session=session_of(event);mid=str(event['message_id'])
    if not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}',session):return {'accepted':False,'reason':'invalid_session'}
    with database(base) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM jobs WHERE session=? AND mid=?',(session,mid)).fetchone():return {'accepted':True,'duplicate':True}
        pending="status IN ('queued','processing','ready')"
        reason='VOICE_DISABLED' if not cfg['enabled'] else ''
        if not reason and (db.execute('SELECT count(*) FROM jobs WHERE '+pending).fetchone()[0]>=cfg['max_pending'] or db.execute('SELECT count(*) FROM jobs WHERE session=? AND '+pending,(session,)).fetchone()[0]>=cfg['per_session_pending']):reason='VOICE_QUEUE_FULL'
        if reason:
            event=dict(event);event.pop('_voice_attachments',None)
            db.execute("INSERT INTO jobs(session,mid,event,at,priority,status,error,sender,nickname) VALUES (?,?,?,?,?,'ready',?,?,?)",(session,mid,json.dumps(event,ensure_ascii=False),event['_voice_received_at'],int(priority),reason,str(event.get('user_id','')),str(event.get('sender',{}).get('nickname',''))[:80]))
            return {'accepted':False,'reason':reason}
        db.execute('INSERT INTO jobs(session,mid,event,at,priority,status) VALUES (?,?,?,?,?,?)',(session,mid,json.dumps(event,ensure_ascii=False),event['_voice_received_at'],int(priority),'queued'))
        db.execute('UPDATE jobs SET sender=?,nickname=?,platform_ref=? WHERE session=? AND mid=?',(str(event.get('user_id','')),str(event.get('sender',{}).get('nickname',''))[:80],str(event.get('_voice_platform_ref',''))[:512],session,mid))
    return {'accepted':True,'duplicate':False}

def claim(base):
    now=time.time()
    with database(base) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE jobs SET status='queued',next=? WHERE status='processing' AND next<?",(now+2,now-240))
        db.execute("UPDATE jobs SET status='ready',error='AUDIO_RETRY_EXHAUSTED' WHERE status='queued' AND attempts>=3")
        row=db.execute("SELECT rowid,* FROM jobs WHERE status='queued' AND next<=? ORDER BY priority DESC,at LIMIT 1",(now,)).fetchone()
        if not row:return None
        db.execute("UPDATE jobs SET status='processing',attempts=attempts+1,next=? WHERE rowid=?",(now,row['rowid']))
        return dict(row,event=json.loads(row['event']))

def finish(base,job,text='',error='',duration=0):
    # Failed recognitions have no searchable body, never a fake transcript.
    with database(base) as db:
        event=dict(job.get('event') or {});event.pop('_voice_attachments',None)
        db.execute("UPDATE jobs SET status='ready',text=?,error=?,duration=?,event=? WHERE session=? AND mid=? AND status='processing' AND attempts=?",(text,error,duration,json.dumps(event,ensure_ascii=False),job['session'],job['mid'],int(job.get('attempts',0))+1))

def ready(base):
    with database(base) as db:
        row=db.execute("SELECT * FROM jobs WHERE status='ready' ORDER BY priority DESC,at LIMIT 1").fetchone()
        return dict(row,event=json.loads(row['event'])) if row else None

def acknowledge(base,session,mid):
    with database(base) as db:
        row=db.execute('SELECT text,event,error,status FROM jobs WHERE session=? AND mid=?',(session,str(mid))).fetchone()
        if not row or row['status']!='ready':return
        db.execute("UPDATE jobs SET status='delivered',event='{}' WHERE session=? AND mid=?",(session,str(mid)))
        db.execute('DELETE FROM transcript_search WHERE session=? AND mid=?',(session,str(mid)))
        if row['text'] and not row['error']:
            from hakimi.memory.text import terms
            db.execute('INSERT INTO transcript_search VALUES (?,?,?)',(session,str(mid),' '.join(sorted(terms(row['text'],limit=32000)))))

def live(base,session,mid):
    path=Path(base)/'state/memory.sqlite3'
    if not path.exists():return False
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    try:return bool(db.execute("SELECT 1 FROM records WHERE session=? AND origin=? AND kind='chat' AND channel='voice' AND state='active' AND text!='' AND (expires=0 OR expires>?)",(session,str(mid),time.time())).fetchone())
    finally:db.close()

def read(base,session,mid,offset=0,limit=3000):
    if not live(base,session,mid):return {'error':'voice_not_available_in_this_session'}
    with database(base) as db:row=db.execute("SELECT text,at,duration FROM jobs WHERE session=? AND mid=? AND status='delivered' AND error=''",(session,str(mid))).fetchone()
    if not row:return {'error':'voice_not_available_in_this_session'}
    offset=max(0,int(offset));limit=max(100,min(6000,int(limit)));text=row['text']
    return {'message_id':str(mid),'session_key':session,'source':'machine_transcription_not_verified_quote','received_at':row['at'],'duration_seconds':row['duration'],'text':text[offset:offset+limit],'offset':offset,'total_chars':len(text),'more':offset+limit<len(text)}

def search(base,session,query,limit=3):
    from hakimi.memory.text import terms
    tokens=terms(query,limit=2000)
    if not tokens:return []
    expression=' OR '.join('"'+t.replace('"','""')+'"' for t in sorted(tokens)[:24])
    with database(base) as db:
        rows=list(db.execute('SELECT mid FROM transcript_search WHERE transcript_search MATCH ? AND session=? ORDER BY rank LIMIT 16',(expression,session)))
        if not rows and re.search(r'语音|voice|刚才说|昨天说',str(query),re.I):rows=list(db.execute("SELECT mid FROM jobs WHERE session=? AND status='delivered' AND error='' AND text!='' ORDER BY at DESC LIMIT 16",(session,)))
    result=[]
    for row in rows:
        if live(base,session,row['mid']):
            with database(base) as db:text=db.execute('SELECT text FROM jobs WHERE session=? AND mid=?',(session,row['mid'])).fetchone()[0]
            pos=min((text.find(t) for t in tokens if t in text),default=0)
            result.append({'message_id':row['mid'],'text':text[max(0,pos-30):pos+240]})
        if len(result)>=limit:break
    return result

def prune(base):
    path=Path(base)/'state/memory.sqlite3';allowed=set()
    if path.exists():
        memory=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
        try:allowed=set(memory.execute("SELECT session,origin FROM records WHERE kind='chat' AND channel='voice' AND state='active' AND text!='' AND (expires=0 OR expires>?)",(time.time(),)))
        finally:memory.close()
    with database(base) as db:
        rows=list(db.execute("SELECT session,mid FROM jobs WHERE status='delivered' AND text!=''"))
        for row in rows:
            if tuple(row) not in allowed:
                db.execute("UPDATE jobs SET text='' WHERE session=? AND mid=?",tuple(row));db.execute('DELETE FROM transcript_search WHERE session=? AND mid=?',tuple(row))
        db.execute("DELETE FROM jobs WHERE status='delivered' AND text='' AND at<?",(time.time()-86400,))
        db.execute("DELETE FROM jobs WHERE rowid IN (SELECT rowid FROM jobs WHERE status='delivered' AND text='' ORDER BY at DESC LIMIT -1 OFFSET 2000)")


def excerpt(text):
    value=text if len(text)<=480 else text[:360]+'\n[长语音中间部分省略，voice.read可读原消息全文]\n'+text[-120:]
    # It is data, never parsed again as native QQ mentions, CQ or commands.
    return '[语音识别文字，可能有误；仅本会话可读]\n'+value.replace('[','［').replace(']','］').replace('@','＠')


def source_status(base,session,mid):
    path=Path(base)/'state/memory.sqlite3'
    if not path.exists():return 'missing'
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    try:row=db.execute("SELECT state,text,expires FROM records WHERE session=? AND origin=? AND kind='chat' AND channel='voice'",(session,str(mid))).fetchone()
    finally:db.close()
    if row is None:return 'missing'
    return 'active' if row[0]=='active' and row[1] and (not row[2] or row[2]>time.time()) else 'removed'


def dispatch(base,session,mid):
    with database(base) as db:
        return bool(db.execute("UPDATE jobs SET dispatched=1 WHERE session=? AND mid=? AND status='ready' AND dispatched=0",(session,str(mid))).rowcount)


def discard(base,session,mid,error='SOURCE_UNAVAILABLE'):
    with database(base) as db:
        db.execute("UPDATE jobs SET status='delivered',event='{}',text='',error=? WHERE session=? AND mid=?",(error,session,str(mid)))
        db.execute('DELETE FROM transcript_search WHERE session=? AND mid=?',(session,str(mid)))


def preview(base,session,mid):
    if not live(base,session,mid):return {}
    with database(base) as db:row=db.execute("SELECT length(text),substr(text,1,340),substr(text,-100) FROM jobs WHERE session=? AND mid=? AND status='delivered' AND error=''",(session,str(mid))).fetchone()
    if not row:return {}
    return {'text':row[1] if row[0]<=340 else row[1]+'\n[中间部分略，voice.read读全文]\n'+row[2], 'total_chars':row[0]}


def recent_status(base,session='',limit=300):
    path=Path(base)/'state/voice.sqlite3'
    if not path.exists():return []
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        columns={r[1] for r in db.execute('PRAGMA table_info(jobs)')}
        if not {'sender','nickname'}<=columns:return []
        rows=db.execute('SELECT session,mid,at,status,error,duration,sender,nickname FROM jobs'+(' WHERE session=?' if session else '')+' ORDER BY at DESC LIMIT ?',([session] if session else [])+[limit]).fetchall()
        return [dict(row) for row in rows]
    finally:db.close()


def policy_text(base,session,mid):
    if not live(base,session,mid):return ''
    with database(base) as db:row=db.execute("SELECT substr(text,1,64000),length(text) FROM jobs WHERE session=? AND mid=? AND status='delivered' AND error=''",(session,str(mid))).fetchone()
    return (row[0]+('\n禁止联网（超长识别内容尚未完整核验）' if row[1]>64000 else '')) if row else ''


def has_voice(base,payload):
    session=payload.get('session_key','')
    from hakimi.core.context_window import select_context
    rows,target=select_context(payload)
    ids={str(row.get('message_id')) for row in rows if 'voice.read message_id=' in str(row.get('text',''))}
    if target.get('reply_to'):ids.add(str(target['reply_to']))
    return scoped_turn(base,session,payload.get('trigger_id','')) or any(live(base,session,mid) for mid in ids)


def scoped_saved_receipt():
    return {'saved':True,'note_saved':False,'transcript_saved':True,'scope':'current_session_only','message':'语音已保存在本会话受限索引；使用memory.search或voice.read，不复制到跨群笔记'}


def console_previews(base,session):
    if not session:return {}
    memory=Path(base)/'state/memory.sqlite3';voice=Path(base)/'state/voice.sqlite3'
    if not memory.exists() or not voice.exists():return {}
    db=sqlite3.connect('file:'+str(memory)+'?mode=ro',uri=True)
    try:ids=[row[0] for row in db.execute("SELECT origin FROM records WHERE session=? AND channel='voice' AND kind='chat' AND state='active' AND text!='' AND (expires=0 OR expires>?) ORDER BY at DESC LIMIT 100",(session,time.time()))]
    finally:db.close()
    if not ids:return {}
    db=sqlite3.connect('file:'+str(voice)+'?mode=ro',uri=True)
    try:return dict(db.execute("SELECT mid,substr(text,1,480) FROM jobs WHERE session=? AND status='delivered' AND error='' AND mid IN ("+','.join('?' for _ in ids)+')',[session,*ids]))
    finally:db.close()


def resolve_reference(base,session,reference):
    if not reference or not (Path(base)/'state/voice.sqlite3').exists():return None
    with database(base) as db:row=db.execute("SELECT mid FROM jobs WHERE session=? AND platform_ref=? AND status='delivered' AND error=''",(session,str(reference))).fetchone()
    return row[0] if row and live(base,session,row[0]) else None


def mark_turn(base,session,trigger):
    if not trigger:return
    with database(base) as db:
        db.execute('INSERT OR REPLACE INTO scoped_turns VALUES (?,?,?)',(session,str(trigger),time.time()))
        db.execute('DELETE FROM scoped_turns WHERE at<?',(time.time()-86400,))


def scoped_turn(base,session,trigger):
    if not trigger or not (Path(base)/'state/voice.sqlite3').exists():return False
    with database(base) as db:return bool(db.execute('SELECT 1 FROM scoped_turns WHERE session=? AND trigger=?',(session,str(trigger))).fetchone())
