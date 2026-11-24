"""Read-only catalogue of current official-channel groups and members, and link listings."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hakimi.social.identity_base import IdentityError, REVISION, _clean_group, _old_root_id, _runtime_path, _valid_group, _valid_user, old_root_key, old_root_label
from hakimi.social.identity_old_catalog import old_groups_catalog
from hakimi.social.identity_store import _columns, _group_links, _member_links, _open_read, _revision_conn, _table_exists


def _route_data(base):
    """Read only group routes/names; never expose openid/remote ids."""
    path = Path(base).resolve() / "state/official_routes.sqlite3"
    with _open_read(path) as conn:
        if conn is None or not _table_exists(conn, "settings"):
            return {}, set()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='group_routes'").fetchone()
            routes = json.loads(row[0]) if row else {}
        except (sqlite3.Error, ValueError, TypeError):
            routes = {}
        actors = set()
        if _table_exists(conn, "actors"):
            try:
                actors = {str(item[0]) for item in conn.execute("SELECT local_id FROM actors")}
            except sqlite3.Error:
                actors = set()
        result = {}
        if isinstance(routes, dict):
            for value in routes.values():
                if not isinstance(value, dict):
                    continue
                local_id = str(value.get("local_id", "") or "")
                if _valid_group(local_id):
                    result[local_id] = {
                        "id": local_id,
                        "name": str(value.get("name", "") or "")[:120],
                        "active": bool(value.get("active", True)),
                    }
        return result, actors


def _runtime_group_stats(conn):
    columns = _columns(conn, "records")
    if conn is None or not columns:
        return {}
    state = " AND r.state!='deleted'" if "state" in columns else ""
    try:
        rows = conn.execute(
            "SELECT r.session,COUNT(*) n,"
            "SUM(CASE WHEN r.kind='chat' THEN 1 ELSE 0 END) chats,MAX(r.at) last_at "
            "FROM records r WHERE r.session LIKE 'group:%'" + state +
            " GROUP BY r.session").fetchall()
    except sqlite3.Error:
        return {}
    result = {}
    for row in rows:
        session = str(row["session"] or "")
        gid = session.split(":", 1)[1] if session.startswith("group:") else ""
        if not _valid_group(gid):
            continue
        result[gid] = {
            "id": gid, "records": int(row["n"] or 0), "chats": int(row["chats"] or 0),
            "last_at": float(row["last_at"] or 0),
        }
    return result


def new_groups_catalog(base, conn=None):
    routes, _actors = _route_data(base)
    if conn is not None:
        stats = _runtime_group_stats(conn)
    else:
        with _open_read(_runtime_path(base)) as reader:
            stats = _runtime_group_stats(reader)
    groups = {}
    for gid, item in stats.items():
        groups[gid] = dict(item, sources={"runtime_records"})
    for gid, route in routes.items():
        entry = groups.setdefault(gid, {"id": gid, "records": 0, "chats": 0,
                                        "last_at": 0.0, "sources": set()})
        entry["sources"].add("official_route")
        entry["name"] = route.get("name", "")
        entry["route_active"] = bool(route.get("active", True))
    result = []
    for gid, item in groups.items():
        result.append({
            "id": gid,
            "name": str(item.get("name", "") or "")[:120] or ("新群 " + gid),
            "records": int(item.get("records", 0)),
            "chats": int(item.get("chats", 0)),
            "last_at": float(item.get("last_at", 0) or 0),
            "route_active": bool(item.get("route_active", False)),
            "sources": sorted(item.get("sources", set())),
        })
    result.sort(key=lambda item: (item["last_at"], item["id"]), reverse=True)
    return result


def new_group_catalog(base, new_group, conn=None):
    for item in new_groups_catalog(base, conn=conn):
        if item["id"] == str(new_group):
            return item
    return None


def _new_members_records_conn(conn, new_group):
    columns = _columns(conn, "records")
    if conn is None or not columns:
        return {}
    state = " AND r.state!='deleted'" if "state" in columns else ""
    nickname = "r.nickname" if "nickname" in columns else "''"
    try:
        rows = conn.execute(
            "SELECT r.user_id," + nickname + " AS nickname,COUNT(*) n,MAX(r.at) last_at "
            "FROM records r WHERE r.session=? AND r.user_id GLOB '[1-9]*'" + state +
            " GROUP BY r.user_id ORDER BY last_at DESC,r.user_id DESC",
            ("group:" + str(new_group),)).fetchall()
    except sqlite3.Error:
        return {}
    result = {}
    for row in rows:
        uid = str(row["user_id"] or "")
        if _valid_user(uid):
            result[uid] = {
                "new_user": uid, "new_nickname": str(row["nickname"] or "")[:80],
                "records": int(row["n"] or 0), "last_at": float(row["last_at"] or 0),
            }
    return result


def new_members_catalog(base, new_group, query="", conn=None):
    new_group = _clean_group(new_group, "new_group")
    if new_group_catalog(base, new_group, conn=conn) is None:
        raise IdentityError(404, "new_group_not_found", "新群不在官方runtime记录或合法route中")
    if conn is None:
        with _open_read(_runtime_path(base)) as reader:
            members = _new_members_records_conn(reader, new_group)
    else:
        members = _new_members_records_conn(conn, new_group)
    q = str(query or "").strip().lower()[:120]
    result = []
    for uid, item in members.items():
        if q and q not in uid.lower() and q not in str(item.get("new_nickname", "")).lower():
            continue
        result.append({
            "new_user": uid,
            "new_nickname": str(item.get("new_nickname", "") or "") or ("用户 " + uid),
            "records": int(item.get("records", 0)),
            "last_at": float(item.get("last_at", 0) or 0),
            "source": "runtime_records",
        })
    result.sort(key=lambda item: (item["last_at"], item["new_user"]), reverse=True)
    return result


def catalog(base):
    """GET-only catalogue.  Does not initialise or write any table."""
    with _open_read(_runtime_path(base)) as conn:
        groups = new_groups_catalog(base, conn=conn)
        group_links = _group_links(conn, old_root_key(base))
        member_links = _member_links(conn, old_root_key(base))
        current_revision = _revision_conn(conn)
    old_groups = old_groups_catalog(base)
    return {
        "version": REVISION,
        "revision": current_revision,
        "old_root": {"id": _old_root_id(base), "label": old_root_label(base)},
        "old_groups": old_groups,
        "new_groups": groups,
        "counts": {
            "old_groups": len(old_groups), "new_groups": len(groups),
            "group_links": len(group_links), "member_links": len(member_links),
        },
        "labels": {
            "old_group": "旧群", "new_group": "新通道群",
            "old_member": "旧QQ号", "new_member": "新通道成员",
            "review": "旧摘要", "record": "历史原文", "handwritten": "旧版手记",
            "no_old_notes": "尚无旧笔记",
        },
    }


def _public_group_link(item):
    return {
        "old_group": item["old_group"], "new_group": item["new_group"],
        "created_revision": int(item.get("created_revision", 0) or 0),
        "created_at": float(item.get("created_at", 0) or 0),
        "updated_at": float(item.get("updated_at", 0) or 0),
    }


def _public_member_link(item):
    return {
        "old_group": item["old_group"], "new_group": item["new_group"],
        "old_user": item["old_user"], "new_user": item["new_user"],
        "created_revision": int(item.get("created_revision", 0) or 0),
        "created_at": float(item.get("created_at", 0) or 0),
        "updated_at": float(item.get("updated_at", 0) or 0),
    }


def links_payload(base):
    """GET-only active mapping list plus recent audit; never leaks route OpenIDs."""
    with _open_read(_runtime_path(base)) as conn:
        groups = _group_links(conn, old_root_key(base))
        members = _member_links(conn, old_root_key(base))
        current_revision = _revision_conn(conn)
        audit = []
        if _table_exists(conn, "identity_audit"):
            try:
                rows = conn.execute(
                    "SELECT revision,at,action,old_group,old_user,new_group,new_user,"
                    "operator,operator_source,detail FROM identity_audit "
                    "WHERE old_root=? ORDER BY id DESC LIMIT 30", (old_root_key(base),)).fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                try:
                    detail = json.loads(row["detail"] or "{}")
                except (TypeError, ValueError):
                    detail = {}
                audit.append({
                    "revision": int(row["revision"] or 0), "at": float(row["at"] or 0),
                    "action": row["action"], "old_group": row["old_group"],
                    "old_user": row["old_user"], "new_group": row["new_group"],
                    "new_user": row["new_user"], "operator": row["operator"],
                    "operator_source": row["operator_source"],
                    "detail": detail if isinstance(detail, dict) else {},
                })
    old_names = {item["id"]: item["name"] for item in old_groups_catalog(base)}
    new_names = {item["id"]: item["name"] for item in new_groups_catalog(base)}
    after = []
    for item in groups:
        public = _public_group_link(item)
        public["old_name"] = old_names.get(item["old_group"], "旧群 " + item["old_group"])
        public["new_name"] = new_names.get(item["new_group"], "新群 " + item["new_group"])
        after.append(public)
    member_rows = []
    for item in members:
        public = _public_member_link(item)
        public["old_group_name"] = old_names.get(item["old_group"], "旧群 " + item["old_group"])
        public["new_group_name"] = new_names.get(item["new_group"], "新群 " + item["new_group"])
        member_rows.append(public)
    return {
        "revision": current_revision,
        "old_root": {"id": _old_root_id(base), "label": old_root_label(base)},
        "group_links": after,
        "member_links": member_rows,
        "audit": audit,
        "operator": "local_operator",
    }
