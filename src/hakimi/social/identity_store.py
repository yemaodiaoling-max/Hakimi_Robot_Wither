"""Identity mapping schema and link tables (operator-owned)."""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from hakimi.social.identity_base import SCHEMA, _runtime_path


def ensure_schema(base):
    """Explicit startup/write initialisation.  Never called by GET or preview."""
    path = _runtime_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    # sqlite3.Connection is a transaction context manager, not a file handle
    # manager: always close it explicitly (important for temporary DB cleanup
    # on Windows and repeated apply calls).
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO identity_revision(id,revision) VALUES (1,0)")
        db.commit()
    return revision(base)


@contextmanager
def _open_read(path):
    path = Path(path)
    if not path.is_file():
        yield None
        return
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        yield connection
    finally:
        if connection is not None:
            connection.close()


@contextmanager
def _open_write(base):
    path = _runtime_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _table_exists(conn, name):
    if conn is None:
        return False
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    except sqlite3.Error:
        return False


def _columns(conn, table):
    if conn is None:
        return set()
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(" + table + ")")}
    except sqlite3.Error:
        return set()


def _revision_conn(conn):
    if conn is None or not _table_exists(conn, "identity_revision"):
        return 0
    try:
        row = conn.execute("SELECT revision FROM identity_revision WHERE id=1").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def revision(base):
    with _open_read(_runtime_path(base)) as conn:
        return _revision_conn(conn)


def _group_links(conn, old_root, *, active_only=True):
    if conn is None or not _table_exists(conn, "identity_group_links"):
        return []
    sql = ("SELECT old_root,old_group,new_group,active,created_revision,created_at,updated_at "
           "FROM identity_group_links WHERE old_root=?")
    params = [str(old_root)]
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY old_group"
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _member_links(conn, old_root, *, active_only=True, old_group=None, new_group=None,
                  old_user=None, new_user=None):
    if conn is None or not _table_exists(conn, "identity_member_links"):
        return []
    clauses = ["old_root=?"]
    params = [str(old_root)]
    if active_only:
        clauses.append("active=1")
    for name, value in (("old_group", old_group), ("new_group", new_group),
                        ("old_user", old_user), ("new_user", new_user)):
        if value is not None:
            clauses.append(name + "=?")
            params.append(str(value))
    sql = ("SELECT old_root,old_group,old_user,new_group,new_user,active,disabled_by,"
           "created_revision,created_at,updated_at FROM identity_member_links WHERE "
           + " AND ".join(clauses) + " ORDER BY old_group,old_user")
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _group_link(conn, old_root, old_group=None, new_group=None, *, active_only=True):
    for item in _group_links(conn, old_root, active_only=active_only):
        if old_group is not None and item["old_group"] != str(old_group):
            continue
        if new_group is not None and item["new_group"] != str(new_group):
            continue
        return item
    return None


def _member_link(conn, old_root, old_group=None, old_user=None, new_group=None,
                 new_user=None, *, active_only=True):
    for item in _member_links(conn, old_root, active_only=active_only,
                              old_group=old_group, old_user=old_user,
                              new_group=new_group, new_user=new_user):
        return item
    return None
