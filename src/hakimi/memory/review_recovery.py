"""Shared metadata for explicitly requested historical review recovery."""


def ensure_metadata(db):
    db.execute("""CREATE TABLE IF NOT EXISTS review_recovery_jobs(
        id INTEGER PRIMARY KEY,kind TEXT NOT NULL,job TEXT NOT NULL,
        session TEXT DEFAULT '',target TEXT DEFAULT '',original_review_at REAL NOT NULL,
        time_basis TEXT NOT NULL,status TEXT NOT NULL,reason TEXT DEFAULT '',
        prepared_at REAL,completed_at REAL,UNIQUE(kind,job))""")
    if 'result' not in {r[1] for r in db.execute('PRAGMA table_info(review_recovery_jobs)')}:
        db.execute("ALTER TABLE review_recovery_jobs ADD COLUMN result TEXT DEFAULT '{}'")
    db.execute('CREATE INDEX IF NOT EXISTS review_recovery_time ON review_recovery_jobs(original_review_at,id)')


def load_metadata(db,kind,job):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_recovery_jobs'").fetchone():
        return None
    row=db.execute('SELECT * FROM review_recovery_jobs WHERE kind=? AND job=?',(kind,str(job))).fetchone()
    if row is None:return None
    result=dict(row);result['original_time_basis']=result['time_basis']
    return result


def complete_metadata(db,kind,job,now,status='done',reason=''):
    if load_metadata(db,kind,job) is None:return
    if status not in {'pending','done','source_unavailable','already_reviewed'}:
        raise ValueError('invalid_recovery_status')
    if not reason:
        previous=load_metadata(db,kind,job)
        reason='已按原复盘时间补记完成' if status=='done' else previous.get('reason','')
    db.execute('UPDATE review_recovery_jobs SET status=?,completed_at=?,reason=? WHERE kind=? AND job=?',
               (status,now,str(reason)[:240],kind,str(job)))
    from hakimi.memory.review_results import snapshot_result
    snapshot_result(db,kind,job)


def review_clock(db,kind,job,now):
    metadata=load_metadata(db,kind,job)
    return metadata['original_review_at'] if metadata else now


def eligible_event(db,event_id,kind=None,job=None):
    """Recovery never revises settled scores; not-selected remains eligible."""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('impression_effects','relationship_review_audit')")}
    if 'impression_effects' in tables and db.execute('SELECT 1 FROM impression_effects WHERE event_id=? LIMIT 1',(event_id,)).fetchone():
        return False
    if 'relationship_review_audit' in tables and db.execute("SELECT 1 FROM relationship_review_audit WHERE event_id=? AND COALESCE(error_code,'')='' AND outcome IN ('applied','revised','no_change') LIMIT 1",(event_id,)).fetchone():
        return False
    return True
