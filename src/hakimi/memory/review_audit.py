"""Bounded verdict metadata. Never stores full chat material or provider errors."""
import json


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS relationship_review_audit(
        id INTEGER PRIMARY KEY,at REAL,job TEXT,source TEXT,event_id INTEGER,
        message_id TEXT,kind TEXT,valence TEXT,closeness TEXT,outcome TEXT,
        reason TEXT,error_code TEXT)''')
    if 'details' not in {r[1] for r in db.execute('PRAGMA table_info(relationship_review_audit)')}:
        db.execute("ALTER TABLE relationship_review_audit ADD COLUMN details TEXT DEFAULT '{}'")


def record(db, now, *, job='', source='', event_id=None, message_id='', item=None,
           outcome='no_change', reason='', error_code='', details=None):
    initialize(db)
    item = item or {}
    db.execute('INSERT INTO relationship_review_audit(at,job,source,event_id,message_id,kind,valence,closeness,outcome,reason,error_code,details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (now, str(job)[:96], source[:24], event_id, str(message_id)[:80],
                str(item.get('kind', ''))[:24], str(item.get('valence', 'neutral'))[:12],
                str(item.get('closeness', 'unchanged'))[:12], outcome[:24],
                str(reason or item.get('reason', ''))[:160], error_code[:64],
                json.dumps(details or {},ensure_ascii=False,separators=(',',':'))))
    maintain(db,now)


def maintain(db,now):
    initialize(db)
    db.execute('DELETE FROM relationship_review_audit WHERE at<? OR id NOT IN (SELECT id FROM relationship_review_audit ORDER BY id DESC LIMIT 1000)', (now-7*86400,))
