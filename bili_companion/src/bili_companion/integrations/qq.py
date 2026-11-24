"""QQ compatibility for the deployed hakimi memory schema (2026-11-01).

Never instantiate hakimi.MemoryStore here: its constructor migrates native data.
Only allowlisted columns/rows are read, through SQLite mode=ro.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, parse_qsl, unquote
import re

from bili_companion.sync import SourceBatch, SourceItem
from bili_companion.sync.types import SENSITIVE, canonical


MAX_SNAPSHOT = 1000  # hard limit shared with SourceBatch and snapshot_records
_DESKTOP_PRIVATE_CHAT_RE = re.compile(
    r'私聊|单聊|小窗|私信|微信|WeChat|'
    r'(?:找|跟|和|与)[^，。|；！？\s]{1,12}(?:私聊|单聊|小窗|聊[^\s，。|；！？]{1,8}|私下聊|单独聊)|'
    r'抖落.*私聊'
)


def _sanitize_desktop_pet_summary(text):
    raw = str(text or '').strip()
    if not raw:
        return ''
    raw = re.sub(r'^\[桌宠近期陪伴脉络\]\s*(?:我在[^\n：:]{0,16}电脑桌面上陪[^\n：:]{0,8}经历[：:]?\s*)?', '', raw).strip()
    clauses = [c.strip() for c in re.split(r'(?<=[。；！？|\n])|(?:\s*[；|]\s*)', raw) if c and c.strip()]
    kept = [c for c in clauses if not _DESKTOP_PRIVATE_CHAT_RE.search(c)]
    cleaned = ''.join(kept).strip()
    if not cleaned:
        return ''
    return f'[桌面近况] {cleaned}'[:200]


def readonly(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


class NativeSource:
    """Full allowlisted snapshot; manifest tracks edits/deletions without raw text."""
    def __init__(self, native_path, store, *, namespace):
        self.path, self.store, self.namespace = Path(native_path), store, namespace
        with store.database.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS bridge_manifest (
                namespace TEXT, source_id TEXT, fingerprint TEXT, revision INTEGER,
                scope TEXT, active INTEGER, PRIMARY KEY(namespace,source_id))''')

    def rows(self):
        raise NotImplementedError

    def __call__(self, checkpoint):
        # Missing source is a deployment error, never evidence that all rows were deleted.
        raw = list(self.rows())
        if len(raw) > MAX_SNAPSHOT:
            # Sliding window: rows() is oldest-first, so keep the newest records.
            # Older ones leave the snapshot through the normal retraction path
            # below; the native store still holds them.
            raw = raw[-MAX_SNAPSHOT:]
        items, active = [], set()
        with self.store.database.connect(write=True) as db:
            previous = {r['source_id']: dict(r) for r in db.execute(
                'SELECT * FROM bridge_manifest WHERE namespace=?', (self.namespace,))}
            for record in raw:
                source_id = record['source_id']
                active.add(source_id)
                fingerprint = hashlib.sha256(canonical(record).encode()).hexdigest()
                old = previous.get(source_id)
                rev = 1 if not old else old['revision'] + int(
                    old['fingerprint'] != fingerprint or not old['active'])
                items.append(SourceItem(revision=rev, **record))
                db.execute('INSERT OR REPLACE INTO bridge_manifest VALUES (?,?,?,?,?,1)',
                    (self.namespace, source_id, fingerprint, rev, record['scope']))
            removed = [r for key, r in previous.items() if key not in active and r['active']]
        # Retractions are durable/idempotent. Mark inactive only AFTER queuing them.
        for old in removed:
            self.store.retract(old['scope'], old['source_id'], old['revision'])
            with self.store.database.connect(write=True) as db:
                db.execute('UPDATE bridge_manifest SET active=0 WHERE namespace=? AND source_id=?',
                    (self.namespace, old['source_id']))
        return SourceBatch(tuple(items), hashlib.sha256(canonical([
            (i.source_id, i.revision) for i in items]).encode()).hexdigest())


class QQSource(NativeSource):
    """Shared sourced lore and explicitly SELF-marked reflection/preferences.

    QQ's shared=1 person rows are deliberately excluded. Blank/numeric user_id
    is not proof that a reflection describes the bot. Private sessions, raw chat,
    original evidence, nickname, origin message IDs and attachments are not read.
    """
    def __init__(self, native_path, store, *, owner_private_session=''):
        super().__init__(native_path, store, namespace='qq-native-v1')
        self.owner_private_session = str(owner_private_session or '').strip()

    def rows(self):
        db = readonly(self.path)
        try:
            cols = {r[1] for r in db.execute('PRAGMA table_info(records)')}
            required = {'id', 'kind', 'session', 'user_id', 'text', 'at', 'source',
                        'shared', 'state', 'channel', 'model', 'expires'}
            if not required <= cols:
                raise ValueError('unsupported QQ records schema')
            now = float(self.store.clock())
            rows = db.execute('''SELECT id,kind,text,at,source,channel,model FROM records
                WHERE state='active' AND session LIKE 'group:%' AND session!='self:bilibili' AND (expires=0 OR expires>?)
                AND ((kind='lore' AND shared=1)
                    OR (kind IN ('reflection','preference') AND user_id='SELF'))
                ORDER BY id''', (now,)).fetchall()
            for row in rows:
                if row['channel'] in {'bilibili', 'bilibili_watch', 'memory_bridge', 'external'} or row['model'] == 'memory_bridge':
                    continue
                body = row['text']
                if not isinstance(body, str) or not body.strip() or SENSITIVE.search(body):
                    continue
                source = row['source'] or ''
                if row['kind'] == 'lore':
                    url = urlsplit(source)
                    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
                        continue
                    sensitive_key = re.compile(r'(?:token|key|auth|sign|secret|password|session|sessdata|cookie|credential)', re.I)
                    if (len(source) > 240 or SENSITIVE.search(source)
                            or any(sensitive_key.search(unquote(key)) for key, _ in parse_qsl(url.query, keep_blank_values=True))
                            or url.fragment):
                        continue
                    refs = (source, 'qq:public-lore:M' + str(row['id']))
                else:
                    refs = ('qq:self-note:M' + str(row['id']),)
                yield dict(source_id='qq:M' + str(row['id']), scope='public' if row['kind'] == 'lore' else 'self',
                    kind='public_fact' if row['kind'] == 'lore' else row['kind'], text=body,
                    created_at=row['at'], attribution='inference' if row['kind'] == 'lore' else 'feeling',
                    source_refs=refs)
            if 'key' in cols:
                if self.owner_private_session:
                    pet_rows = db.execute('''SELECT id,text,at FROM records
                        WHERE state='active' AND session=? AND channel='desktop_pet'
                        AND kind='episode' AND key='桌宠阶段脉络'
                        AND (expires=0 OR expires>?) AND at>=?
                        ORDER BY at DESC, id DESC LIMIT 2''',
                        (self.owner_private_session, now, now - 7 * 86400)).fetchall()
                else:
                    pet_rows = db.execute('''SELECT id,text,at FROM records
                        WHERE state='active' AND session LIKE 'private:%' AND channel='desktop_pet'
                        AND kind='episode' AND key='桌宠阶段脉络'
                        AND (expires=0 OR expires>?) AND at>=?
                        ORDER BY at DESC, id DESC LIMIT 2''',
                        (now, now - 7 * 86400)).fetchall()
                for prow in reversed(pet_rows):
                    clean = _sanitize_desktop_pet_summary(prow['text'])
                    if not clean or SENSITIVE.search(clean):
                        continue
                    ref_id = 'qq:desktop-pet:M' + str(prow['id'])
                    yield dict(source_id=ref_id, scope='self', kind='reflection',
                        text=clean, created_at=prow['at'], attribution='feeling',
                        source_refs=(ref_id,))
        finally:
            db.close()


class BilibiliSource(NativeSource):
    """Export native observed media, never imported bridge facts or text-only titles."""
    def __init__(self, native_path, store, *, account_id):
        super().__init__(native_path, store, namespace='bilibili-native:' + account_id)
        self.account_id = account_id

    def rows(self):
        db = readonly(self.path)
        try:
            for row in db.execute("SELECT payload FROM evidence WHERE platform='bilibili' AND account=? ORDER BY observed,id", (self.account_id,)):
                record = json.loads(row['payload'])
                scope = record['scope']
                if (scope['kind'] not in {'video', 'live_room'} or scope['platform'] != 'bilibili'
                        or scope['account_id'] != self.account_id
                        or record['attribution'] != 'observation'
                        or record['modality'] not in {'audio', 'video', 'subtitle', 'mixed'}
                        or record['media_end'] <= record['media_start']):
                    continue
                prefix = 'bilibili:video:' if scope['kind'] == 'video' else 'bilibili:live:'
                ref = prefix + scope['identity'] + ':session:' + record['session_id']
                yield dict(source_id='bilibili:evidence:' + record['id'], scope='self',
                    kind='watch_note', text=record['text'], created_at=record['observed_at'],
                    attribution='observation', source_refs=(ref,), coverage=({
                        'start':record['media_start'], 'end':record['media_end'],
                        'modality':record['modality']},))
        finally:
            db.close()
