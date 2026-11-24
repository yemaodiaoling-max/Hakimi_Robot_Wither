"""Shared bot labels and delivered-round cooldowns; no model calls on ingestion."""
from functools import lru_cache
from contextlib import contextmanager,closing
from pathlib import Path
import re,sqlite3,time

AT=re.compile(r'\[CQ:at,qq=([1-9][0-9]{0,19})\]')
BOT_REVIEW_SCHEMA={'type':'array','maxItems':3,'items':{'type':'object','additionalProperties':False,
    'properties':{'user_id':{'type':'string'},'verdict':{'type':'string','enum':['confirmed','ordinary','pending']},
                  'reason':{'type':'string','maxLength':240}},'required':['user_id','verdict','reason']}}

def _path(base):return Path(base)/'state/bot_registry.sqlite3'

@contextmanager
def _db(base):
    p=_path(base);p.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(p,timeout=3)) as db,db:
        db.execute('''CREATE TABLE IF NOT EXISTS bots(user_id TEXT PRIMARY KEY,status TEXT,source TEXT,
            nickname TEXT,session TEXT,reason TEXT,updated REAL,rounds INTEGER DEFAULT 0,
            last_reply REAL DEFAULT 0,cooldown REAL DEFAULT 0,receipt TEXT DEFAULT '')''')
        yield db

@lru_cache(maxsize=8)
def _read(path,stamp):
    with closing(sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        return {r['user_id']:dict(r) for r in db.execute('SELECT * FROM bots')}

def labels(base):
    p=_path(base).resolve()
    try:return _read(str(p),p.stat().st_mtime_ns)
    except FileNotFoundError:return {}

def confirmed(base,uid):return labels(base).get(str(uid),{}).get('status')=='confirmed'

def cooling(base,uid,now=None):
    return confirmed(base,uid) and labels(base)[str(uid)]['cooldown']>(time.time() if now is None else now)

def platform(base,uid,nickname,session):
    old=labels(base).get(str(uid),{})
    if old.get('status')=='confirmed':return
    with _db(base) as db:
        db.execute('''INSERT INTO bots(user_id,status,source,nickname,session,reason,updated)
            VALUES (?,'confirmed','platform',?,?,'QQ bot flag',?) ON CONFLICT(user_id) DO UPDATE SET
            status='confirmed',source='platform',nickname=excluded.nickname,session=excluded.session,updated=excluded.updated''',
            (str(uid),nickname,session,time.time()))

def propose(base,payload,name,reason=''):
    from hakimi.transport.member_mentions import resolve
    result=resolve(base,payload,name)
    if not result['ok']:return result
    member=result['member'];uid=member['user_id'];old=labels(base).get(uid,{})
    reason=reason.strip()[:240]
    if old.get('status') in {'confirmed','pending'} or old.get('reason')==reason:
        return {'ok':True,'status':old.get('status'),**member}
    with _db(base) as db:
        db.execute('''INSERT INTO bots(user_id,status,source,nickname,session,reason,updated)
            VALUES (?,'pending','dsh',?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            status='pending',source='dsh',nickname=excluded.nickname,session=excluded.session,
            reason=excluded.reason,updated=excluded.updated''',
            (uid,member['nickname'],payload['session_key'],reason,time.time()))
    return {'ok':True,'status':'pending',**member}

def target(base,payload,segments=(),reply_to=None):
    if not payload.get('session_key','').startswith('group:'):return ''
    explicit_recipient = False
    for text in segments:
        for uid in AT.findall(text):
            if confirmed(base,uid):return uid
            explicit_recipient = True
    if explicit_recipient:return ''
    from hakimi.core.context_window import select_context
    rows,row=select_context(payload)
    if reply_to is not None:row=next((r for r in rows if str(r.get('message_id'))==str(reply_to)),row)
    uid=str(row.get('user_id',''))
    return uid if confirmed(base,uid) else ''

def guidance(base,payload):
    return '本轮与机器人接话，简短自然，每轮一条文字消息。' if target(base,payload) else ''

def shape(segments,uid):
    # Keep one model-written text bubble, including its chosen quote.
    for text in segments:
        text=AT.sub('',text).strip()
        quote=re.match(r'^\[CQ:reply,id=-?\d+\]',text)
        prefix=quote[0] if quote else '';body=text[len(prefix):].strip()
        if not body or '[CQ:' in body:continue
        if len(body)>80:
            short=body[:80];ends=list(re.finditer(r'[，。！？!?；;…]',short))
            body=short[:ends[-1].end()] if ends and ends[-1].end()>=40 else short
        return [prefix+f'[CQ:at,qq={uid}] '+body]
    return [f'[CQ:at,qq={uid}]'] if any(AT.fullmatch(text.strip()) for text in segments) else []

def delivered(base,uid,receipt,now):
    if not confirmed(base,uid):return False
    with _db(base) as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT rounds,last_reply,cooldown,receipt FROM bots WHERE user_id=?',(str(uid),)).fetchone()
        if row[3]==receipt:return row[2]>now
        rounds=(row[0] if now-row[1]<600 and row[2]<=now and not row[2] else 0)+1
        until=now+600 if rounds>=4 else 0
        db.execute('UPDATE bots SET rounds=?,last_reply=?,cooldown=?,receipt=? WHERE user_id=?',
                   (rounds,now,until,receipt,str(uid)))
    return until>now

def candidates(base):
    pending=sorted((r for r in labels(base).values() if r['status']=='pending'),key=lambda r:r['updated'])[:3]
    if not pending:return []
    path=Path(base)/'state/memory.sqlite3'
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            for index,r in enumerate(pending):
                args=(r['user_id'],)
                count,first,last=db.execute("SELECT COUNT(*),MIN(at),MAX(at) FROM records WHERE user_id=? AND kind='chat' AND session LIKE 'group:%' AND state!='deleted'",args).fetchone()
                r=dict(r);r['history_span']={'count':count,'first':first,'last':last}
                samples=[]
                for order,limit in [('ASC',2),('DESC',4)]:
                    samples.extend(db.execute("SELECT id,nickname,text,at FROM records WHERE user_id=? AND kind='chat' AND session LIKE 'group:%' AND state!='deleted' ORDER BY at "+order+' LIMIT ?',(*args,limit)).fetchall())
                r['samples']=[{'nickname':n,'text':t[:180],'at':at} for i,n,t,at in sorted({row[0]:row for row in samples}.values(),key=lambda row:row[3])]
                pending[index]=r
    return [{k:r.get(k) for k in ('user_id','nickname','session','reason','updated','history_span','samples')} for r in pending]

def apply_reviews(base,reviews,candidates,now):
    allowed={r['user_id']:r for r in candidates};count=0
    with _db(base) as db:
        for r in reviews if isinstance(reviews,list) else []:
            uid=str(r.get('user_id',''));candidate=allowed.get(uid);verdict=r.get('verdict')
            if not candidate or verdict not in {'confirmed','ordinary'}:continue
            count+=db.execute("UPDATE bots SET status=?,source='terra',reason=?,updated=? WHERE user_id=? AND status='pending' AND updated=?",
                (verdict,str(r.get('reason',''))[:240],now,uid,candidate['updated'])).rowcount
    return count
