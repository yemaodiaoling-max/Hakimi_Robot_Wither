"""Console API: status, groups, memory lists and single memories."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from hakimi.console.errors import ApiError
from hakimi.console.console_db import KINDS, _like, _page, _paginate, _path, _session_clause, db_conn
from hakimi.models import vertex_route
from hakimi.social import identity_links


def status_payload(base, started_at):
    with db_conn(base) as db:
        records = db.execute("SELECT COUNT(*) n,MAX(id) max_id,MAX(at) max_at FROM records").fetchone()
        chats = db.execute(
            "SELECT COUNT(*) n FROM records WHERE kind='chat' AND state!='deleted' "
            "AND session LIKE 'group:%'").fetchone()[0]
        people = db.execute(
            "SELECT COUNT(DISTINCT user_id) n FROM records WHERE kind='chat' AND user_id GLOB '[1-9]*' "
            "AND state!='deleted' AND session LIKE 'group:%'").fetchone()[0]
        groups = db.execute(
            "SELECT COUNT(DISTINCT session) n FROM records WHERE state!='deleted' "
            "AND session LIKE 'group:%'").fetchone()[0]
        originals = db.execute("SELECT COUNT(*) n,MAX(id) max_id,MAX(created_at) max_at FROM memory_originals").fetchone()
        try:
            assessments = db.execute(
                "SELECT COUNT(*) n,MAX(id) max_id,MAX(created_at) max_at FROM social_assessments "
                "WHERE state='active' AND session LIKE 'group:%'").fetchone()
        except sqlite3.Error:
            assessments = None
        updated = max(filter(None, [records["max_at"], originals["max_at"],
                                    assessments["max_at"] if assessments else 0]), default=0)
    path = _path(base)
    stamps = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            info = candidate.stat()
            stamps.append(f"{info.st_mtime_ns}:{info.st_size}")
        except OSError:
            pass
    raw = "|".join(stamps + [str(records["max_id"]), str(originals["max_id"]),
                             str(assessments["max_id"] if assessments else 0)])
    return {
        "version": hashlib.sha256(raw.encode()).hexdigest()[:16],
        "updated_at": float(updated or 0),
        "started_at": started_at,
        "counts": {
            "records": int(records["n"]), "chats": int(chats), "people": int(people),
            "groups": int(groups), "manual_originals": int(originals["n"]),
            "assessments": int(assessments["n"] if assessments else 0),
        },
    }


def vertex_budget_payload(base):
    """Read-only cache view; never queries the gateway or returns a fake zero."""
    return vertex_route.console_status(base)


def groups_payload(base):
    from hakimi.console.group_catalog import decorate_groups
    with db_conn(base) as db:
        rows = db.execute(
            "SELECT session,COUNT(*) n,SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END) chats,MAX(at) last_at "
            "FROM records WHERE state!='deleted' AND session LIKE 'group:%' "
            "GROUP BY session ORDER BY last_at DESC").fetchall()
    groups = decorate_groups(base, [{
        "session": row["session"], "records": int(row["n"]),
        "chats": int(row["chats"] or 0), "last_at": row["last_at"],
    } for row in rows])
    total = len(groups)
    groups = groups[:500]
    return {"groups": groups, "total": total, "returned": len(groups), "truncated": total > len(groups)}


def memories_payload(base, values):
    q = str(values.get("q", "") or "").strip()[:120]
    group = str(values.get("group", "") or "").strip()
    person = str(values.get("person", "") or "").strip()
    kind = str(values.get("kind", "") or "").strip()
    page, size = _page(values, 1, 60)
    clause, params = _session_clause(group)
    where = ["r.state!='deleted'"]
    args = []
    if person:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", person):
            raise ApiError(400, "invalid_person", "人物ID格式无效")
        where.append("r.user_id=?")
        args.append(person)
    if kind:
        if kind not in KINDS:
            raise ApiError(400, "invalid_kind", "记忆类型无效")
        where.append("r.kind=?")
        args.append(kind)
    if q:
        where.append("(r.text LIKE ? ESCAPE '\\' OR r.key LIKE ? ESCAPE '\\' OR o.text LIKE ? ESCAPE '\\')")
        args.extend([_like(q)] * 3)
    sql_where = " AND ".join(where) + clause
    args.extend(params)
    with db_conn(base) as db:
        total = db.execute(
            "SELECT COUNT(*) FROM records r LEFT JOIN memory_originals o ON o.record_id=r.id WHERE "
            + sql_where, args).fetchone()[0]
        rows = db.execute(
            "SELECT r.id,r.kind,r.session,r.user_id,r.key,r.at,r.state,r.original_id,r.model,r.channel,"
            "COALESCE(o.text,r.text) AS body,o.content_hash,o.provider FROM records r "
            "LEFT JOIN memory_originals o ON o.record_id=r.id WHERE " + sql_where +
            " ORDER BY r.at DESC,r.id DESC LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size]).fetchall()
    items = [{
        "memory_id": "M" + str(row["id"]), "kind": row["kind"], "session": row["session"],
        "user_id": row["user_id"], "key": row["key"], "time": row["at"], "state": row["state"],
        "preview": str(row["body"] or "")[:180], "has_original": bool(row["original_id"]),
        "hash": row["content_hash"], "provider": row["provider"] or row["model"] or row["channel"],
    } for row in rows]
    paging = _paginate(total, page, size)
    paging["returned"] = len(items)
    return {"query": q, "group": group or "all", "kind": kind or "all",
            "person": person or "all", "memories": items, **paging}


def memory_payload(base, values):
    raw = str(values.get("id", "") or "").strip()
    if re.fullmatch(r"(?:L[1-9][0-9]{0,19}|LM[0-9a-f]{24}|R[1-9][0-9]{0,19}-[gm][0-9-]+)",
                    raw, re.IGNORECASE):
        group = str(values.get("group", "") or "").strip()
        session = group if group.startswith(("group:", "private:")) else (
            "group:" + group if group else "")
        try:
            offset = max(0, int(values.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = max(100, min(int(values.get("limit", 2000)), 6000))
        except (TypeError, ValueError):
            limit = 2000
        record = identity_links.read_legacy(
            base, raw, session,
            cross_groups=str(values.get("scope", "") or "") == "all_groups",
            offset=offset, limit=limit)
        if record is None:
            raise ApiError(404, "memory_not_found", "旧记忆不存在或对应群/成员尚未确认匹配")
        return record
    raw = raw.upper().removeprefix("M")
    if not raw.isdigit() or not re.fullmatch(r"[1-9][0-9]{0,19}", raw):
        raise ApiError(400, "invalid_memory", "M编号格式无效")
    try:
        offset = max(0, int(values.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(100, min(int(values.get("limit", 2000)), 6000))
    except (TypeError, ValueError):
        limit = 2000
    rid = int(raw)
    with db_conn(base) as db:
        row = db.execute(
            "SELECT r.*,o.text AS original_text,o.content_hash AS original_hash,o.provider AS original_provider,"
            "o.channel AS original_channel FROM records r "
            "LEFT JOIN memory_originals o ON o.record_id=r.id WHERE r.id=?", (rid,)).fetchone()
        if not row:
            raise ApiError(404, "memory_not_found", "记忆不存在")
        if row["state"] == "deleted":
            raise ApiError(404, "memory_not_found", "记忆不存在")
        if row["original_id"]:
            original = db.execute("SELECT * FROM memory_originals WHERE id=?", (row["original_id"],)).fetchone()
        else:
            original = db.execute("SELECT * FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                                  (rid,)).fetchone()
        text = str(original["text"] if original else (row["text"] or row["summary"] or ""))
        versions = []
        if row["kind"] != "chat" and row["key"]:
            versions = db.execute(
                "SELECT id,state,at FROM records WHERE kind=? AND session=? AND user_id=? AND key=? "
                "AND original_id IS NOT NULL AND state!='deleted' ORDER BY at,id",
                (row["kind"], row["session"], row["user_id"], row["key"])).fetchall()
        newer = db.execute("SELECT id FROM records WHERE id=?", (row["superseded_by"],)).fetchone() \
            if row["superseded_by"] else None
    piece = text[offset:offset + limit]
    next_offset = offset + len(piece) if offset + len(piece) < len(text) else None
    return {
        "memory_id": "M" + str(rid), "kind": row["kind"], "session": row["session"],
        "user_id": row["user_id"], "key": row["key"], "time": row["at"], "state": row["state"],
        "text": piece, "text_kind": ("manual_original" if original and not original["legacy"]
                                     else "legacy_note" if original else
                                     "raw_chat" if row["kind"] == "chat" else "knowledge_note"),
        "hash": (original["content_hash"] if original else row["content_hash"]),
        "provider": (original["provider"] if original else row["model"]),
        "channel": (original["channel"] if original else row["channel"]),
        "source": row["source"], "evidence": row["evidence"], "verification": row["verification"],
        "legacy": bool(original and original["legacy"]),
        "supersedes": "M" + str(row["supersedes_id"]) if row["supersedes_id"] else None,
        "superseded_by": "M" + str(newer[0]) if newer else None,
        "versions": [{"memory_id": "M" + str(item["id"]), "state": item["state"], "time": item["at"]}
                     for item in versions],
        "offset": offset, "limit": limit, "total_chars": len(text), "returned_chars": len(piece),
        "next_offset": next_offset, "has_more": next_offset is not None,
        "truncated": len(piece) < len(text),
    }
