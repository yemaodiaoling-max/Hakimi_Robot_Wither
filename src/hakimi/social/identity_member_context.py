"""Read-only recent group messages used for manual identity verification.

The endpoint layer owns authentication.  This module only validates the two
public identifiers and reads a bounded window from the memory database.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
import sqlite3

__all__ = ["ContextError", "payload"]

_MEMBER_ID = re.compile(r"[1-9][0-9]{0,19}")
_EXCERPT_LIMIT = 240
_HINT = "仅供你核对身份，不会据此自动匹配"

_MESSAGE_SQL = """
SELECT id, text, summary, at
FROM records
WHERE session = ? AND user_id = ? AND kind = 'chat' AND state != 'deleted'
ORDER BY at DESC, id DESC
LIMIT 3
"""


class ContextError(Exception):
    """An error that the HTTP layer can map to a status and public code."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


def _invalid_group():
    return ContextError(400, "invalid_group", "群号格式无效")


def _invalid_user():
    return ContextError(400, "invalid_user", "成员编号格式无效")


def _numeric_id(value):
    if not isinstance(value, str):
        return None
    if _MEMBER_ID.fullmatch(value) is None:
        return None
    return value


def _identifiers(values):
    if not isinstance(values, Mapping):
        raise ContextError(400, "invalid_request", "参数格式无效")
    if set(values.keys()) != {"group", "user"}:
        raise ContextError(400, "invalid_request", "只接受 group 和 user 两个参数")

    raw_group = values.get("group")
    if isinstance(raw_group, str) and raw_group.startswith("group:"):
        raw_group = raw_group[len("group:"):]
    group = _numeric_id(raw_group)
    if group is None:
        raise _invalid_group()

    user = _numeric_id(values.get("user"))
    if user is None:
        raise _invalid_user()
    return group, user


def _database(base):
    try:
        path = Path(base).resolve() / "state" / "memory.sqlite3"
        ready = path.is_file()
    except (OSError, TypeError, ValueError) as exc:
        raise ContextError(503, "database_not_ready", "记忆库尚未初始化") from exc
    if not ready:
        raise ContextError(503, "database_not_ready", "记忆库尚未初始化")
    return path


def _read_rows(path, session, user):
    connection = None
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        return connection.execute(_MESSAGE_SQL, (session, user)).fetchall()
    except sqlite3.Error as exc:
        raise ContextError(503, "database_busy", "记忆库暂时不可读") from exc
    finally:
        if connection is not None:
            connection.close()


def payload(base, values):
    """Return the latest three observable messages for one group member."""
    group, user = _identifiers(values)
    path = _database(base)
    rows = _read_rows(path, "group:" + group, user)
    if not rows:
        raise ContextError(404, "member_not_observed", "该账号尚未在所选群出现")

    messages = []
    for row in rows:
        text = str(row["text"] or "").strip()
        summary = str(row["summary"] or "").strip()
        if text:
            excerpt = text[:_EXCERPT_LIMIT]
            label = "群内原话"
        elif summary:
            excerpt = summary[:_EXCERPT_LIMIT]
            label = "旧摘要"
        else:
            continue
        messages.append({
            "memory_id": "M" + str(row["id"]),
            "at": row["at"],
            "excerpt": excerpt,
            "label": label,
        })

    return {
        "group": group,
        "user": user,
        "messages": messages,
        "hint": _HINT,
    }
