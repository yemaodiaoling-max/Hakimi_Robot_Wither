"""Social assessment tables, indexes and in-place migrations (DDL only)."""
from __future__ import annotations

import sqlite3
import uuid

from hakimi.social.social_base import TERRA_PROVENANCE


SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS social_assessments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 request_id TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 target_id TEXT NOT NULL,
 author TEXT NOT NULL,
 provider TEXT NOT NULL,
 provenance TEXT NOT NULL,
 impression TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 affinity_policy TEXT NOT NULL DEFAULT '{}',
 affinity INTEGER,
 closeness INTEGER,
 confidence INTEGER,
 relationship_type TEXT NOT NULL DEFAULT '',
 evidence_record_ids TEXT NOT NULL DEFAULT '[]',
 evidence_dates TEXT NOT NULL DEFAULT '[]',
 evidence_times TEXT NOT NULL DEFAULT '[]',
 expires_at REAL NOT NULL DEFAULT 0,
 original_record_id INTEGER,
 created_at REAL NOT NULL,
 session TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 supersedes_id INTEGER,
 superseded_by INTEGER,
 state TEXT NOT NULL DEFAULT 'active',
 seed_id TEXT NOT NULL DEFAULT '',
 CHECK (subject_id = 'hakimi' OR subject_id GLOB '[1-9]*'),
 CHECK (target_id GLOB '[1-9]*'),
 CHECK (affinity IS NULL OR (affinity BETWEEN 0 AND 100)),
 CHECK (closeness IS NULL OR (closeness BETWEEN 0 AND 100)),
 CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 100)),
 CHECK (provenance IN ('gemini_manual', 'dsh_assisted', 'terra_assisted')),
 CHECK (state IN ('active', 'superseded', 'deleted'))
);
"""
SCHEMA_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS social_assessments_request
 ON social_assessments(request_id);
CREATE INDEX IF NOT EXISTS social_assessments_subject
 ON social_assessments(subject_id, state, created_at);
CREATE INDEX IF NOT EXISTS social_assessments_target
 ON social_assessments(target_id, state, created_at);
CREATE INDEX IF NOT EXISTS social_assessments_pair
 ON social_assessments(subject_id, target_id, provenance, state, created_at);
CREATE INDEX IF NOT EXISTS social_assessments_original
 ON social_assessments(original_record_id);
CREATE INDEX IF NOT EXISTS social_assessments_session
 ON social_assessments(session, state, created_at);
"""


def _table_sql(db, name="social_assessments"):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return str(row[0] or "") if row else ""


def _quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def _savepoint(db):
    name = "social_schema_" + uuid.uuid4().hex
    db.execute("SAVEPOINT " + _quote(name))
    return name


def _release_savepoint(db, name):
    db.execute("RELEASE SAVEPOINT " + _quote(name))


def _rollback_savepoint(db, name):
    try:
        db.execute("ROLLBACK TO SAVEPOINT " + _quote(name))
        db.execute("RELEASE SAVEPOINT " + _quote(name))
    except sqlite3.Error:
        pass


def _create_indexes(db):
    # Applies the six idempotent V5 indexes one statement at a time; never via
    # executescript(), whose implicit COMMIT would defeat atomic rollback.
    for statement in SCHEMA_INDEXES.split(";"):
        if statement.strip():
            db.execute(statement)


def _unique_migration_table(db):
    """Return an unused temporary name; never drop a pre-existing backup."""
    while True:
        name = "social_assessments_v5_migrate_" + uuid.uuid4().hex[:16]
        row = db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if not row:
            return name


def _rebuild_for_terra(db):
    """Atomically rebuild only the CHECK constraint, preserving every row.

    SQLite cannot alter a CHECK constraint in place.  All DDL/DML runs inside a
    caller-scoped savepoint: the old table is renamed to a fresh temporary name,
    the V5 table is created, rows are copied with explicit ids, the old indexes
    and temporary table are dropped, then the savepoint is released.  A failure
    rolls the rename/copy back to the canonical table name.  No pre-existing
    backup table is ever dropped or overwritten.
    """
    old_sql = _table_sql(db)
    if TERRA_PROVENANCE in old_sql:
        return
    temporarily = _unique_migration_table(db)
    db.execute("ALTER TABLE social_assessments RENAME TO " + _quote(temporarily))
    # SQLite keeps index names global; drop the old ones before recreating the
    # same names on the new table.  The savepoint restores them on failure.
    for row in db.execute("PRAGMA index_list(" + _quote(temporarily) + ")").fetchall():
        name = str(row[1])
        if name and not name.startswith("sqlite_autoindex"):
            db.execute("DROP INDEX IF EXISTS " + _quote(name))
    db.execute(SCHEMA_TABLE)
    new_columns = [row[1] for row in db.execute("PRAGMA table_info(social_assessments)")]
    old_columns = {row[1] for row in db.execute("PRAGMA table_info(" + _quote(temporarily) + ")")}
    copied = [name for name in new_columns if name in old_columns]
    if copied:
        names = ",".join(_quote(name) for name in copied)
        db.execute("INSERT INTO social_assessments(" + names + ") SELECT " + names +
                   " FROM " + _quote(temporarily))
    db.execute("DROP TABLE " + _quote(temporarily))
    _create_indexes(db)


def ensure_schema(db):
    """Create/migrate the social projection atomically without touching others.

    Existing gemini_manual and dsh_assisted rows, ids, versions and supersede
    links are copied verbatim.  The only schema change is the wider provenance
    allow-list plus additive evidence-time/expiry columns.  The whole operation
    uses an explicit SAVEPOINT because sqlite3.executescript() commits pending
    work before running, which would make a mid-migration failure destructive.
    """
    savepoint = _savepoint(db)
    try:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_assessments'"
        ).fetchone()
        if not exists:
            db.execute(SCHEMA_TABLE)
            _create_indexes(db)
        elif TERRA_PROVENANCE not in _table_sql(db):
            _rebuild_for_terra(db)
        else:
            columns = {row[1] for row in db.execute("PRAGMA table_info(social_assessments)")}
            additions = {
                "reason": "TEXT NOT NULL DEFAULT ''",
        "affinity_policy": "TEXT NOT NULL DEFAULT '{}'",
                "evidence_dates": "TEXT NOT NULL DEFAULT '[]'",
                "evidence_times": "TEXT NOT NULL DEFAULT '[]'",
                "expires_at": "REAL NOT NULL DEFAULT 0",
                "supersedes_id": "INTEGER",
                "superseded_by": "INTEGER",
                "state": "TEXT NOT NULL DEFAULT 'active'",
                "seed_id": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE social_assessments ADD COLUMN {name} {declaration}")
            _create_indexes(db)
        _release_savepoint(db, savepoint)
    except BaseException:
        _rollback_savepoint(db, savepoint)
        raise
