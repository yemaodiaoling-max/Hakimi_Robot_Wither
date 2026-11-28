"""Bounded, incremental event cards, sharing the notebook's DSH judgment batch.

Cards describe observed conversation, never replace personal impressions. Source
metadata stays local after raw-chat expiry; the existing indexer embeds cards.
"""
import hashlib
import json
import re

from hakimi.memory.text import terms

CURSOR = 'daily_events_v1'
MAX_PENDING = 24
MAX_AGE = 7200
INSTRUCTION = (
    'daily_events与relationship_events是同一JSON对象的顶层字段。'
    '从daily_messages整理最多2张日常事件卡，选具体经历、约定、帮助、误会/和解或持续话题，无合适事件返回空列表。'
    '核对author/author_name、mentions/mention_names和reply_author/reply_author_name，self_id或SELF代表哈基米，明确@优先于引用，群友互聊严格按每条消息的author_name区分各自言行、把引用正文里的被引用人与当前发言人明确分开，优先写昵称。'
    '每卡text限140字内，写清人物、言行与先后，转述和引用保留归属；旧事按原文明示时间理解，event_time摘录原文明确时间短语，未知则空字符串。'
    '输出daily_events:[{"source_ids":[原文id],"text":"短事件","event_time":"原文时间短语或空"}]，同卡来源归属同一session。'
    '同时独立返回relationship_events(0..2条，默认空列表)：'
    '[{"event_id":来源id,"kind":"relationship","directed_to_hakimi":true,"event_type":"类型",'
    '"evidence":"原文片段","reaction_type":"反应类型","reaction_source":"reply","reaction_evidence":"回复或笔记片段","reason":"简述"}]。'
    '确认作者是在对哈基米说话，隐式互动须有邻近SELF实际发言；每段短对话最多选1条代表性消息。'
)




