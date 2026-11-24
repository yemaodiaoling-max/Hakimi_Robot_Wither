"""Immutable evidence and explicitly scoped, revisable notes; no QQ imports."""
from dataclasses import asdict
import hashlib
import json
import re
import uuid
from bili_companion.domain import Evidence, Scope
from .sources import media_refs


def terms(text):
    chunks = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    return {token for chunk in chunks for token in (
        [chunk] if chunk.isascii() or len(chunk) == 1 else
        [chunk[i:i+2] for i in range(len(chunk)-1)])}


class MemoryStore:
    def __init__(self, database):
        self.database = database
        with database.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, scope TEXT, account TEXT, platform TEXT,
                    session TEXT, observed REAL, text TEXT, public INTEGER, payload TEXT);
                CREATE INDEX IF NOT EXISTS evidence_scope ON evidence(scope,observed);
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, scope TEXT, text TEXT, evidence_ids TEXT,
                    revision INTEGER, content_hash TEXT);
                CREATE TABLE IF NOT EXISTS memory_jobs (
                    task_key TEXT PRIMARY KEY, scope TEXT, content_hash TEXT,
                    state TEXT, attempts INTEGER DEFAULT 0, error TEXT DEFAULT '');
            """)

    def append(self, evidence: Evidence):
        payload = json.dumps(asdict(evidence), ensure_ascii=False, sort_keys=True)
        with self.database.connect(write=True) as db:
            old = db.execute("SELECT payload FROM evidence WHERE id=?", (evidence.id,)).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError('immutable evidence ID conflict')
                return False
            db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?)", (
                evidence.id, evidence.scope.key(), evidence.scope.account_id,
                evidence.scope.platform, evidence.session_id, evidence.observed_at,
                evidence.text, int(evidence.public), payload))
            return True

    def get(self, evidence_id, scope: Scope, *, include_public=False):
        with self.database.connect() as db:
            row = db.execute("""SELECT * FROM evidence WHERE id=? AND
                (scope=? OR (? AND public=1 AND account=? AND platform=?))""",
                (evidence_id, scope.key(), include_public, scope.account_id, scope.platform)).fetchone()
            return json.loads(row['payload']) if row else None

    def recall(self, scope, query, *, include_public=False, limit=4, budget=600):
        query_terms = terms(query)
        with self.database.connect() as db:
            rows = db.execute("""SELECT id,text,observed,scope,session FROM evidence WHERE scope=? OR
                (? AND public=1 AND account=? AND platform=?) ORDER BY observed DESC LIMIT 2000""",
                (scope.key(), include_public, scope.account_id, scope.platform)).fetchall()
            notes = db.execute("SELECT id,text,0 AS observed,scope,NULL AS session FROM notes WHERE scope=?", (scope.key(),)).fetchall()
        scored = sorted([dict(row) for row in [*rows, *notes]],
                        key=lambda row: (len(terms(row['text']) & query_terms), row['observed']), reverse=True)
        result = []
        for row in scored:
            if not query_terms & terms(row['text']) or len(row['text']) > budget:
                continue
            platform, account, kind, identity = json.loads(row.pop('scope'))
            row['source_refs'] = media_refs(Scope(account,kind,identity,platform), row.pop('session'))
            result.append(row)
            budget -= len(row['text'])
            if len(result) >= min(4, max(1, limit)):
                break
        return result

    def revise(self, scope, text, evidence_ids, *, note_id=None, expected_revision=0):
        if not text.strip() or not evidence_ids or len(text) > 8000:
            raise ValueError('note requires bounded text and evidence')
        note_id = note_id or str(uuid.uuid4())
        with self.database.connect(write=True) as db:
            for evidence_id in evidence_ids:
                if not db.execute("SELECT 1 FROM evidence WHERE id=? AND scope=?", (evidence_id, scope.key())).fetchone():
                    raise ValueError('evidence outside scope')
            old = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
            if old and (old['scope'] != scope.key() or old['revision'] != expected_revision):
                raise ValueError('scope or revision conflict')
            if not old and expected_revision:
                raise ValueError('missing revision')
            digest = hashlib.sha256(text.encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?)", (
                note_id, scope.key(), text, json.dumps(evidence_ids), expected_revision+1, digest))
        return note_id

    def enqueue(self, scope, task_key, content_hash):
        with self.database.connect(write=True) as db:
            return bool(db.execute("INSERT OR IGNORE INTO memory_jobs(task_key,scope,content_hash,state) VALUES (?,?,?,'pending')",
                                   (task_key, scope.key(), content_hash)).rowcount)

    def delete(self, scope, evidence_id):
        with self.database.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM evidence WHERE id=? AND scope=?", (evidence_id, scope.key())).fetchone():
                return False
            for row in db.execute("SELECT id,evidence_ids FROM notes WHERE scope=?", (scope.key(),)).fetchall():
                if evidence_id in json.loads(row['evidence_ids']):
                    db.execute("DELETE FROM notes WHERE id=?", (row['id'],))
            db.execute("DELETE FROM evidence WHERE id=?", (evidence_id,))
            db.execute("DELETE FROM memory_jobs WHERE scope=?", (scope.key(),))
            return True
