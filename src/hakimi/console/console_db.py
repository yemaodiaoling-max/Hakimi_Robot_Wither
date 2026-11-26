"""Read-only database access and paging helpers for the console API."""
from __future__ import annotations
from hakimi.core import bot_identity

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from hakimi.console.errors import ApiError
from hakimi.social.social_schema import ensure_schema


DEFAULT_SELF_ID = str(bot_identity.SELF_QQ)
SESSIONS = re.compile(r"(?:group|private):[1-9][0-9]{0,19}")
KINDS = {"chat", "person", "preference", "lore", "episode", "reflection", "commitment", "dialogue"}
METRICS = {"manual", "assisted", "interaction"}


def _path(base):
    return Path(base).resolve() / "state/memory.sqlite3"


@contextmanager
def open_db(base):
    """Read-only connection context; always closed, including on errors."""
    path = _path(base)
    if not path.exists():
        raise ApiError(503, "database_not_ready", "记忆库尚未初始化")
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        yield connection
    except sqlite3.Error as exc:
        raise ApiError(503, "database_busy", "记忆库暂时不可读") from exc
    finally:
        if connection is not None:
            connection.close()


@contextmanager
def db_conn(base):
    """Read-only connection that is always closed, including on query errors."""
    with open_db(base) as connection:
        yield connection


def initialise_social(base):
    """Explicit startup schema initialisation (never on request paths)."""
    from hakimi.memory.memory_store import MemoryStore
    store = MemoryStore(str(Path(base).resolve()))
    with store.db() as db:
        ensure_schema(db)


def _like(query):
    escaped = str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def _session_clause(group, alias="r"):
    """Whole-public-group scope unless a specific group/private session is named."""
    if not group:
        return f" AND {alias}.session LIKE 'group:%'", []
    if not SESSIONS.fullmatch(group):
        raise ApiError(400, "invalid_group", "群筛选格式无效")
    return f" AND {alias}.session=?", [group]


def _page(values, default=1, maximum=100):
    try:
        page = max(1, int(values.get("page", default)))
    except (TypeError, ValueError):
        page = 1
    try:
        size = max(1, min(int(values.get("page_size", 30)), maximum))
    except (TypeError, ValueError):
        size = 30
    return page, size


def _paginate(total, page, size):
    total = int(total)
    pages = max(1, (total + size - 1) // size)
    return {
        "page": page, "page_size": size, "total": total, "pages": pages,
        "next": page + 1 if page < pages else None,
        "has_more": page < pages, "truncated": total > size,
    }


def _name_map(db, ids, session=None):
    ids = [str(value) for value in ids if re.fullmatch(r"[1-9][0-9]{0,19}", str(value))]
    if not ids:
        return {}
    clause = " AND session=?" if session else " AND session LIKE 'group:%'"
    params = list(ids)
    if session:
        params.append(session)
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        "SELECT user_id,nickname,at FROM records WHERE user_id IN (" + marks + ") AND nickname!='' "
        "AND state!='deleted'" + clause + " ORDER BY at DESC", params,
    ).fetchall()
    names = {}
    for row in rows:
        names.setdefault(row["user_id"], str(row["nickname"])[:80])
    return names


def _self_ids(db):
    values = {DEFAULT_SELF_ID}
    try:
        for row in db.execute("SELECT DISTINCT self_id FROM records WHERE self_id!='' LIMIT 20"):
            if re.fullmatch(r"[1-9][0-9]{0,19}", str(row[0])):
                values.add(str(row[0]))
    except sqlite3.Error:
        pass
    return values
