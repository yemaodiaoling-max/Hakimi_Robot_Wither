"""Local original text + attributed relations + FTS/dense retrieval; no model calls.

Design boundaries:
  * inbound chat is short-lived raw evidence; it is not promoted to a fact;
  * manual notes have an immutable original row (exact UTF-8 text + SHA256) and a
    mutable index/state row, so cleanup/correction can never rewrite the original;
  * semantic vectors live in SQLite and are produced by the separate loopback
    embedding service; lexical FTS remains a complete fallback;
  * graph evidence is normalised, deduplicated and scope-checked.
"""
from contextlib import contextmanager
from collections import OrderedDict
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
import threading

from hakimi.memory.text import terms
from hakimi.memory.lifecycle_rules import expanded_terms
from hakimi.memory.store_base import CHANNEL_WEIGHT, DENSE_THRESHOLD, EXTRA_SCHEMA, LEGACY_MANUAL_PROVIDERS, MANUAL_KINDS, NAME_KEYS, QUALITY, RRF_K, SCHEMA, SELF_REPORT, _dense_cosine, _hamming, _normalise_vector, _signature, _summary, _vector_blob, _vector_values, chunk_text, content_hash, cosine, index_text, key_name, vectorize  # noqa: F401 (re-export)
from hakimi.memory.store_write import StoreWriteMixin
from hakimi.memory.store_embeddings import StoreEmbeddingMixin
from hakimi.memory.store_maintenance import StoreMaintenanceMixin
from hakimi.memory.store_retrieval import StoreRetrievalMixin
from hakimi.memory.store_read import StoreReadMixin


