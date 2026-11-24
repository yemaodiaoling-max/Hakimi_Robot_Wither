"""Separate, endpoint-bound digest inbox/outbox; never writes native memory tables."""
import json
import time
import uuid
from .types import canonical, digest, seal_packet, validate_packet, text, revision, number
from bili_companion.memory.store import terms


class SyncStore:
    def __init__(self, database, *, endpoint, local_id, peer_id, clock=time.time):
        if endpoint not in {'qq', 'bilibili'}:
            raise ValueError('invalid endpoint')
        text(local_id, 160)
        text(peer_id, 160)
        self.database, self.endpoint = database, endpoint
        self.local_id, self.peer_id, self.clock = local_id, peer_id, clock
        self.peer_endpoint = 'qq' if endpoint == 'bilibili' else 'bilibili'
        self.channel = canonical([endpoint, local_id, peer_id])
        with database.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    channel TEXT PRIMARY KEY, checkpoint TEXT DEFAULT '', last_sync REAL,
                    next_due REAL DEFAULT 0, lease_until REAL DEFAULT 0,
                    failures INTEGER DEFAULT 0, lease_token TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS sync_outbox (
                    channel TEXT, packet_id TEXT, payload TEXT, expires REAL,
                    state TEXT, attempts INTEGER DEFAULT 0,
                    PRIMARY KEY(channel,packet_id));
                CREATE TABLE IF NOT EXISTS sync_exported (
                    channel TEXT, source_scope TEXT, source_id TEXT, revision INTEGER,
                    kind TEXT, packet_id TEXT,
                    PRIMARY KEY(channel,source_scope,source_id,revision,kind));
                CREATE TABLE IF NOT EXISTS sync_inbox (
                    channel TEXT, packet_id TEXT, checksum TEXT, expires REAL,
                    PRIMARY KEY(channel,packet_id));
                CREATE TABLE IF NOT EXISTS sync_facts (
                    channel TEXT, source_scope TEXT, source_id TEXT, revision INTEGER,
                    packet_id TEXT, payload TEXT, expires REAL,
                    PRIMARY KEY(channel,source_scope,source_id,revision));
                CREATE TABLE IF NOT EXISTS sync_tombstones (
                    channel TEXT, source_scope TEXT, source_id TEXT, revision INTEGER,
                    PRIMARY KEY(channel,source_scope,source_id));
                CREATE TABLE IF NOT EXISTS sync_source_receipts (
                    channel TEXT, source_scope TEXT, source_id TEXT, revision INTEGER,
                    fingerprint TEXT,
                    PRIMARY KEY(channel,source_scope,source_id,revision));
                CREATE TABLE IF NOT EXISTS sync_active_export (
                    channel TEXT, source_scope TEXT, source_id TEXT, revision INTEGER,
                    payload TEXT, valid_until REAL,
                    PRIMARY KEY(channel,source_scope,source_id));
            """)
            if 'lease_token' not in {row[1] for row in db.execute('PRAGMA table_info(sync_state)')}:
                db.execute("ALTER TABLE sync_state ADD COLUMN lease_token TEXT DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO sync_state(channel) VALUES (?)", (self.channel,))

    def status(self):
        with self.database.connect() as db:
            state = dict(db.execute("SELECT * FROM sync_state WHERE channel=?", (self.channel,)).fetchone())
            state.pop('channel')
            state['pending'] = db.execute("SELECT COUNT(*) FROM sync_outbox WHERE channel=? AND state='pending'", (self.channel,)).fetchone()[0]
            return state

    def claim(self, *, force=False, lease_seconds=120):
        now = self.clock()
        number(now)
        with self.database.connect(write=True) as db:
            state = db.execute("SELECT * FROM sync_state WHERE channel=?", (self.channel,)).fetchone()
            if state['lease_until'] > now or (not force and state['next_due'] > now):
                return None
            token = str(uuid.uuid4())
            db.execute("UPDATE sync_state SET lease_until=?,lease_token=? WHERE channel=?", (now+lease_seconds, token, self.channel))
            return token

    def _lease(self, db, token):
        if token is not None:
            row = db.execute("SELECT lease_token,lease_until FROM sync_state WHERE channel=?", (self.channel,)).fetchone()
            if row['lease_token'] != token or row['lease_until'] <= self.clock():
                raise ValueError('stale worker lease')

    def _packet(self, facts, sources, kind):
        now = self.clock()
        return seal_packet(dict(version=1, packet_id=str(uuid.uuid4()), origin=self.endpoint,
            recipient=self.peer_endpoint, origin_id=self.local_id, recipient_id=self.peer_id,
            kind=kind, created_at=now, expires_at=now+7*86400,
            source_ids=sources, facts=facts))

    def _stage(self, db, facts, sources, kind):
        selected_facts, selected_sources = [], []
        for i, identity in enumerate(sources):
            exists = db.execute("""SELECT packet_id FROM sync_exported WHERE channel=? AND
                source_scope=? AND source_id=? AND revision=? AND kind=?""", (
                self.channel, identity['scope'], identity['source_id'], identity['revision'], kind)).fetchone()
            if kind != 'retraction':
                tomb = db.execute("""SELECT MAX(revision) FROM sync_exported WHERE channel=? AND
                    source_scope=? AND source_id=? AND kind='retraction'""", (
                    self.channel, identity['scope'], identity['source_id'])).fetchone()[0]
                if tomb is not None and tomb >= identity['revision']:
                    continue
                active = db.execute("""SELECT * FROM sync_active_export WHERE channel=? AND
                    source_scope=? AND source_id=?""", (self.channel, identity['scope'], identity['source_id'])).fetchone()
                if active and active['revision'] > identity['revision']:
                    continue
                if active and active['revision'] == identity['revision'] and active['payload'] != canonical(facts[i]):
                    raise ValueError('local source revision conflict')
                renewable = active is not None and active['valid_until'] <= self.clock()+86400
            else:
                renewable = False
            if not exists or renewable:
                selected_sources.append(identity)
                if facts:
                    selected_facts.append(facts[i])
        if not selected_sources:
            return None
        packet = self._packet(selected_facts, selected_sources, kind)
        validate_packet(packet, now=self.clock())
        db.execute("INSERT INTO sync_outbox(channel,packet_id,payload,expires,state) VALUES (?,?,?,?,'pending')",
                   (self.channel, packet['packet_id'], canonical(packet), packet['expires_at']))
        for source in selected_sources:
            db.execute("INSERT OR REPLACE INTO sync_exported VALUES (?,?,?,?,?,?)", (
                self.channel, source['scope'], source['source_id'], source['revision'], kind, packet['packet_id']))
        if kind != 'retraction':
            for fact in selected_facts:
                db.execute("INSERT OR REPLACE INTO sync_active_export VALUES (?,?,?,?,?,?)", (
                    self.channel, fact['scope'], fact['source_id'], fact['revision'],
                    canonical(fact), packet['expires_at']))
        return packet['packet_id']

    def stage(self, facts, *, expected_checkpoint, checkpoint, lease_token=None):
        packets = self.stage_batches([facts] if facts else [], expected_checkpoint=expected_checkpoint,
                                     checkpoint=checkpoint, lease_token=lease_token)
        return packets[0] if packets else None

    def stage_batches(self, batches, *, expected_checkpoint, checkpoint, lease_token=None):
        """Atomically checkpoint source scan and durable outbox, before transport."""
        text(checkpoint, 512, empty=True)
        with self.database.connect(write=True) as db:
            self._lease(db, lease_token)
            current = db.execute("SELECT checkpoint FROM sync_state WHERE channel=?", (self.channel,)).fetchone()[0]
            if current != expected_checkpoint:
                raise ValueError('stale source checkpoint')
            kind = 'watch_digest' if self.endpoint == 'bilibili' else 'qq_digest'
            packet_ids = []
            for facts in batches:
                sources = [{k: fact[k] for k in ('source_id', 'revision', 'scope')} for fact in facts]
                packet_id = self._stage(db, facts, sources, kind)
                if packet_id:
                    packet_ids.append(packet_id)
            db.execute("UPDATE sync_state SET checkpoint=? WHERE channel=?", (checkpoint, self.channel))
            return packet_ids

    def retract(self, scope, source_id, source_revision):
        """Queue a tombstone through a source revision; never edits original memory."""
        text(scope, 160)
        text(source_id, 160)
        revision(source_revision)
        with self.database.connect(write=True) as db:
            packet = self._stage(db, [], [dict(scope=scope, source_id=source_id, revision=source_revision)], 'retraction')
            queued = db.execute("SELECT packet_id,payload,state FROM sync_outbox WHERE channel=? AND payload!=''", (self.channel,)).fetchall()
            for row in queued:
                original = json.loads(row['payload'])
                if original['kind'] == 'retraction':
                    continue
                keep = [f for f in original['facts'] if not (f['scope'] == scope and
                    f['source_id'] == source_id and f['revision'] <= source_revision)]
                if len(keep) == len(original['facts']):
                    continue
                db.execute("UPDATE sync_outbox SET state='cancelled',payload='' WHERE channel=? AND packet_id=?", (self.channel, row['packet_id']))
                if row['state'] == 'pending' and keep:
                    for fact in keep:
                        db.execute("""DELETE FROM sync_exported WHERE channel=? AND source_scope=?
                            AND source_id=? AND revision=? AND kind=?""", (
                            self.channel, fact['scope'], fact['source_id'], fact['revision'], original['kind']))
                    self._stage(db, keep, [{k:f[k] for k in ('scope', 'source_id', 'revision')} for f in keep], original['kind'])
            db.execute("""DELETE FROM sync_active_export WHERE channel=? AND source_scope=?
                AND source_id=? AND revision<=?""", (self.channel, scope, source_id, source_revision))
            db.execute("UPDATE sync_state SET next_due=0 WHERE channel=?", (self.channel,))
            return packet

    def renew(self, *, lease_token=None):
        """Refresh unchanged active digests at most once per six days, not per poll."""
        kind = 'watch_digest' if self.endpoint == 'bilibili' else 'qq_digest'
        with self.database.connect(write=True) as db:
            self._lease(db, lease_token)
            rows = db.execute("""SELECT payload FROM sync_active_export WHERE channel=?
                AND valid_until<=? LIMIT 1000""", (self.channel, self.clock()+86400)).fetchall()
            facts = [json.loads(row[0]) for row in rows]
            packet_ids, batch = [], []
            for fact in facts:
                if batch and (len(batch) >= 8 or sum(len(f['text']) for f in batch)+len(fact['text']) > 2400
                              or len(canonical([*batch, fact]).encode('utf-8')) > 24_000):
                    packet_ids.append(self._stage(db, batch, [{k:f[k] for k in ('scope', 'source_id', 'revision')} for f in batch], kind))
                    batch = []
                batch.append(fact)
            if batch:
                packet_ids.append(self._stage(db, batch, [{k:f[k] for k in ('scope', 'source_id', 'revision')} for f in batch], kind))
            return packet_ids

    def pending(self, limit=10):
        self.cleanup()
        with self.database.connect() as db:
            return [json.loads(row[0]) for row in db.execute("""SELECT payload FROM sync_outbox
                WHERE channel=? AND state='pending' ORDER BY rowid LIMIT ?""", (self.channel, min(50, limit)))]

    def attempted(self, packet_id, *, lease_token=None):
        with self.database.connect(write=True) as db:
            self._lease(db, lease_token)
            if not db.execute("""UPDATE sync_outbox SET attempts=attempts+1
                WHERE channel=? AND packet_id=? AND state='pending' AND expires>?""",
                (self.channel, packet_id, self.clock())).rowcount:
                raise ValueError('packet no longer pending')

    def acknowledge(self, packet_id, *, lease_token=None):
        with self.database.connect(write=True) as db:
            self._lease(db, lease_token)
            db.execute("UPDATE sync_outbox SET state='acked' WHERE channel=? AND packet_id=?",
                       (self.channel, packet_id))

    def finish(self, *, success, interval_seconds, lease_token=None):
        now = self.clock()
        with self.database.connect(write=True) as db:
            self._lease(db, lease_token)
            previous = db.execute("SELECT failures FROM sync_state WHERE channel=?", (self.channel,)).fetchone()[0]
            failures = 0 if success else min(previous+1, 20)
            delay = interval_seconds if success else min(900, 5*2**min(failures-1, 8))
            db.execute("""UPDATE sync_state SET lease_until=0, failures=?, next_due=?,
                lease_token='',last_sync=CASE WHEN ? THEN ? ELSE last_sync END WHERE channel=?""",
                (failures, now+delay, success, now, self.channel))

    def receive(self, packet):
        validate_packet(packet, now=self.clock())
        if (packet['recipient'] != self.endpoint or packet['recipient_id'] != self.local_id
                or packet['origin'] != self.peer_endpoint or packet['origin_id'] != self.peer_id):
            raise ValueError('packet endpoint identity mismatch')
        with self.database.connect(write=True) as db:
            existing = db.execute("SELECT checksum FROM sync_inbox WHERE channel=? AND packet_id=?",
                                  (self.channel, packet['packet_id'])).fetchone()
            if existing:
                if existing[0] != packet['checksum']:
                    raise ValueError('packet ID conflict')
                return False
            for index, identity in enumerate(packet['source_ids']):
                key = (self.channel, identity['scope'], identity['source_id'])
                rev = identity['revision']
                tomb = db.execute("SELECT revision FROM sync_tombstones WHERE channel=? AND source_scope=? AND source_id=?", key).fetchone()
                if packet['kind'] == 'retraction':
                    db.execute("""INSERT INTO sync_tombstones VALUES (?,?,?,?)
                        ON CONFLICT(channel,source_scope,source_id) DO UPDATE SET revision=MAX(revision,excluded.revision)""", (*key, rev))
                    db.execute("DELETE FROM sync_facts WHERE channel=? AND source_scope=? AND source_id=? AND revision<=?", (*key, rev))
                elif not tomb or tomb[0] < rev:
                    highest = db.execute("""SELECT MAX(revision) FROM sync_source_receipts
                        WHERE channel=? AND source_scope=? AND source_id=?""", key).fetchone()[0]
                    if highest is not None and rev < highest:
                        continue
                    payload = canonical(packet['facts'][index])
                    receipt = db.execute("""SELECT fingerprint FROM sync_source_receipts WHERE
                        channel=? AND source_scope=? AND source_id=? AND revision=?""", (*key, rev)).fetchone()
                    if receipt and receipt[0] != digest(packet['facts'][index]):
                        raise ValueError('source revision conflict')
                    prior = db.execute("""SELECT payload FROM sync_facts WHERE channel=? AND
                        source_scope=? AND source_id=? AND revision=?""", (*key, rev)).fetchone()
                    if prior and prior[0] != payload:
                        raise ValueError('source revision conflict')
                    db.execute("INSERT OR IGNORE INTO sync_facts VALUES (?,?,?,?,?,?,?)", (
                        *key, rev, packet['packet_id'], payload, packet['expires_at']))
                    db.execute("""UPDATE sync_facts SET expires=MAX(expires,?),packet_id=?
                        WHERE channel=? AND source_scope=? AND source_id=? AND revision=?""",
                        (packet['expires_at'], packet['packet_id'], *key, rev))
                    db.execute("INSERT OR IGNORE INTO sync_source_receipts VALUES (?,?,?,?,?)", (
                        *key, rev, digest(packet['facts'][index])))
            db.execute("INSERT INTO sync_inbox VALUES (?,?,?,?)", (
                self.channel, packet['packet_id'], packet['checksum'], packet['expires_at']))
            return True

    def recall(self, scope='self', query='', *, budget=1000):
        """Return recipient-account self-space digests, never native evidence.

        Total canonical JSON length (including metadata) is at most 1000 chars.
        Origin scopes remain provenance, not recipient permissions.
        """
        if scope != 'self':
            return []
        budget = min(1000, max(0, int(budget)))
        query_terms = terms(query)
        records = []
        for record in self.snapshot_records():
            record = {key:value for key,value in record.items() if key != 'expires_at'}
            relevance = len(terms(record['summary']) & query_terms)
            if query_terms and not relevance:
                continue
            records.append((relevance, record['occurred_at'], record))
        result = []
        for _, _, record in sorted(records, key=lambda x: (x[0], x[1]), reverse=True):
            # Preserve all metadata; trim only the already-lossy excerpt.
            while record['summary'] and len(canonical([*result, record])) > budget:
                record['summary'] = record['summary'][:-1]
                record['truncated'] = True
            if not record['summary']:
                continue
            result.append(record)
            if len(result) >= 4:
                break
        return result

    def snapshot_records(self, *, limit=1000):
        """Endpoint-bound active latest digests for trusted local context snapshots.

        This is not model injection: call recall() for the separate 1000-char cap.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('snapshot limit must be 1..1000')
        with self.database.connect() as db:
            rows = db.execute("""SELECT f.* FROM sync_facts f WHERE channel=? AND expires>?
                AND NOT EXISTS (SELECT 1 FROM sync_source_receipts n WHERE n.channel=f.channel
                    AND n.source_scope=f.source_scope AND n.source_id=f.source_id AND n.revision>f.revision)
                AND NOT EXISTS (SELECT 1 FROM sync_tombstones t WHERE t.channel=f.channel
                    AND t.source_scope=f.source_scope AND t.source_id=f.source_id AND t.revision>=f.revision)
                ORDER BY rowid DESC LIMIT ?""", (self.channel, self.clock(), limit)).fetchall()
        records = []
        for row in rows:
            fact = json.loads(row['payload'])
            records.append(dict(source=self.peer_endpoint,
                summary=fact['text'], source_refs=fact['source_refs'], coverage=fact['coverage'],
                attribution=fact['attribution'], occurred_at=fact['created_at'],
                packet_id=row['packet_id'], source_id=fact['source_id'], revision=fact['revision'],
                is_digest=True, truncated=fact['truncated'], expires_at=row['expires']))
        return records

    def cleanup(self):
        now = self.clock()
        with self.database.connect(write=True) as db:
            db.execute("DELETE FROM sync_facts WHERE channel=? AND expires<=?", (self.channel, now))
            # Retain source revision receipts/tombstones: old packets cannot resurrect deletes.
            db.execute("UPDATE sync_outbox SET state='expired',payload='' WHERE channel=? AND expires<=?",
                       (self.channel, now))
