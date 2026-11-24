"""Read-only catalogue of the old QQ memory: groups, members, reviews, handwritten notes."""
from __future__ import annotations

import sqlite3

from hakimi.memory import legacy_md_notes
from hakimi.social.identity_base import IdentityError, _clean_group, _read_json, _valid_group, _valid_user, source_root
from hakimi.social.identity_store import _columns, _open_read, _table_exists


def _old_conn(base):
    return _open_read(source_root(base) / "state/memory.sqlite3")


def _old_columns(conn):
    return _columns(conn, "records")


def _has_column(columns, name):
    return name in columns


def _row_get(row, key, default=""):
    try:
        value = row[key]
        return default if value is None else value
    except (IndexError, KeyError, TypeError):
        return default


def _old_group_stats(base):
    with _old_conn(base) as conn:
        columns = _old_columns(conn)
        if conn is None or not columns:
            return {}
        state = " AND r.state!='deleted'" if _has_column(columns, "state") else ""
        chats = ("SUM(CASE WHEN r.kind='chat' THEN 1 ELSE 0 END)"
                 if _has_column(columns, "kind") else "0")
        try:
            rows = conn.execute(
                "SELECT r.session,COUNT(*) n," + chats + " chats,MAX(r.at) last_at "
                "FROM records r WHERE r.session LIKE 'group:%'" + state +
                " GROUP BY r.session").fetchall()
        except sqlite3.Error:
            return {}
        notes = {}
        if "kind" in columns:
            try:
                for row in conn.execute(
                        "SELECT r.session,COUNT(*) n FROM records r WHERE r.session LIKE 'group:%' "
                        "AND r.kind NOT IN ('chat','dialogue')" + state + " GROUP BY r.session"):
                    notes[str(row[0])] = int(row[1] or 0)
            except sqlite3.Error:
                notes = {}
        result = {}
        for row in rows:
            session = str(row["session"] or "")
            if not session.startswith("group:"):
                continue
            gid = session.split(":", 1)[1]
            if not _valid_group(gid):
                continue
            result[gid] = {
                "id": gid, "records": int(row["n"] or 0), "chats": int(row["chats"] or 0),
                "notes": int(notes.get(session, 0)), "last_at": float(row["last_at"] or 0),
            }
        return result


def _review_data(base):
    review = _read_json(source_root(base) / "official_memory_review.json", {})
    if not isinstance(review, dict):
        review = {}
    groups = {}

    def block_for(gid):
        return groups.setdefault(gid, {
            "id": gid, "name": "", "group_notes": [], "pending_members": [],
            "sources": set(),
        })

    def note_text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("note", "text", "summary", "content"):
                if isinstance(value.get(key), str):
                    return value[key]
        return ""

    def add(gid, block, source):
        if not _valid_group(gid) or not isinstance(block, dict):
            return
        target = block_for(str(gid))
        target["sources"].add(source)
        if isinstance(block.get("name"), str) and block["name"].strip():
            target["name"] = block["name"].strip()[:120]
        raw_notes = block.get("group_notes") or []
        if isinstance(raw_notes, list):
            for index, note in enumerate(raw_notes):
                text = note_text(note)
                if text.strip():
                    target["group_notes"].append({
                        "index": len(target["group_notes"]), "text": text,
                        "kind": str(note.get("kind", "") if isinstance(note, dict) else ""),
                        "at": float(note.get("created_at", 0) or 0) if isinstance(note, dict) else 0.0,
                    })
        members = block.get("pending_members") or []
        if isinstance(members, list):
            for member in members:
                if not isinstance(member, dict):
                    continue
                old_qq = str(member.get("old_qq", "") or "").strip()
                if not _valid_user(old_qq):
                    continue
                notes = []
                for note in member.get("notes") or []:
                    text = note_text(note)
                    if text.strip():
                        notes.append(text)
                target["pending_members"].append({
                    "old_qq": old_qq,
                    "old_nickname": str(member.get("old_nickname", "") or "")[:80],
                    "notes": notes,
                })

    top_group = str(review.get("source_group", "") or "").strip()
    if top_group:
        add(top_group, review, "top")
    reviews = review.get("group_reviews")
    if isinstance(reviews, dict):
        for gid, block in reviews.items():
            add(str(gid), block, "group_reviews")
    return {"review": review, "groups": groups}


