"""Local public-source receipts, bound only after a reply is committed."""
from contextlib import contextmanager
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import time

from hakimi.core.context_window import select_context


@contextmanager
def database(base):
    path=Path(base)/'state/web_sources.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=2);db.row_factory=sqlite3.Row
    try:
        db.executescript('''CREATE TABLE IF NOT EXISTS sources (
            session TEXT,trigger TEXT,label TEXT,url TEXT,data TEXT,
            PRIMARY KEY(session,trigger,label),UNIQUE(session,trigger,url));
            CREATE TABLE IF NOT EXISTS replies (
            session TEXT,reply TEXT,trigger TEXT,labels TEXT,created REAL,
            PRIMARY KEY(session,reply));
            CREATE TABLE IF NOT EXISTS messages (
            session TEXT,message TEXT,reply TEXT,PRIMARY KEY(session,message));
            CREATE TABLE IF NOT EXISTS turns (
            session TEXT,trigger TEXT,calls INTEGER,deadline REAL,presearch TEXT,
            PRIMARY KEY(session,trigger));''')
        columns={row[1] for row in db.execute('PRAGMA table_info(sources)')}
        if 'expires' not in columns:db.execute('ALTER TABLE sources ADD COLUMN expires REAL DEFAULT 0')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM sources WHERE expires<?',(time.time(),))
        yield db;db.commit()
    except BaseException:db.rollback();raise
    finally:db.close()


def identity(payload):
    return str(payload.get('session_key') or ''),str(payload.get('trigger_id') or '')


def record(base,payload,source):
    session,trigger=identity(payload)
    if not session or not trigger:return dict(source)
    with database(base) as db:
        old=db.execute('SELECT label,data FROM sources WHERE session=? AND trigger=? AND url=?',
                       (session,trigger,source['url'])).fetchone()
        label=old['label'] if old else 'W'+str(db.execute('SELECT count(*) FROM sources WHERE session=? AND trigger=?',(session,trigger)).fetchone()[0]+1)
        data=json.loads(old['data']) if old else {}
        if data.get('evidence_level')=='page_read' and source.get('evidence_level')=='search_snippet':
            return data
        data.update(source);data['source_id']=label
        if source.get('text'):
            data['content_sha256']=hashlib.sha256(source['text'].encode()).hexdigest()
        db.execute('INSERT OR REPLACE INTO sources (session,trigger,label,url,data,expires) VALUES (?,?,?,?,?,?)',
                   (session,trigger,label,source['url'],json.dumps(data,ensure_ascii=False),time.time()+86400))
    return data


def commit(base,record):
    """Called after durable outbox append; drafts and silent turns never bind."""
    session=str(record.get('session_key') or '');trigger=str(record.get('trigger_id') or '')
    labels=set(re.findall(r'\[(W[1-9][0-9]*)\]',html.unescape('\n'.join(record.get('segments',[])))))
    if not session or not trigger:return
    with database(base) as db:
        candidates={row['label']:json.loads(row['data']) for row in db.execute('SELECT label,data FROM sources WHERE session=? AND trigger=?',(session,trigger))}
        known=set(candidates)
        labels=sorted(labels & known)
        references=sorted(known)
        if references:db.execute('INSERT OR IGNORE INTO replies VALUES (?,?,?,?,?)',
            (session,record['reply_id'],trigger,json.dumps({'labels':labels,'sources':[dict(candidates[label],text=str(candidates[label].get('text',''))[:1200],snapshot_excerpt=True,attribution='explicitly_cited' if label in labels else 'turn_reference_not_fact_verification') for label in references]},ensure_ascii=False),record.get('created_at',time.time())))


def delivered(base,session,message,reply):
    if not message or not reply:return
    with database(base) as db:
        if db.execute('SELECT 1 FROM replies WHERE session=? AND reply=?',(session,str(reply))).fetchone():
            db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?)',(session,str(message),str(reply)))


def lookup(base,payload,message_id=None):
    session,_=identity(payload)
    _,target=select_context(payload)
    explicit=str(message_id or target.get('reply_to') or '')
    with database(base) as db:
        if explicit:
            reply=db.execute('SELECT reply FROM messages WHERE session=? AND message=?',(session,explicit)).fetchone()
            row=db.execute('SELECT * FROM replies WHERE session=? AND reply=?',(session,reply[0])).fetchone() if reply else None
        else:
            row=db.execute('SELECT r.* FROM replies r JOIN messages m ON m.session=r.session AND m.reply=r.reply WHERE r.session=? ORDER BY r.created DESC LIMIT 1',(session,)).fetchone()
        if not row:return {'sources':[],'status':'no_committed_sources_for_this_reply'}
        snapshot=json.loads(row['labels'])
        if isinstance(snapshot,dict):sources=snapshot.get('sources',[])
        else:
            labels=set(snapshot)
            sources=[json.loads(item[0]) for item in db.execute('SELECT data FROM sources WHERE session=? AND trigger=?',(session,row['trigger']))
                     if json.loads(item[0]).get('source_id') in labels]
        sources=[dict(item,text=str(item.get('text',''))[:1800]) for item in sources[:6]]
    return {'reply_id':row['reply'],'sources':sources,'status':'submitted_and_delivered','network_used':False}


def cached(base,payload,url):
    session,trigger=identity(payload)
    with database(base) as db:
        rows=list(db.execute('SELECT data FROM sources WHERE session=? AND trigger=? AND url=?',(session,trigger,url)))
        if not rows:
            snapshots=list(db.execute('''SELECT r.labels FROM replies r JOIN messages m
                ON m.session=r.session AND m.reply=r.reply
                WHERE r.session=? ORDER BY r.created DESC LIMIT 30''',(session,)))
            rows=[]
            for snapshot in snapshots:
                value=json.loads(snapshot[0])
                if isinstance(value,dict):rows.extend((json.dumps(item),) for item in value.get('sources',[]) if item.get('url')==url)
        for row in rows:
            data=json.loads(row[0])
            if data.get('evidence_level')=='page_read' and data.get('text') and not data.get('snapshot_excerpt'):
                return dict(data,cached=True,network_used=False)
    return None


def reserve_access(base,payload,*,max_calls=6,total_seconds=40,timeout=15):
    session,trigger=identity(payload)
    if not session or not trigger:raise ValueError('web_turn_identity_required')
    now=time.time()
    with database(base) as db:
        db.execute('INSERT OR IGNORE INTO turns VALUES (?,?,0,?,NULL)',(session,trigger,now+total_seconds))
        row=db.execute('SELECT calls,deadline FROM turns WHERE session=? AND trigger=?',(session,trigger)).fetchone()
        remaining=min(float(timeout),row['deadline']-now)
        if row['calls']>=max_calls or remaining<.2:raise TimeoutError('web_turn_budget_exhausted')
        db.execute('UPDATE turns SET calls=calls+1 WHERE session=? AND trigger=?',(session,trigger))
    return remaining
