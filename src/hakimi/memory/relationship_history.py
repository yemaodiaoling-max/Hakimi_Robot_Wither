"""Compact score-change history; permanent effects remain the deduplication ledger."""
from datetime import datetime
from zoneinfo import ZoneInfo

DETAIL_DAYS=30
DETAIL_LIMIT=1000
DAILY_DAYS=365
DAILY_LIMIT=3000
CLEANUP_BATCH=200


def initialize(db):
    schema='''
    CREATE TABLE IF NOT EXISTS relationship_change_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL,scope TEXT,target TEXT,session TEXT,
        event_id INTEGER,original_id INTEGER,source TEXT,revision INTEGER,
        affinity_before INTEGER,affinity_after INTEGER,affinity_delta INTEGER,
        closeness_before INTEGER,closeness_after INTEGER,closeness_delta INTEGER,reason TEXT);
    CREATE INDEX IF NOT EXISTS relationship_change_time ON relationship_change_log(at DESC,id DESC);
    CREATE INDEX IF NOT EXISTS relationship_change_event ON relationship_change_log(scope,target,event_id);
    CREATE INDEX IF NOT EXISTS relationship_change_person ON relationship_change_log(target,session,at DESC);
    CREATE TABLE IF NOT EXISTS relationship_change_daily(
        scope TEXT,target TEXT,session TEXT,source TEXT,day TEXT,at REAL,
        events INTEGER,revisions INTEGER,affinity_delta INTEGER,closeness_delta INTEGER,
        affinity_before INTEGER,affinity_after INTEGER,closeness_before INTEGER,closeness_after INTEGER,
        first_at REAL,first_id INTEGER,last_at REAL,last_id INTEGER,reason TEXT,
        PRIMARY KEY(scope,target,session,source,day));
    CREATE INDEX IF NOT EXISTS relationship_daily_time ON relationship_change_daily(at DESC);
    CREATE INDEX IF NOT EXISTS relationship_daily_person ON relationship_change_daily(target,session,at DESC);
    CREATE INDEX IF NOT EXISTS impression_effect_time ON impression_effects(updated DESC);
    CREATE INDEX IF NOT EXISTS impression_effect_reason_expiry ON impression_effects(updated) WHERE reason!='';
    '''
    for statement in schema.split(';'):
        if statement.strip():
            db.execute(statement)


def record(db,job,event,before,after,now,*,revision=False):
    initialize(db)
    source='note' if job['record_id'] else job.get('review_source','daily')
    source=source if source in {'note','window','daily'} else 'daily'
    values=[]
    for field,anchor in (('affinity',50),('closeness',0)):
        old=before.get(field);new=after.get(field)
        delta=(new if new is not None else anchor)-(old if old is not None else anchor)
        values.extend((old,new,delta))
    reason=str(event.get('reason',''))[:160]
    scope='groups' if job['session'].startswith('group:') else job['session']
    cur=db.execute('INSERT INTO relationship_change_log(at,scope,target,session,event_id,original_id,source,revision,affinity_before,affinity_after,affinity_delta,closeness_before,closeness_after,closeness_delta,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (now,scope,str(job['target']),job['session'],event['event_id'],job['original_id'],source,int(revision),*values,reason))
    key=(scope,str(job['target']),job['session'],source,datetime.fromtimestamp(now,ZoneInfo('America/New_York')).strftime('%Y-%m-%d'))
    old=db.execute('SELECT * FROM relationship_change_daily WHERE scope=? AND target=? AND session=? AND source=? AND day=?',key).fetchone()
    if old:
        first=(now,cur.lastrowid)<(old['first_at'],old['first_id'])
        last=(now,cur.lastrowid)>(old['last_at'],old['last_id'])
        db.execute('UPDATE relationship_change_daily SET at=?,events=events+1,revisions=revisions+?,affinity_delta=affinity_delta+?,closeness_delta=closeness_delta+?,affinity_before=?,closeness_before=?,affinity_after=?,closeness_after=?,first_at=?,first_id=?,last_at=?,last_id=?,reason=? WHERE scope=? AND target=? AND session=? AND source=? AND day=?',
                   (now if last else old['at'],int(revision),values[2],values[5],values[0] if first else old['affinity_before'],values[3] if first else old['closeness_before'],
                    values[1] if last else old['affinity_after'],values[4] if last else old['closeness_after'],now if first else old['first_at'],
                    cur.lastrowid if first else old['first_id'],now if last else old['last_at'],cur.lastrowid if last else old['last_id'],reason if last else old['reason'],*key))
    else:
        db.execute('INSERT INTO relationship_change_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (*key,now,1,int(revision),values[2],values[5],values[0],values[1],values[3],values[4],now,cur.lastrowid,now,cur.lastrowid,reason))


def maintain(db,now):
    """Existing five-minute maintenance; bounded deletes, no score/ledger deletion."""
    initialize(db)
    for table,limit,days in (('relationship_change_log',DETAIL_LIMIT,DETAIL_DAYS),('relationship_change_daily',DAILY_LIMIT,DAILY_DAYS)):
        # rowid is metadata only; ordering uses actual time then append order.
        ids={r[0] for r in db.execute('SELECT rowid FROM '+table+' WHERE at<? ORDER BY at,rowid LIMIT ?', (now-days*86400,CLEANUP_BATCH))}
        ids.update(r[0] for r in db.execute('SELECT rowid FROM '+table+' ORDER BY at DESC,rowid DESC LIMIT ? OFFSET ?', (CLEANUP_BATCH,limit)))
        if ids:
            db.executemany('DELETE FROM '+table+' WHERE rowid=?',[(i,) for i in sorted(ids)[:CLEANUP_BATCH]])
    # Keep exact-source IDs and settled deltas forever, while retiring prose.
    rows=db.execute("SELECT scope,target,event_id FROM impression_effects WHERE reason!='' AND updated<? ORDER BY updated LIMIT ?",(now-DETAIL_DAYS*86400,CLEANUP_BATCH)).fetchall()
    db.executemany("UPDATE impression_effects SET reason='' WHERE scope=? AND target=? AND event_id=?",[tuple(row) for row in rows])
    from hakimi.memory.review_audit import maintain as maintain_audit
    maintain_audit(db,now)