def _md_notes(base):
    """Read the old handwritten note file; failures are already fail-closed."""
    try:
        return legacy_md_notes.read_notes(source_root(base))
    except Exception:
        return []


def _md_group_headers(base):
    try:
        return legacy_md_notes.groups(source_root(base))
    except Exception:
        return []


def _group_note_preview(base, old_group, old_user=None, limit=5):
    """Preview old notes without reading any current/new-runtime row."""
    previews = []
    count = 0
    with _old_conn(base) as conn:
        columns = _old_columns(conn)
        if conn is not None and columns:
            state = " AND r.state!='deleted'" if _has_column(columns, "state") else ""
            kind = " AND r.kind NOT IN ('chat','dialogue')" if _has_column(columns, "kind") else ""
            clauses = ["r.session=?"]
            params = ["group:" + str(old_group)]
            if old_user is not None:
                clauses.append("r.user_id=?")
                params.append(str(old_user))
            else:
                if _has_column(columns, "user_id"):
                    clauses.append("(r.user_id='' OR r.user_id='0' OR r.user_id=?)")
                    params.append(str(old_group))
            originals = _table_exists(conn, "memory_originals")
            text_col = "r.text" if _has_column(columns, "text") else "''"
            summary_col = "r.summary" if _has_column(columns, "summary") else "''"
            body = (("COALESCE(o.text," + text_col + "," + summary_col + ")") if originals
                    else ("COALESCE(" + text_col + "," + summary_col + ")"))
            kind_col = "r.kind" if _has_column(columns, "kind") else "''"
            state_col = "r.state" if _has_column(columns, "state") else "'active'"
            source = "records r"
            if originals:
                source += (" LEFT JOIN memory_originals o ON o.id="
                           "(SELECT MAX(id) FROM memory_originals WHERE record_id=r.id)")
            sql = ("SELECT r.id,r.session,r.user_id,r.at," + body + " AS body," + kind_col +
                   " AS kind," + state_col + " AS state FROM " + source + " WHERE " +
                   " AND ".join(clauses) + state + kind +
                   " ORDER BY r.at DESC,r.id DESC LIMIT ?")
            try:
                rows = conn.execute(sql, [*params, max(1, min(int(limit) + 20, 80))]).fetchall()
            except sqlite3.Error:
                rows = []
            for row in rows:
                text = str(_row_get(row, "body", "") or "")
                if not text.strip():
                    continue
                count += 1
                if len(previews) < limit:
                    previews.append({
                        "memory_id": "L" + str(row["id"]),
                        "origin": "历史原文",
                        "kind": str(_row_get(row, "kind", "")),
                        "at": float(_row_get(row, "at", 0) or 0),
                        "old_user": str(_row_get(row, "user_id", "") or ""),
                        "preview": text[:160],
                    })
    review = _review_data(base)["groups"].get(str(old_group), {})
    if old_user is None:
        for note in review.get("group_notes", []):
            count += 1
            if len(previews) < limit:
                previews.append({
                    "memory_id": "R%s-g%s" % (old_group, note["index"]),
                    "origin": "旧摘要", "kind": "review", "at": float(note.get("at", 0) or 0),
                    "old_user": "", "preview": note["text"][:160],
                })
    if old_user is not None:
        for member in review.get("pending_members", []):
            if member.get("old_qq") != str(old_user):
                continue
            for index, note in enumerate(member.get("notes", [])):
                count += 1
                if len(previews) < limit:
                    previews.append({
                        "memory_id": "R%s-m%s-%s" % (old_group, old_user, index),
                        "origin": "旧摘要", "kind": "review", "at": 0.0,
                        "old_user": str(old_user), "preview": str(note)[:160],
                    })
    for note in _md_notes(base):
        if note["group_id"] != str(old_group):
            continue
        if old_user is not None and note["user_id"] != str(old_user):
            continue
        count += 1
        if len(previews) < limit:
            previews.append({
                "memory_id": note["id"].upper(),
                "origin": note["source_label"],
                "kind": note["source_kind"],
                "at": 0.0,
                "old_user": note["user_id"],
                "preview": note["text"][:160],
            })
    return {"count": count, "preview": previews, "no_old_notes": count == 0}