class MemoryStore(StoreWriteMixin, StoreEmbeddingMixin, StoreMaintenanceMixin, StoreRetrievalMixin, StoreReadMixin):
    @staticmethod
    def scope(session,users,cross_groups=False,include_inactive=False):
        # Actual stores migrate channel before use.  Keep the low-level builder
        # usable by legacy tables, while every real memory query is fail-closed.
        return StoreRetrievalMixin.scope(session,users,cross_groups,include_inactive,voice_scoped=True)

    def __init__(self, base, encoder=None):
        self.base = Path(base)
        self.path = self.base / "state/memory.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.next_prune = 0
        self.topic_cache = OrderedDict()
        self.topic_lock = threading.RLock()
        self.encoder = encoder
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.executescript(EXTRA_SCHEMA)
            try:
                from hakimi.memory.graph_index import INDEX_SQL
                db.executescript(INDEX_SQL)
            except ImportError:
                pass
            try:
                from hakimi.social.social_schema import ensure_schema as ensure_social_schema
                ensure_social_schema(db)
            except ImportError:
                pass
            columns = {r[1] for r in db.execute('PRAGMA table_info(records)')}
            declarations = {
                'summary': "TEXT NOT NULL DEFAULT ''",
                'vector': "BLOB NOT NULL DEFAULT X''",
                'evidence': "TEXT NOT NULL DEFAULT ''",
                'verification': "TEXT NOT NULL DEFAULT 'legacy'",
                'shared': "INTEGER NOT NULL DEFAULT 0",
                'state': "TEXT NOT NULL DEFAULT 'active'",
                'original_id': "INTEGER",
                'content_hash': "TEXT NOT NULL DEFAULT ''",
                'channel': "TEXT NOT NULL DEFAULT ''",
                'model': "TEXT NOT NULL DEFAULT ''",
                'supersedes_id': "INTEGER",
                'superseded_by': "INTEGER",
                'legacy': "INTEGER NOT NULL DEFAULT 0",
                'nickname': "TEXT NOT NULL DEFAULT ''",
                'direct_targets': "TEXT NOT NULL DEFAULT ''",
                'reply_to': "TEXT NOT NULL DEFAULT ''",
                'self_id': "TEXT NOT NULL DEFAULT ''",
                'topics': "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in declarations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE records ADD COLUMN {name} {declaration}")
            db.execute("CREATE INDEX IF NOT EXISTS records_origin ON records(session,origin,kind)")
            db.execute("CREATE INDEX IF NOT EXISTS records_original ON records(original_id)")
            db.execute("CREATE INDEX IF NOT EXISTS records_state ON records(state,at)")
            db.execute("CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT)")
            self._migrate_originals(db)
            # Serialise request ids where the existing database is clean.  Old
            # databases may already contain duplicate request ids from the retry
            # bug; BEGIN IMMEDIATE still makes new writes atomic in that case.
            try:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS memory_originals_request_unique "
                           "ON memory_originals(request_id)")
            except sqlite3.IntegrityError:
                pass
            # Evidence survives original-text expiry, but never import credentials or attachments.
            db.execute("UPDATE records SET evidence=substr((SELECT c.text FROM records c WHERE c.kind='chat' AND c.session=records.session AND c.origin=records.origin LIMIT 1),1,520) WHERE kind!='chat' AND evidence='' AND EXISTS(SELECT 1 FROM records c WHERE c.kind='chat' AND c.session=records.session AND c.origin=records.origin)")
            # Older versions copied the latest alias into this global label, which
            # could leak a private nickname into a public graph.  Person display
            # labels are now derived per scope from visible aliases instead.
            try:
                db.execute("UPDATE graph_nodes SET label='用户'||substr(node_id,8) "
                           "WHERE node_type='person' AND label!='用户'||substr(node_id,8)")
            except sqlite3.Error:
                pass

    @staticmethod
    def _legacy_manual_row(row):
        """True only for a known old manually-authored note.

        Automatic episodes, dialogues, cold-topic programs and delivered replies
        are deliberately excluded.  Their exact original text, when still kept,
        is ordinary retention data and must not become an immortal manual row.
        """
        get = row.get if hasattr(row, "get") else (lambda key, default=None: row[key] if key in row.keys() else default)
        kind = str(get("kind", "") or "")
        if kind == "chat" or kind == "dialogue" or kind not in MANUAL_KINDS:
            return False
        provider = str(get("provider", "") or "").strip().lower()
        model = str(get("model", "") or "").strip().lower()
        channel = str(get("channel", "") or "").strip().lower()
        values = {value for value in (provider, model, channel) if value}
        if values & {"deterministic", "delivered_reply", "program", "auto"}:
            return False
        return bool(values & LEGACY_MANUAL_PROVIDERS)

    def _migrate_originals(self, db):
        """Backfill immutable legacy originals once, without manufacturing authorship."""
        rows = db.execute("SELECT * FROM records WHERE text!='' AND (content_hash='' OR content_hash IS NULL) LIMIT 4000").fetchall()
        for row in rows:
            digest = content_hash(row['text'])
            db.execute("UPDATE records SET content_hash=? WHERE id=?", (digest, row['id']))
        # Remove originals that the previous v3 pass mistakenly attached to
        # deterministic/delivered automatic records.  Their retained record text
        # is not manual authorship; legacy=1 historical rows are left untouched.
        automatic = db.execute(
            "SELECT o.id AS original_id,r.id AS record_id,r.kind,r.model,r.channel "
            "FROM memory_originals o JOIN records r ON r.id=o.record_id WHERE o.legacy=0"
        ).fetchall()
        for row in automatic:
            provider = str(row['model'] or row['channel'] or "").strip().lower()
            if row['kind'] == 'dialogue' or provider in {"deterministic", "delivered_reply", "program", "auto"}:
                db.execute("DELETE FROM memory_originals WHERE id=?", (row['original_id'],))
                db.execute("UPDATE records SET original_id=NULL,legacy=0 WHERE id=?", (row['record_id'],))
        rows = db.execute("SELECT * FROM records WHERE kind!='chat' AND kind!='dialogue' AND text!='' "
                          "AND original_id IS NULL LIMIT 4000").fetchall()
        for row in rows:
            if not self._legacy_manual_row(row):
                continue
            exists = db.execute("SELECT id FROM memory_originals WHERE record_id=?", (row['id'],)).fetchone()
            if exists:
                db.execute("UPDATE records SET original_id=? WHERE id=?", (exists[0], row['id']))
                continue
            request_id = "legacy:" + str(row['source_key'])
            metadata = json.dumps({"legacy": True, "topics": row['topics'] or []}, ensure_ascii=False)
            cursor = db.execute(
                "INSERT INTO memory_originals(record_id,request_id,content_hash,text,kind,session,user_id,"
                "item_key,source,evidence,provider,channel,verification,metadata,created_at,legacy) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (row['id'], request_id, content_hash(row['text']), row['text'], row['kind'], row['session'],
                 row['user_id'], row['key'], row['source'], row['evidence'], row['model'] or row['channel'],
                 row['channel'], row['verification'], metadata, row['at']),
            )
            db.execute("UPDATE records SET original_id=?,legacy=1 WHERE id=?", (cursor.lastrowid, row['id']))
        db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('schema_version','3')")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # ---------------------------------------------------------------- encoder
    def encoder_instance(self):
        if self.encoder is not None:
            return self.encoder
        from hakimi.memory.memory_embedding_client import default_client
        return default_client()

    def _encoder_info(self, encoder):
        model = str(getattr(encoder, "model_id", "BAAI/bge-small-zh-v1.5"))
        revision = str(getattr(encoder, "revision", "") or "")
        return model, revision

    def _refresh_encoder_identity(self, encoder):
        """Let the loopback client learn the service's actual model revision."""
        health = getattr(encoder, "health", None)
        if callable(health):
            try:
                health()
            except Exception:
                pass
        return self._encoder_info(encoder)

    def _insert_search(self, db, record_id, text, key):
        tokens = expanded_terms(index_text(text) + ' ' + str(key), lambda value: terms(value, limit=32000))
        db.execute('DELETE FROM search WHERE rowid=?', (record_id,))
        db.execute('INSERT INTO search(rowid,tokens) VALUES (?,?)', (record_id, " ".join(sorted(tokens))))
        return tokens































@lru_cache(maxsize=16)
def store(base):
    return MemoryStore(Path(base))


def migrate_v8_expiry(base, *, dry_run=False, now=None):
    """Explicit owner-deploy migration for V8's no-calendar-expiry policy."""
    return store(str(Path(base).resolve())).migrate_v8_expiry(dry_run=dry_run, now=now)
