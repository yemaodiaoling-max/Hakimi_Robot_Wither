"""Local rolling activity retention; never touches evidence, notes or cost ledgers."""
import json
import logging
import math

RETENTION_SECONDS=30*86400
SCHEMAS={
    'watcher_state': {'id','value'},
    'watcher_events': {'at','kind','details'},
    'video_history': {'bvid','selected_at','updated_at','selection_reason','observations','interest','decision','summary','coverage','evidence_ids','status'},
    'video_selection_hints': {'bvid','topic','source'},
    'video_selection_audit': {'request_id','at','bvid','reason','qq_context_json'},
    'video_comment_attempts': {'account','bvid','session','text','at','day','state','uploader_id'},
    'comment_receipts': {'account','bvid','oid','rpid','text','at','visibility','checked_at'},
    'comment_reply_attempts': {'account','bvid','root','parent','author','incoming','text','at','day','state','rpid','request_id'},
    'qq_memory_reads': {'id','read_at','keyword','status','records_json'},
    'activity_retention_state': {'scope','last_at','observed_retired'},
}


def retained_observation_count(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='activity_retention_state' AND type='table'").fetchone():
        return 0
    if {r[1] for r in db.execute('PRAGMA table_info(activity_retention_state)')}!=SCHEMAS['activity_retention_state']:
        logging.getLogger(__name__).warning('activity_retention_count_unavailable error_type=SchemaMismatch')
        return 0  # A smaller denominator increases pacing conservatively; keep watching.
    row=db.execute("SELECT observed_retired FROM activity_retention_state WHERE scope='watcher'").fetchone()
    value=row[0] if row else 0
    return value if isinstance(value,int) and value>=0 else 0


def prune_activity(database,now,*,scope):
    """Run once per elapsed day, atomically; incompatible schemas leave all rows intact."""
    if scope not in {'watcher','bridge'} or not math.isfinite(now):
        raise ValueError('invalid retention arguments')
    try:
        with database.connect(write=True) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            targets=({'watcher_state','watcher_events','video_history','video_selection_hints',
                      'video_selection_audit','video_comment_attempts','comment_receipts','comment_reply_attempts'} if scope=='watcher' else {'qq_memory_reads'})
            for name in (targets|{'activity_retention_state'}) & tables:
                if {r[1] for r in db.execute('PRAGMA table_info('+name+')')}!=SCHEMAS[name]:
                    raise ValueError('retention_schema_mismatch')
            db.execute('CREATE TABLE IF NOT EXISTS activity_retention_state (scope TEXT PRIMARY KEY,last_at REAL,observed_retired INTEGER)')
            prior=db.execute('SELECT last_at,observed_retired FROM activity_retention_state WHERE scope=?',(scope,)).fetchone()
            if prior and now-prior[0]<86400:return {'state':'not_due','next_at':prior[0]+86400}
            unsettled=set()
            if scope=='watcher' and 'budget' in tables:
                if not {'id','state'}<={r[1] for r in db.execute('PRAGMA table_info(budget)')}:
                    raise ValueError('retention_schema_mismatch')
                unsettled={r[0] for r in db.execute("SELECT id FROM budget WHERE state!='settled' OR state IS NULL")}
            cutoff=now-RETENTION_SECONDS
            counts={}
            retired=prior[1] if prior else 0
            if scope=='bridge':
                if 'qq_memory_reads' in tables:
                    counts['qq_memory_reads']=db.execute('DELETE FROM qq_memory_reads WHERE read_at<?',(cutoff,)).rowcount
            else:
                protected=set()
                if 'video_history' in tables and 'watcher_state' not in tables:
                    raise ValueError('retention_state_missing')
                if 'watcher_state' in tables:
                    row=db.execute('SELECT value FROM watcher_state WHERE id=1').fetchone()
                    if not row:raise ValueError('retention_state_missing')
                    state=json.loads(row[0])
                    for key in ('video','bookmark'):
                        if state.get(key):protected.add(state[key]['bvid'])
                if 'video_history' in tables:
                    old=[r[0] for r in db.execute("SELECT bvid FROM video_history WHERE updated_at<? AND (selection_reason!='' OR summary!='' OR coverage!='[]' OR evidence_ids!='[]' OR decision!='')",(cutoff,)) if r[0] not in protected]
                    for bvid in old:
                        db.execute("UPDATE video_history SET selection_reason='',summary='',coverage='[]',evidence_ids='[]',decision='' WHERE bvid=?",(bvid,))
                        if 'video_selection_hints' in tables:db.execute('DELETE FROM video_selection_hints WHERE bvid=?',(bvid,))
                    counts['video_history_compacted']=len(old)
                if 'video_selection_audit' in tables:
                    for row in db.execute('SELECT request_id,bvid FROM video_selection_audit WHERE at<?',(cutoff,)).fetchall():
                        if row[1] not in protected and row[0] not in unsettled:
                            db.execute('DELETE FROM video_selection_audit WHERE request_id=?',(row[0],))
                if 'video_comment_attempts' in tables:
                    for row in db.execute("SELECT account,bvid FROM video_comment_attempts WHERE at<? AND state IN ('sent','rejected','cancelled','expired')",(cutoff,)).fetchall():
                        if row[1] not in protected:
                            db.execute("UPDATE video_comment_attempts SET text='',session='' WHERE account=? AND bvid=?",tuple(row))
                if 'watcher_events' in tables:
                    # Model-role events remain part of the operator's billing audit.
                    # All ledger/call/action audit tables remain completely untouched.
                    retired+=db.execute("SELECT count(*) FROM watcher_events WHERE at<? AND kind='observed'",(cutoff,)).fetchone()[0]
                    counts['watcher_events']=db.execute("DELETE FROM watcher_events WHERE at<? AND kind!='model_role'",(cutoff,)).rowcount
                if 'comment_receipts' in tables:
                    db.execute("UPDATE comment_receipts SET text='' WHERE at<?",(cutoff,))
                if 'comment_reply_attempts' in tables:
                    # Keep identity tombstones and cost links to prevent replay.
                    db.execute("UPDATE comment_reply_attempts SET incoming='',text='' WHERE at<?",(cutoff,))
            db.execute('INSERT OR REPLACE INTO activity_retention_state VALUES (?,?,?)',(scope,now,retired))
            return {'state':'pruned','counts':counts,'next_at':now+86400}
    except Exception as exc:
        logging.getLogger(__name__).warning('activity_retention_failed scope=%s error_type=%s',scope,type(exc).__name__)
        return {'state':'failed','error_type':type(exc).__name__}