def _bias_nickname(base, old_user):
    config = _read_json(source_root(base) / "config.json", {})
    users = config.get("user_bias", {}).get("users", {}) if isinstance(config, dict) else {}
    item = users.get(str(old_user), {}) if isinstance(users, dict) else {}
    if isinstance(item, dict) and isinstance(item.get("nickname"), str):
        return item["nickname"][:80]
    return ""


def _old_members_records(base, old_group):
    with _old_conn(base) as conn:
        columns = _old_columns(conn)
        if conn is None or not columns:
            return {}
        state = " AND r.state!='deleted'" if _has_column(columns, "state") else ""
        nickname = "r.nickname" if _has_column(columns, "nickname") else "''"
        kind = "r.kind" if _has_column(columns, "kind") else "'chat'"
        try:
            rows = conn.execute(
                "SELECT r.user_id," + nickname + " AS nickname,r.at," + kind + " AS kind "
                "FROM records r WHERE r.session=? AND r.user_id GLOB '[1-9]*'" + state +
                " ORDER BY r.at DESC,r.id DESC", ("group:" + str(old_group),)).fetchall()
        except sqlite3.Error:
            return {}
        members = {}
        for row in rows:
            uid = str(row["user_id"] or "")
            if not _valid_user(uid):
                continue
            item = members.setdefault(uid, {
                "old_user": uid, "old_nickname": "", "records": 0, "notes": 0,
                "last_at": 0.0,
            })
            item["records"] += 1
            if not item["old_nickname"] and str(row["nickname"] or "").strip():
                item["old_nickname"] = str(row["nickname"])[:80]
            item["last_at"] = max(item["last_at"], float(row["at"] or 0))
            if str(row["kind"] or "") not in {"chat", "dialogue"}:
                item["notes"] += 1
        return members


def _old_user_record_nickname(base, old_user):
    with _old_conn(base) as conn:
        columns = _old_columns(conn)
        if conn is None or "nickname" not in columns:
            return ""
        try:
            row = conn.execute(
                "SELECT nickname FROM records WHERE user_id=? AND nickname!='' "
                "ORDER BY at DESC,id DESC LIMIT 1", (str(old_user),)).fetchone()
            return str(row[0])[:80] if row else ""
        except sqlite3.Error:
            return ""


def old_groups_catalog(base):
    stats = _old_group_stats(base)
    config = _read_json(source_root(base) / "config.json", {})
    weights = config.get("group_weights", {}) if isinstance(config, dict) else {}
    profiles = _read_json(source_root(base) / "group_profiles.json", {})
    profile_groups = profiles.get("groups", {}) if isinstance(profiles, dict) else {}
    review = _review_data(base)["groups"]
    md_headers = {str(item["group_id"]): item for item in _md_group_headers(base)}
    md_notes = _md_notes(base)
    md_counts = {}
    for note in md_notes:
        md_counts[note["group_id"]] = md_counts.get(note["group_id"], 0) + 1
    groups = {}
    for gid in (weights.keys() if isinstance(weights, dict) else []):
        if _valid_group(gid):
            groups.setdefault(str(gid), {"id": str(gid), "sources": set()})["sources"].add("config")
    for gid in (profile_groups.keys() if isinstance(profile_groups, dict) else []):
        if _valid_group(gid):
            groups.setdefault(str(gid), {"id": str(gid), "sources": set()})["sources"].add("group_profiles")
    for gid in review:
        groups.setdefault(str(gid), {"id": str(gid), "sources": set()})["sources"].add("review")
    for gid in set(md_headers) | set(md_counts):
        if _valid_group(gid):
            groups.setdefault(str(gid), {"id": str(gid), "sources": set()})["sources"].add("memory_md")
    for gid, item in stats.items():
        entry = groups.setdefault(gid, {"id": gid, "sources": set()})
        entry["sources"].add("memory")
    result = []
    for gid, entry in groups.items():
        weight = weights.get(gid, {}) if isinstance(weights, dict) else {}
        profile = profile_groups.get(gid, {}) if isinstance(profile_groups, dict) else {}
        review_group = review.get(gid, {})
        name = ""
        if isinstance(weight, dict) and isinstance(weight.get("name"), str):
            name = weight["name"].strip()
        if not name and isinstance(profile, dict) and isinstance(profile.get("name"), str):
            name = profile["name"].strip()
        if not name and isinstance(review_group.get("name"), str):
            name = review_group["name"].strip()
        if not name and gid in md_headers:
            name = str(md_headers[gid].get("name", "") or "").strip()
        stat = stats.get(gid, {})
        result.append({
            "id": gid,
            "name": name[:120] or ("旧群 " + gid),
            "records": int(stat.get("records", 0)),
            "chats": int(stat.get("chats", 0)),
            "notes": (int(stat.get("notes", 0)) + len(review_group.get("group_notes", []))
                      + int(md_counts.get(gid, 0))),
            "review_summaries": len(review_group.get("group_notes", [])),
            "handwritten_notes": int(md_counts.get(gid, 0)),
            "last_at": float(stat.get("last_at", 0) or 0),
            "sources": sorted(entry["sources"]),
        })
    result.sort(key=lambda item: (item["last_at"], item["id"]), reverse=True)
    return result


