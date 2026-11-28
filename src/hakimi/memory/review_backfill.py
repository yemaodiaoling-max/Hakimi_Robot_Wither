"""Spend durable fresh-review credits on one isolated historical job at a time."""
from pathlib import Path
import time
from uuid import uuid4

from hakimi.memory import review_recovery


JOBS = {
    'window': ('reply_review_windows', 'id'),
    'note': ('impression_jobs', 'original_id'),
    'daily': ('daily_event_jobs', 'source_id'),
}
LEASE_SECONDS = 600
NOT_DISPATCHED = {'REVIEW_BUDGET_PAUSED','REVIEW_DISABLED','REVIEW_API_CONFIG','REVIEW_API_INPUT_TOO_LARGE'}
PAUSE_LANE = {'REVIEW_BUDGET_PAUSED','REVIEW_DISABLED','REVIEW_API_CONFIG','REVIEW_API_QUOTA','REVIEW_API_AUTH'}


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS review_backfill_state(
        id INTEGER PRIMARY KEY CHECK(id=1), credits INTEGER NOT NULL DEFAULT 0,
        token TEXT NOT NULL DEFAULT '', leased_until REAL NOT NULL DEFAULT 0)""")
    db.execute('INSERT OR IGNORE INTO review_backfill_state(id) VALUES (1)')
    db.execute("""CREATE TABLE IF NOT EXISTS review_backfill_jobs(
        kind TEXT NOT NULL, job TEXT NOT NULL, available_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(kind,job))""")


def _has_jobs(db):
    for kind, (table, key) in JOBS.items():
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        statuses = "'error','expired','backfill'" if kind=='daily' else "'error','backfill'"
        readable = ("AND (j.status='backfill' OR EXISTS(SELECT 1 FROM records s WHERE s.id=j.source_id "
                    "AND s.state='active' AND s.text!=''))" if kind=='daily' else "")
        if db.execute(f"""SELECT 1 FROM {table} j WHERE status IN ({statuses})
                {readable}
                AND NOT EXISTS(SELECT 1 FROM review_recovery_jobs r WHERE r.kind=? AND r.job=CAST(j.{key} AS TEXT)
                AND r.status!='pending') LIMIT 1""", (kind,)).fetchone():
            return True
    return False


def grant(db, base):
    """In the transaction that completed a real, ordinary review."""
    from hakimi.curation.review_runtime import sol_backfill_enabled
    if not sol_backfill_enabled(base) or not _has_jobs(db):
        return
    initialize(db)
    db.execute('UPDATE review_backfill_state SET credits=credits+10 WHERE id=1')


def current(db, claim):
    return bool(db.execute('SELECT 1 FROM review_backfill_state WHERE token=?', (claim['token'],)).fetchone())


def debit(index, claim):
    """Debit before dispatch so a killed worker cannot replay a paid attempt for free."""
    with index.db() as db:
        return bool(db.execute('UPDATE review_backfill_state SET credits=credits-1 WHERE token=? AND credits>0',
                               (claim['token'],)).rowcount)


def finish(db, claim):
    """Called in the same transaction as the historical verdict."""
    db.execute('DELETE FROM review_backfill_jobs WHERE kind=? AND job=?', (claim['kind'],claim['job']))
    db.execute("UPDATE review_backfill_state SET token='',leased_until=0 WHERE token=?", (claim['token'],))


def _pick(db, now):
    # Prefer original material that still exists. Daily jobs with expired
    # source text can be marked in bounded passes after usable jobs.
    candidates = []
    queries = {
        'daily': """SELECT CAST(j.source_id AS TEXT) job,j.created_at at,
                   COALESCE(r.session,'') session,COALESCE(r.user_id,'') target,
                   COALESCE(r.state='active' AND r.text!='',0) readable
                   FROM daily_event_jobs j LEFT JOIN records r ON r.id=j.source_id
                   WHERE j.status IN ('error','expired','backfill')
                   AND (j.status='backfill' OR (r.state='active' AND r.text!=''))""",
        'note': """SELECT CAST(j.original_id AS TEXT) job,j.noted_at at,j.session,j.target,
                  COALESCE(r.state NOT IN ('deleted','retracted') AND r.text!='',0) readable
                  FROM impression_jobs j LEFT JOIN records r ON r.id=j.record_id
                  WHERE j.status IN ('error','backfill')""",
        'window': """SELECT j.id job,j.created_at at,j.session,'' target,1 readable
                    FROM reply_review_windows j WHERE j.status IN ('error','backfill')""",
    }
    for kind, (table, _) in JOBS.items():
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        row = db.execute(f"""SELECT p.*,COALESCE(b.available_at,0)>0 retried FROM ({queries[kind]}) p
            LEFT JOIN review_recovery_jobs r ON r.kind=? AND r.job=p.job
            LEFT JOIN review_backfill_jobs b ON b.kind=? AND b.job=p.job
            WHERE (r.id IS NULL OR (r.status='pending' AND COALESCE(b.available_at,0)<=?))
            ORDER BY p.readable DESC,retried,p.at,p.job LIMIT 1""", (kind,kind,now)).fetchone()
        if row:
            candidates.append(dict(row, kind=kind))
    return min(candidates, key=lambda item: (-item['readable'],item['retried'],item['at'],item['kind'],item['job']), default=None)


def _claim(index, now):
    with index.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='review_backfill_state'").fetchone():
            return None
        state = db.execute('SELECT credits,leased_until FROM review_backfill_state WHERE id=1').fetchone()
        if not state or state['credits'] <= 0 or state['leased_until'] > now:
            return None
        db.execute('BEGIN IMMEDIATE')
        state = db.execute('SELECT credits,leased_until FROM review_backfill_state WHERE id=1').fetchone()
        if state['credits'] <= 0 or state['leased_until'] > now:
            return None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='reply_review_windows'").fetchone() and db.execute(
            "SELECT 1 FROM reply_review_windows WHERE status IN ('ready','retry') AND next_attempt<=? LIMIT 1",(now,)).fetchone():
            return None
        review_recovery.ensure_metadata(db)
        claim = _pick(db, now)
        if claim is None:
            if not db.execute('SELECT 1 FROM review_backfill_jobs LIMIT 1').fetchone():
                db.execute("UPDATE review_backfill_state SET credits=0,token='',leased_until=0 WHERE id=1")
            return None
        kind, job = claim['kind'], claim['job']
        table, key = JOBS[kind]
        event_id = int(job) if kind == 'daily' else None
        from hakimi.ops.recover_reviews import _original_time
        at, basis = _original_time(db,kind,job,claim['at'],event_id)
        db.execute("""INSERT OR IGNORE INTO review_recovery_jobs(kind,job,session,target,
            original_review_at,time_basis,status,reason,prepared_at)
            VALUES (?,?,?,?,?,?,'pending','等待历史补评',?)""",
            (kind,job,claim['session'],claim['target'],at,basis,now))
        db.execute('INSERT OR IGNORE INTO review_backfill_jobs(kind,job) VALUES (?,?)', (kind,job))
        if not claim['readable']:
            review_recovery.complete_metadata(db,kind,job,now,'source_unavailable','原始来源不足，本次补记未计分')
            db.execute(f"UPDATE {table} SET status='done' WHERE {key}=?", (job,))
            db.execute('DELETE FROM review_backfill_jobs WHERE kind=? AND job=?', (kind,job))
            return {'skipped': True}
        db.execute(f"UPDATE {table} SET status='backfill' WHERE {key}=?", (job,))
        token = uuid4().hex
        db.execute('UPDATE review_backfill_state SET token=?,leased_until=? WHERE id=1', (token,now+LEASE_SECONDS))
        return dict(claim, token=token)


def _defer(index, claim, result, now):
    with index.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not current(db,claim):
            return
        refunded = result.get('reason') in NOT_DISPATCHED
        pause_lane = result.get('reason') in PAUSE_LANE
        delay = 900 if pause_lane else 300
        db.execute('UPDATE review_backfill_jobs SET available_at=? WHERE kind=? AND job=?',
                   (now+delay,claim['kind'],claim['job']))
        # A bad historical job never heads the foreground queue or the next
        # historical pass. Quota exhaustion pauses only the historical lane.
        pause = now+delay if pause_lane else 0
        db.execute("UPDATE review_backfill_state SET credits=credits+?,token='',leased_until=? WHERE id=1",
                   (int(refunded),pause))


def tick(base):
    from hakimi.curation import impression_events, post_reply_review, review_runtime
    from hakimi.memory.memory_store import store
    if not review_runtime.sol_backfill_enabled(base):
        return {'status':'idle'}
    index = store(str(Path(base).resolve()))
    with index.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='review_backfill_state'").fetchone():
            initialize(db)
    for _ in range(20):
        claim = _claim(index,time.time())
        if not claim:
            return {'status':'idle'}
        if not claim.get('skipped'):
            break
    else:
        return {'status':'skipped'}
    try:
        worker = post_reply_review.tick if claim['kind']=='window' else impression_events.tick
        result = worker(base,backfill=claim)
    except Exception:
        _defer(index,claim,{},time.time())
        raise
    _defer(index,claim,result,time.time())
    return result
