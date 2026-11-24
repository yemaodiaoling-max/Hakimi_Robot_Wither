"""Bounded interest sampling of QQ's shareable memory, never raw chat."""
import hashlib
import random
import re

from .qq import QQSource
from bili_companion.sync import SourceBatch, SourceItem
from bili_companion.sync.types import canonical

KEYWORDS = ('音乐', '科学', '游戏', '动物', '生活', '艺术', '历史', '偏好')


class QQInterestSampler(QQSource):
    renew_active = False  # stage_batches renews only this bounded selected subset.
    def __init__(self, native_path, store, *, rng=None, keywords=KEYWORDS, owner_private_session=''):
        super().__init__(native_path, store, owner_private_session=owner_private_session)
        self.rng = rng or random.Random()
        self.keywords = tuple(k for k in keywords if isinstance(k, str) and k.strip()) or KEYWORDS
        with store.database.connect(write=True) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS qq_interest_seen (
                channel TEXT, source_id TEXT, fingerprint TEXT, revision INTEGER,
                scope TEXT, active INTEGER, selected_at REAL, keyword TEXT,
                PRIMARY KEY(channel,source_id));
                CREATE TABLE IF NOT EXISTS qq_interest_rotation (
                channel TEXT PRIMARY KEY, position INTEGER);''')

    def __call__(self, checkpoint):
        # Only the complete allowlist can prove deletion. A random miss never can.
        records = []
        for record in self.rows():
            records.append(record)
            if len(records) > 1000:
                raise ValueError('source snapshot exceeds validated 1000-record capacity')
        now = self.store.clock()
        with self.store.database.connect() as db:
            previous = {r['source_id']: dict(r) for r in db.execute(
                'SELECT * FROM qq_interest_seen WHERE channel=?', (self.store.channel,))}
            legacy = {r['source_id']: dict(r) for r in db.execute(
                'SELECT * FROM bridge_manifest WHERE namespace=?', (self.namespace,))}
            rotation = db.execute('SELECT position FROM qq_interest_rotation WHERE channel=?', (self.store.channel,)).fetchone()
            exported = {(r['source_id'], r['revision']) for r in db.execute(
                "SELECT source_id,revision FROM sync_exported WHERE channel=? AND kind='qq_digest'", (self.store.channel,))}
        position = rotation[0] if rotation else 0
        keyword = self.keywords[position % len(self.keywords)]
        unread = [r for r in records if r['source_id'] not in previous
            or previous[r['source_id']]['fingerprint'] != hashlib.sha256(canonical(r).encode()).hexdigest()
            or previous[r['source_id']]['selected_at'] is None
            or now-previous[r['source_id']]['selected_at'] >= 86400
            or (r['source_id'], previous[r['source_id']]['revision']) not in exported]
        derived = set()
        for record in unread:
            for phrase in re.findall(r'[一-鿿]{2,}|[A-Za-z]{3,}', record['text']):
                for offset in range(0, len(phrase), 4):
                    term = phrase[offset:offset+4]
                    if len(term) >= 2 and term.lower() not in {'http', 'https', 'www', 'com', '这个', '那个', '我们', '他们'}:
                        derived.add(term)
        vocabulary = [k for k in self.keywords if any(k in r['text'] for r in unread)]
        vocabulary += [k for k in sorted(derived) if k not in vocabulary]
        keyword = vocabulary[position % len(vocabulary)] if vocabulary else ''
        if vocabulary and self.rng.random() < .15:
            keyword = self.rng.choice(vocabulary)
        self.last_keyword = keyword
        self.last_state = ('sampled' if keyword else 'no_keyword_candidates') if unread else 'no_unread'
        active = {r['source_id'] for r in records}
        for identity, old in {**legacy, **previous}.items():
            if old['active'] and identity not in active:
                self.store.retract(old['scope'], identity, old['revision'])
                with self.store.database.connect(write=True) as db:
                    db.execute('UPDATE qq_interest_seen SET active=0 WHERE channel=? AND source_id=?', (self.store.channel, identity))
                    db.execute('UPDATE bridge_manifest SET active=0 WHERE namespace=? AND source_id=?', (self.namespace, identity))
        candidates = []
        desktop_candidates = []
        updates = []
        for record in records:
            identity = record['source_id']
            fingerprint = hashlib.sha256(canonical(record).encode()).hexdigest()
            old = previous.get(identity) or legacy.get(identity)
            revision = 1 if old is None else old['revision'] + int(old['fingerprint'] != fingerprint or not old['active'] or identity not in previous)
            last = previous.get(identity, {}).get('selected_at')
            fresh = old is None or revision != old['revision'] or (identity, revision) not in exported
            updates.append((identity, fingerprint, revision, record['scope'], last, previous.get(identity, {}).get('keyword', '')))
            if identity.startswith('qq:desktop-pet:'):
                if (fresh or last is None or now - last >= 86400) and (now - float(record['created_at']) <= 7 * 86400):
                    item = SourceItem(revision=revision, pre_truncated=len(record['text']) > 200, **{**record, 'text': record['text'][:200]})
                    desktop_candidates.append((int(fresh), float(record['created_at']), item))
                continue
            if not keyword or keyword not in record['text']:
                continue
            if not fresh and last is not None and now-last < 86400:
                continue
            # A stable 200-character excerpt per revision avoids renewal conflicts.
            item = SourceItem(revision=revision, pre_truncated=len(record['text']) > 200, **{**record, 'text': record['text'][:200]})
            candidates.append((int(fresh), int(keyword in record['text']), self.rng.random(), item))
        desktop_candidates.sort(key=lambda c: c[:2], reverse=True)
        candidates.sort(key=lambda c: c[:3], reverse=True)
        selected, budget = [], 1000
        if desktop_candidates:
            top_desktop = desktop_candidates[0][2]
            if len(top_desktop.text) <= budget:
                selected.append(top_desktop)
                budget -= len(top_desktop.text)
                if self.last_state in {'no_keyword_candidates', 'no_unread'}:
                    self.last_state = 'sampled'
        for _, _, _, item in candidates:
            if len(item.text) > budget:
                continue
            selected.append(item)
            budget -= len(item.text)
            if len(selected) == 6:
                break
        chosen = {i.source_id for i in selected}
        with self.store.database.connect(write=True) as db:
            for identity, fingerprint, revision, scope, last, old_keyword in updates:
                db.execute('INSERT OR REPLACE INTO qq_interest_seen VALUES (?,?,?,?,?,1,?,?)',
                    (self.store.channel, identity, fingerprint, revision, scope,
                     now if identity in chosen else last, keyword if identity in chosen else old_keyword))
            db.execute('INSERT OR REPLACE INTO qq_interest_rotation VALUES (?,?)', (self.store.channel, position+1))
        return SourceBatch(tuple(selected), hashlib.sha256(canonical([(i.source_id, i.revision) for i in selected]).encode()).hexdigest())


def imported_records(store):
    """Independent imported summaries with durable actual receive time/provenance."""
    if not hasattr(store, 'database') or not hasattr(store, 'channel'):
        return []
    records = store.snapshot_records()
    with store.database.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='qq_interest_imports'").fetchone()
        if not exists:
            return []
        metadata = {(r['source_id'], r['revision']): dict(r) for r in db.execute(
            'SELECT * FROM qq_interest_imports WHERE channel=?', (store.channel,))}
    return [{**r, 'read_at': metadata.get((r['source_id'], r['revision']), {}).get('read_at', r['occurred_at']),
             'keyword': metadata.get((r['source_id'], r['revision']), {}).get('keyword', ''), 'imported': True} for r in records if r['source'] == 'qq']