def old_group_catalog(base, old_group):
    for item in old_groups_catalog(base):
        if item["id"] == str(old_group):
            return item
    return None


def old_members_catalog(base, old_group, query=""):
    old_group = _clean_group(old_group, "old_group")
    if old_group_catalog(base, old_group) is None:
        raise IdentityError(404, "old_group_not_found", "旧群不在旧数据目录中")
    records = _old_members_records(base, old_group)
    review = _review_data(base)["groups"].get(old_group, {})
    members = {}
    for uid, item in records.items():
        members[uid] = dict(item, sources={"memory"}, nickname_source="old_records")
    for member in review.get("pending_members", []):
        uid = member["old_qq"]
        entry = members.setdefault(uid, {
            "old_user": uid, "old_nickname": "", "records": 0, "notes": 0,
            "last_at": 0.0, "sources": set(), "nickname_source": "review",
        })
        entry.setdefault("sources", set()).add("review")
        if not entry.get("old_nickname") and member.get("old_nickname"):
            entry["old_nickname"] = member["old_nickname"][:80]
            entry["nickname_source"] = "review"
        notes = len(member.get("notes", []))
        entry["notes"] = int(entry.get("notes", 0)) + notes
        entry["review_notes"] = int(entry.get("review_notes", 0)) + notes
    for note in _md_notes(base):
        if note["group_id"] != old_group:
            continue
        uid = note["user_id"]
        entry = members.setdefault(uid, {
            "old_user": uid, "old_nickname": "", "records": 0, "notes": 0,
            "last_at": 0.0, "sources": set(), "nickname_source": "memory_md",
        })
        entry.setdefault("sources", set()).add("memory_md")
        if not entry.get("old_nickname") and note.get("nickname"):
            entry["old_nickname"] = note["nickname"][:80]
            entry["nickname_source"] = "memory_md"
        entry["notes"] = int(entry.get("notes", 0)) + 1
        entry["handwritten_notes"] = int(entry.get("handwritten_notes", 0)) + 1
    q = str(query or "").strip().lower()[:120]
    result = []
    for uid, item in members.items():
        nickname = str(item.get("old_nickname", "") or "")
        nickname_source = str(item.get("nickname_source", "") or "")
        if not nickname:
            nickname = _bias_nickname(base, uid)
            if nickname:
                nickname_source = "user_bias"
        if not nickname:
            nickname = _old_user_record_nickname(base, uid)
            if nickname:
                nickname_source = "old_records"
        if q and q not in uid.lower() and q not in nickname.lower():
            continue
        preview_count = int(item.get("notes", 0))
        result.append({
            "old_user": uid,
            "old_nickname": nickname or ("旧QQ " + uid),
            "nickname_source": nickname_source or "unknown",
            "records": int(item.get("records", 0)),
            "notes": preview_count,
            "has_notes": preview_count > 0,
            "sources": sorted(item.get("sources", set()) or {"memory"}),
            "last_at": float(item.get("last_at", 0) or 0),
        })
    result.sort(key=lambda item: (item["last_at"], item["old_user"]), reverse=True)
    return result