def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS daily_event_jobs(
        source_id INTEGER PRIMARY KEY,created_at REAL,status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS daily_event_cards(
        record_id INTEGER PRIMARY KEY,observed_at REAL,recorded_at REAL,
        event_time TEXT DEFAULT '',sources TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS daily_event_jobs_pending ON daily_event_jobs(status,next_attempt,source_id)')
    columns = {r[1] for r in db.execute('PRAGMA table_info(daily_event_jobs)')}
    if 'priority' not in columns:
        db.execute('ALTER TABLE daily_event_jobs ADD COLUMN priority INTEGER DEFAULT 0')
    # Existing prune batches predate the priority field; preserve their work,
    # but stop letting a cleanup backlog occupy live interaction slots.
    db.execute("UPDATE daily_event_jobs SET priority=1 WHERE status='pending' AND EXISTS(SELECT 1 FROM records r WHERE r.id=source_id AND r.at<created_at-?)", (MAX_AGE,))
    # Start forward. Only prune's small final batch may inspect pre-existing rows.
    db.execute("INSERT OR IGNORE INTO cursors(name,identity,offset) "
               "SELECT ?,'daily',COALESCE(MAX(id),0) FROM records", (CURSOR,))


def _candidate(row):
    if row['kind'] != 'chat' or row['state'] != 'active' or not row['origin']:
        return False
    text = re.sub(r'\[CQ:[^\]]*\]|https?://\S+|\[(?:群友发图|表情包图片)[^\]]*\]', '', row['text']).strip()
    return len(text) >= 8 and len(terms(text)) >= 3 and not re.fullmatch(r'(.)\1{5,}', text)


def _stage(db, rows, now, *, priority=None):
    # Cleanup gets four independent low-priority slots, never the live quota.
    counts={0:0,1:0}
    for item in db.execute("SELECT priority,COUNT(*) FROM daily_event_jobs WHERE status='pending' GROUP BY priority"):
        counts[item[0]]=item[1]
    seen = set(); through = None
    for row in rows:
        lane = priority if priority is not None else int(row['at'] < now-MAX_AGE)
        limit = 4 if lane else MAX_PENDING
        fingerprint = (row['session'], row['user_id'], row['text'].strip())
        from hakimi.memory.relationship_events import candidate
        eligible = fingerprint not in seen and (_candidate(row) or (row['kind']=='chat' and row['state']=='active' and row['origin'] and candidate(db,row)))
        existing = db.execute('SELECT status FROM daily_event_jobs WHERE source_id=?', (row['id'],)).fetchone()
        # Stop BEFORE an unqueued live candidate. Old cleanup is best-effort;
        # a full cleanup lane must not prevent discovery of fresh messages.
        if eligible and not existing and counts[lane] >= limit:
            if not lane or priority is not None:
                break
            through=row['id'];continue
        through = row['id']
        if not eligible or existing:
            continue
        seen.add(fingerprint)
        result = db.execute('INSERT OR IGNORE INTO daily_event_jobs(source_id,created_at,priority) VALUES (?,?,?)',
                            (row['id'], now, lane))
        counts[lane] += result.rowcount
    return through


def discover(db, now):
    initialize(db)
    db.execute("UPDATE daily_event_jobs SET status='expired' WHERE status='pending' AND created_at<?",
               (now - MAX_AGE,))
    db.execute("UPDATE daily_event_jobs SET status='expired' WHERE status='pending' AND NOT EXISTS(SELECT 1 FROM records r WHERE r.id=source_id AND r.state='active' AND r.text!='')")
    after = db.execute('SELECT offset FROM cursors WHERE name=?', (CURSOR,)).fetchone()[0]
    rows = db.execute("SELECT * FROM records WHERE id>? AND kind='chat' ORDER BY id LIMIT 120",
                      (after,)).fetchall()
    through = _stage(db, rows, now)
    if through is not None:
        db.execute('UPDATE cursors SET offset=? WHERE name=?', (through, CURSOR))


def protect_before_prune(db, old_ids, now):
    """DB-only bounded grace period; never invoke a model on the read/sync path."""
    initialize(db)
    db.execute("UPDATE daily_event_jobs SET status='expired' WHERE status='pending' AND created_at<?",
               (now - MAX_AGE,))
    # Inspect at most one small batch, not the historical archive.
    sample = list(old_ids)[:120]
    if sample:
        marks = ','.join('?' for _ in sample)
        rows = db.execute('SELECT * FROM records WHERE id IN (' + marks + ') ORDER BY at DESC,id DESC', sample).fetchall()
        _stage(db, rows, now, priority=1)
    return {r[0] for r in db.execute("SELECT j.source_id FROM daily_event_jobs j JOIN records r ON r.id=j.source_id "
                                   "WHERE j.status='pending' AND j.attempts<4 AND j.created_at>=? "
                                   "AND r.state='active' AND r.text!=''", (now - MAX_AGE,))}


def _resolve_nicknames(db, session, user_ids):
    resolved = {}
    for uid in {str(u or '').strip() for u in user_ids}:
        if not uid or uid == 'SELF':
            continue
        hit = db.execute(
            "SELECT nickname FROM records WHERE kind='chat' AND session=? AND user_id=? AND nickname!='' "
            "ORDER BY at DESC, id DESC LIMIT 1",
            (session, uid),
        ).fetchone()
        if not hit or not str(hit['nickname'] or '').strip():
            hit = db.execute(
                "SELECT nickname FROM records WHERE kind='chat' AND user_id=? AND nickname!='' "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (uid,),
            ).fetchone()
        if hit and str(hit['nickname'] or '').strip():
            resolved[uid] = str(hit['nickname']).strip()
    return resolved


def prepare(db, now, *, with_impressions=False, exclude_recovery=False):
    discover(db, now)
    ordinary = ("AND NOT EXISTS(SELECT 1 FROM review_recovery_jobs v WHERE v.kind='daily' AND v.job=CAST(j.source_id AS TEXT)) "
                if exclude_recovery else "")
    rows = db.execute("SELECT r.*,j.priority AS review_priority,j.attempts AS review_attempts FROM daily_event_jobs j JOIN records r ON r.id=j.source_id "
                      "WHERE j.status='pending' AND j.next_attempt<=? AND r.state='active' AND r.text!='' " + ordinary +
                      "ORDER BY j.priority,(j.attempts>0) DESC,r.at DESC,r.id DESC LIMIT 12", (now,)).fetchall()
    last = db.execute("SELECT value FROM memory_meta WHERE key='daily_events_last_attempt'").fetchone()
    oldest = db.execute("SELECT MIN(created_at) FROM daily_event_jobs WHERE status='pending'").fetchone()[0]
    if not rows:
        return []
    retry = any(row['review_attempts'] for row in rows)
    if not with_impressions and not retry:
        if len(rows) < 2 and now - float(oldest or now) < 1800:
            return []
        if last and now - float(last[0]) < 1800:
            return []
    result = []
    size = 0
    for row in rows:
        reply = db.execute("SELECT id,user_id,nickname,at,text,state FROM records WHERE kind='chat' AND session=? AND origin=? LIMIT 1",
                           (row['session'], row['reply_to'])).fetchone() if row['reply_to'] else None
        try:
            mentions = json.loads(row['direct_targets'] or '[]')
        except (ValueError, TypeError):
            mentions = []
        if not isinstance(mentions, list):
            mentions = []
        if not row['direct_targets']:
            mentions = re.findall(r'\[CQ:at,qq=(\d+)\]', row['text'])
        nicks = _resolve_nicknames(db, row['session'], [row['user_id'], reply['user_id'] if reply else '', *mentions[:12]])
        author_name = str(row['nickname'] or '').strip() or nicks.get(str(row['user_id']), '')
        reply_author_name = (str(reply['nickname'] or '').strip() or nicks.get(str(reply['user_id']), '')) if reply else ''
        mention_names = {str(m): nicks[str(m)] for m in mentions[:12] if str(m) in nicks}
        packet = {'id': row['id'], 'message_id': row['origin'], 'session': row['session'], 'author': row['user_id'],
                  'author_name': author_name,
                  'source': 'prune' if row['review_priority'] else 'daily', 'at': row['at'], 'self_id': row['self_id'], 'mentions': mentions[:12], 'mention_names': mention_names, 'reply_to': row['reply_to'],
                  'reply_author': reply['user_id'] if reply else '', 'reply_author_name': reply_author_name, 'text': row['text'][:420]}
        if reply and reply['state'] == 'active' and reply['text']:
            packet['reply_context'] = {'id': reply['id'], 'at': reply['at'], 'text': reply['text'][:140]}
        from hakimi.memory.relationship_events import packet as relationship_packet
        packet['context'] = [dict(r, text=r['text'][:140]) for r in relationship_packet(db,row)['context']]
        size += len(json.dumps(packet, ensure_ascii=False))
        if size > 6500:
            break
        result.append(packet)
    return sorted(result, key=lambda p: (p['at'], p['id']))


def validate(items, packets):
    if not isinstance(items, list) or len(items) > 2:
        raise ValueError('invalid_daily_events')
    allowed = {p['id']: p for p in packets}
    seen = set()
    validated = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('invalid_daily_event')
        ids = item.get('source_ids')
        text = item.get('text')
        event_time = item.get('event_time', '')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 8 or
                any(type(i) is not int or i not in allowed or i in seen for i in ids) or
                len(set(ids)) != len(ids) or len({allowed[i]['session'] for i in ids}) != 1 or
                not isinstance(text, str) or not 1 <= len(text.strip()) <= 140 or
                not isinstance(event_time, str) or len(event_time) > 40):
            raise ValueError('invalid_daily_source')
        if event_time and not any(event_time in allowed[i]['text'] for i in ids):
            raise ValueError('ungrounded_event_time')
        seen.update(ids)
        validated.append({'source_ids': ids, 'text': text.strip(), 'event_time': event_time})
    return validated


def apply(index, db, packets, items, now, *, status='pending', update_cadence=True):
    """Recheck the exact raw input, then atomically save card and source metadata."""
    for item in items:
        rows = [db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone() for rid in item['source_ids']]
        submitted = {p['id']: p for p in packets}
        if any(db.execute('SELECT status FROM daily_event_jobs WHERE source_id=?', (rid,)).fetchone()[0] != status
               for rid in item['source_ids']):
            continue
        if any(not r or r['state'] != 'active' or not r['text'] or
               r['text'][:420] != submitted[r['id']]['text'] for r in rows):
            continue
        sources = [{'id': r['id'], 'message_id': r['origin'], 'session': r['session'],
                    'author': r['user_id'], 'at': r['at'], 'mentions': r['direct_targets'],
                    'self_id': r['self_id'], 'reply_to': r['reply_to'],
                    'reply_author': submitted[r['id']]['reply_author'],
                    'reply_record_id': submitted[r['id']].get('reply_context', {}).get('id'),
                    'reply_at': submitted[r['id']].get('reply_context', {}).get('at')}
                   for r in rows]
        key = 'daily-' + hashlib.sha256(','.join(map(str, sorted(item['source_ids']))).encode()).hexdigest()[:20]
        observed = max(r['at'] for r in rows)
        # Automatic cards remain ordinary episodes, not Gemini/manual originals.
        distinct_authors = {str(r['user_id'] or '') for r in rows if r['user_id'] and str(r['user_id']) not in {'SELF', str(r['self_id'] or '')}}
        person_id = next(iter(distinct_authors)) if len(distinct_authors) == 1 else ''
        index._put(db, {'kind': 'episode', 'session_key': rows[0]['session'], 'key': key,
                       'note': item['text'], 'created_at': observed, 'person_id': person_id,
                       'origin_message_id': rows[0]['origin'], 'provider': 'dsh_event',
                       'evidence': item['text'],
                       'channel': 'daily_events', 'verification': 'observed_dialogue'}, 'episode')
        card = db.execute("SELECT id FROM records WHERE kind='episode' AND session=? AND key=? ORDER BY id DESC LIMIT 1",
                          (rows[0]['session'], key)).fetchone()
        if card:
            db.execute('INSERT OR IGNORE INTO daily_event_cards VALUES (?,?,?,?,?)',
                       (card[0], observed, now, item['event_time'], json.dumps(sources, ensure_ascii=False)))
    for packet in packets:
        db.execute("UPDATE daily_event_jobs SET status='done' WHERE source_id=? AND status=?", (packet['id'],status))
    if update_cadence:
        db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('daily_events_last_attempt',?)", (str(now),))


def failed(db, packets, now):
    for packet in packets:
        db.execute("UPDATE daily_event_jobs SET attempts=attempts+1,next_attempt=? + CASE attempts WHEN 0 THEN 30 WHEN 1 THEN 120 ELSE 300 END,"
                   "status=CASE WHEN attempts>=3 THEN 'error' ELSE 'pending' END "
                   "WHERE source_id=? AND status='pending'", (now, packet['id']))
    # Failure does not start the successful 30-minute batching interval.
