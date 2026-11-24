"""Read-only retrieval of linked legacy memory for prompts and the operator console."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time

from hakimi.memory import legacy_md_notes
from hakimi.social.identity_base import LEGACY_MD_NOTE, LEGACY_RECORD_ID, LEGACY_REVIEW_GROUP, LEGACY_REVIEW_MEMBER, _old_root_id, _runtime_path, _valid_group, _valid_user, old_root_key, old_root_label, source_root
from hakimi.social.identity_edit import preview
from hakimi.social.identity_new_catalog import new_groups_catalog
from hakimi.social.identity_old_catalog import _group_note_preview, _old_columns, _old_conn, _review_data, _row_get, old_groups_catalog
from hakimi.social.identity_store import _group_link, _group_links, _member_link, _member_links, _open_read, _table_exists


def _mapping_group(conn, old_root, old_group, new_group=None, *, active_only=True):
    item = _group_link(conn, old_root, old_group=old_group, new_group=new_group,
                       active_only=active_only)
    return item


def _mapping_member(conn, old_root, old_group, old_user, *, active_only=True):
    return _member_link(conn, old_root, old_group=old_group, old_user=old_user,
                        active_only=active_only)


def _old_record_row(base, record_id, conn=None):
    close = False
    if conn is None:
        manager = _old_conn(base)
        conn = manager.__enter__()
        close = True
    try:
        columns = _old_columns(conn)
        if conn is None or not columns:
            return None
        originals = _table_exists(conn, "memory_originals")
        select = ["r.id", "r.session", "r.user_id"]
        for name, alias in (("kind", "kind"), ("at", "at"), ("state", "state"),
                            ("key", "key"), ("source", "source"),
                            ("verification", "verification"), ("text", "text"),
                            ("summary", "summary"), ("content_hash", "content_hash"),
                            ("original_id", "original_id"), ("model", "model"),
                            ("channel", "channel"), ("expires", "expires")):
            select.append("r." + name if name in columns else "NULL AS " + alias)
        if originals:
            select.extend(["o.text AS original_text", "o.content_hash AS original_hash",
                           "o.provider AS original_provider", "o.channel AS original_channel"])
        else:
            select.extend(["NULL AS original_text", "NULL AS original_hash",
                           "NULL AS original_provider", "NULL AS original_channel"])
        sql = "SELECT " + ",".join(select) + " FROM records r"
        if originals:
            sql += (" LEFT JOIN memory_originals o ON o.id="
                    "(SELECT MAX(id) FROM memory_originals WHERE record_id=r.id)")
        sql += " WHERE r.id=?"
        return conn.execute(sql, (int(record_id),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if close:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


def _legacy_text(row):
    text = str(_row_get(row, "original_text", "") or _row_get(row, "text", "")
               or _row_get(row, "summary", "") or "")
    digest = str(_row_get(row, "original_hash", "") or _row_get(row, "content_hash", "")
                 or hashlib.sha256(text.encode("utf-8")).hexdigest())
    return text, digest


def _legacy_origin(row):
    if (_row_get(row, "original_text", "") or _row_get(row, "text", "")):
        return "历史原文"
    return "旧摘要"


def _page_bounds(offset, limit):
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(1, min(int(limit or 3000), 6000))
    except (TypeError, ValueError):
        limit = 3000
    return offset, limit


def _paginate(item, offset, limit):
    text = str(item.get("text", "") or "")
    piece = text[offset:offset + limit]
    item.update({
        "text": piece, "offset": offset, "limit": limit, "total_chars": len(text),
        "returned_chars": len(piece),
        "next_offset": (offset + len(piece) if offset + len(piece) < len(text) else None),
        "has_more": offset + len(piece) < len(text),
        "excerpted": len(piece) < len(text),
    })
    return item


def _mapping_label(old_group, new_group, old_user="", new_user=""):
    text = "旧群%s→新群%s" % (old_group, new_group)
    if old_user:
        text += "；旧QQ%s→本地ID %s" % (old_user, new_user)
    return text


def _review_note_raw(base, legacy_id):
    """Resolve a fixed old-review id without consulting current mappings."""
    match = LEGACY_REVIEW_GROUP.fullmatch(legacy_id)
    member_match = LEGACY_REVIEW_MEMBER.fullmatch(legacy_id)
    if match:
        old_group, index = match.group(1), int(match.group(2))
        review = _review_data(base)["groups"].get(old_group, {})
        notes = review.get("group_notes", [])
        if index >= len(notes):
            return None
        return {
            "id": legacy_id.upper(), "kind": "review", "text": notes[index]["text"],
            "time": float(notes[index].get("at", 0) or 0), "old_group": old_group,
            "old_user": "", "origin_label": "旧摘要", "review_summary": True,
            "text_kind": "legacy_review_summary", "source": "official_memory_review",
        }
    if member_match:
        old_group, old_user = member_match.group(1), member_match.group(2)
        index = int(member_match.group(3))
        review = _review_data(base)["groups"].get(old_group, {})
        member = next((item for item in review.get("pending_members", [])
                       if item.get("old_qq") == old_user), None)
        if member is None or index >= len(member.get("notes", [])):
            return None
        return {
            "id": legacy_id.upper(), "kind": "review_member",
            "text": member["notes"][index], "time": 0.0, "old_group": old_group,
            "old_user": old_user, "origin_label": "旧摘要", "review_summary": True,
            "text_kind": "legacy_review_summary", "source": "official_memory_review",
        }
    return None


def _md_note_raw(base, legacy_id):
    match = LEGACY_MD_NOTE.fullmatch(legacy_id)
    if not match:
        return None
    canonical = "LM" + match.group(1).lower()
    try:
        note = legacy_md_notes.read_note(source_root(base), canonical)
    except Exception:
        note = None
    return note


def _read_review_note(base, legacy_id, conn, *, session, session_group, cross_groups):
    raw = _review_note_raw(base, legacy_id)
    if raw is None:
        return None
    old_group = raw["old_group"]
    old_user = raw["old_user"]
    link = _mapping_group(conn, old_root_key(base), old_group)
    if not link:
        return None
    new_group = link["new_group"]
    if not cross_groups and session_group and new_group != session_group:
        return None
    new_user = ""
    if old_user:
        member = _mapping_member(conn, old_root_key(base), old_group, old_user)
        if not member:
            return None
        new_user = member["new_user"]
    digest = hashlib.sha256(raw["text"].encode("utf-8")).hexdigest()
    return {
        **raw,
        "session_key": "group:" + new_group, "user_id": new_user,
        "hash": digest, "canonical_hash": digest, "legacy": True, "history": True,
        "new_group": new_group, "new_user": new_user,
        "identity_label": _mapping_label(old_group, new_group, old_user, new_user),
        "original_id": None, "provider": "local_operator",
        "state": "active",
    }


def _read_md_note(base, note, conn, *, session_group, cross_groups):
    old_group = str(note.get("group_id", "") or "")
    old_user = str(note.get("user_id", "") or "")
    if not _valid_group(old_group) or not _valid_user(old_user):
        return None
    link = _mapping_group(conn, old_root_key(base), old_group)
    if not link:
        return None
    new_group = link["new_group"]
    if not cross_groups and session_group and new_group != session_group:
        return None
    member = _mapping_member(conn, old_root_key(base), old_group, old_user)
    if not member:
        return None
    new_user = member["new_user"]
    return {
        "id": str(note["id"]).upper(), "kind": str(note.get("source_kind", "legacy_handwritten")),
        "session_key": "group:" + new_group, "user_id": new_user, "time": 0.0,
        "text": str(note.get("text", "")), "hash": str(note.get("hash", "")),
        "canonical_hash": str(note.get("hash", "")), "legacy": True, "history": True,
        "origin_label": str(note.get("source_label", "旧版手记（历史看法，尚未重新确认）")),
        "old_group": old_group, "new_group": new_group,
        "old_user": old_user, "new_user": new_user,
        "identity_label": _mapping_label(old_group, new_group, old_user, new_user),
        "source": str(note.get("source", "memory.md")), "review_summary": False,
        "source_label": str(note.get("source_label", "")),
        "text_kind": "legacy_handwritten", "original_id": None,
        "provider": "local_operator", "state": "active",
    }


def _operator_mapping_metadata(base, item):
    """Add current mapping labels for the operator view, never gate on them."""
    item.setdefault("new_group", "")
    item.setdefault("new_user", "")
    old_group = str(item.get("old_group", "") or "")
    old_user = str(item.get("old_user", "") or "")
    try:
        with _open_read(_runtime_path(base)) as conn:
            if conn is not None and old_group:
                link = _group_link(conn, old_root_key(base), old_group)
                if link:
                    item["new_group"] = link["new_group"]
                if old_user and _valid_user(old_user):
                    member = _member_link(conn, old_root_key(base), old_group, old_user)
                    if member:
                        item["new_group"] = member["new_group"]
                        item["new_user"] = member["new_user"]
    except (OSError, sqlite3.Error):
        pass
    if item.get("new_group"):
        if _valid_user(old_user) and not item.get("new_user"):
            item["identity_label"] = (
                "旧群%s→新群%s；旧QQ%s（成员尚未确认）"
                % (old_group, item["new_group"], old_user))
        else:
            item["identity_label"] = _mapping_label(
                old_group, item["new_group"], old_user if _valid_user(old_user) else "",
                item.get("new_user", ""))
    else:
        item["identity_label"] = "旧群%s（尚未确认匹配）" % old_group
    return item


def read_legacy(base, legacy_id, session, *, cross_groups=False, offset=0, limit=3000, now=None):
    """Read one old record/note through a currently active mapping.

    Model/tool reads always require the active group (and, for person notes, the
    active member) mapping.  A private session may use an explicit
    ``cross_groups=True`` scope for mapped *public* old groups, exactly like
    memory.search/read all-groups; the old source session itself must still be
    a group record.  Old private records are never readable.
    """
    legacy_id = str(legacy_id or "").strip()
    session = str(session or "")
    private_session = session.startswith("private:")
    if private_session and not cross_groups:
        return None
    session_group = ""
    if session.startswith("group:"):
        session_group = session.split(":", 1)[1]
        if not _valid_group(session_group):
            return None
    elif private_session:
        session_group = ""
    elif session:
        return None
    offset, limit = _page_bounds(offset, limit)
    now = time.time() if now is None else now
    with _open_read(_runtime_path(base)) as conn:
        if conn is None:
            return None
        note = _md_note_raw(base, legacy_id)
        if note is not None:
            item = _read_md_note(base, note, conn, session_group=session_group,
                                 cross_groups=cross_groups)
            return _paginate(item, offset, limit) if item is not None else None
        if LEGACY_REVIEW_GROUP.fullmatch(legacy_id) or LEGACY_REVIEW_MEMBER.fullmatch(legacy_id):
            note = _read_review_note(base, legacy_id, conn, session=session,
                                     session_group=session_group, cross_groups=cross_groups)
            return _paginate(note, offset, limit) if note is not None else None
        record_match = LEGACY_RECORD_ID.fullmatch(legacy_id)
        if not record_match:
            return None
        rid = int(record_match.group(1))
        row = _old_record_row(base, rid)
        if row is None:
            return None
        old_session = str(_row_get(row, "session", "") or "")
        if not old_session.startswith("group:"):
            return None
        old_group = old_session.split(":", 1)[1]
        link = _mapping_group(conn, old_root_key(base), old_group)
        if not link:
            return None
        new_group = link["new_group"]
        if not cross_groups and session_group and new_group != session_group:
            return None
        old_user = str(_row_get(row, "user_id", "") or "")
        new_user = ""
        kind = str(_row_get(row, "kind", "") or "")
        if _valid_user(old_user) and kind not in {
                "preference", "lore", "episode", "reflection", "commitment"}:
            member = _mapping_member(conn, old_root_key(base), old_group, old_user)
            if not member:
                return None
            new_user = member["new_user"]
        text, digest = _legacy_text(row)
        if not text:
            return None
        state = str(_row_get(row, "state", "active") or "active")
        expires = float(_row_get(row, "expires", 0) or 0)
        status = "expired" if expires and expires <= now else state
        origin_label = _legacy_origin(row)
        item = {
            "id": "L" + str(rid), "kind": kind, "session_key": "group:" + new_group,
            "user_id": new_user, "time": float(_row_get(row, "at", 0) or 0),
            "text": text, "hash": digest, "canonical_hash": digest,
            "legacy": True, "history": True, "origin_label": origin_label,
            "old_group": old_group, "new_group": new_group,
            "old_user": old_user if _valid_user(old_user) else "",
            "new_user": new_user,
            "identity_label": _mapping_label(
                old_group, new_group, old_user if _valid_user(old_user) else "", new_user),
            "source": str(_row_get(row, "source", "") or "") or "old_memory_database",
            "review_summary": origin_label == "旧摘要",
            "original_id": int(_row_get(row, "original_id")) if _row_get(row, "original_id") else None,
            "provider": str(_row_get(row, "original_provider", "")
                            or _row_get(row, "model", "") or _row_get(row, "channel", "") or ""),
            "state": status,
            "text_kind": "legacy_summary" if origin_label == "旧摘要" else "legacy_history",
        }
        return _paginate(item, offset, limit)


def read_for_operator(base, legacy_id, *, offset=0, limit=3000):
    """Admin-only read of one fixed old public source, without a current mapping.

    Accepts only known L/R/LM ids.  It never accepts a path, SQL, arbitrary file,
    or an old private record; pagination is the same as the model reader.
    """
    legacy_id = str(legacy_id or "").strip()
    offset, limit = _page_bounds(offset, limit)
    note = _md_note_raw(base, legacy_id)
    if note is not None:
        item = {
            "id": str(note["id"]).upper(),
            "kind": str(note.get("source_kind", "legacy_handwritten")),
            "session_key": "", "user_id": str(note.get("user_id", "")), "time": 0.0,
            "text": str(note.get("text", "")), "hash": str(note.get("hash", "")),
            "canonical_hash": str(note.get("hash", "")), "legacy": True, "history": True,
            "origin_label": str(note.get("source_label", "旧版手记（历史看法，尚未重新确认）")),
            "old_group": str(note.get("group_id", "")), "new_group": "", "old_user": str(note.get("user_id", "")),
            "new_user": "", "source": str(note.get("source", "memory.md")),
            "source_label": str(note.get("source_label", "")), "review_summary": False,
            "text_kind": "legacy_handwritten", "original_id": None,
            "provider": "local_operator", "state": "active",
        }
        return _paginate(_operator_mapping_metadata(base, item), offset, limit)
    raw = _review_note_raw(base, legacy_id)
    if raw is not None:
        item = {
            **raw, "session_key": "", "user_id": raw.get("old_user", ""),
            "hash": hashlib.sha256(raw["text"].encode("utf-8")).hexdigest(),
            "canonical_hash": hashlib.sha256(raw["text"].encode("utf-8")).hexdigest(),
            "legacy": True, "history": True, "new_group": "", "new_user": "",
            "original_id": None, "provider": "local_operator", "state": "active",
        }
        return _paginate(_operator_mapping_metadata(base, item), offset, limit)
    record_match = LEGACY_RECORD_ID.fullmatch(legacy_id)
    if not record_match:
        return None
    rid = int(record_match.group(1))
    row = _old_record_row(base, rid)
    if row is None:
        return None
    old_session = str(_row_get(row, "session", "") or "")
    if not old_session.startswith("group:"):
        return None
    old_group = old_session.split(":", 1)[1]
    if not _valid_group(old_group):
        return None
    text, digest = _legacy_text(row)
    if not text:
        return None
    old_user = str(_row_get(row, "user_id", "") or "")
    origin_label = _legacy_origin(row)
    item = {
        "id": "L" + str(rid), "kind": str(_row_get(row, "kind", "") or ""),
        "session_key": "", "user_id": "", "time": float(_row_get(row, "at", 0) or 0),
        "text": text, "hash": digest, "canonical_hash": digest,
        "legacy": True, "history": True, "origin_label": origin_label,
        "old_group": old_group, "new_group": "",
        "old_user": old_user if _valid_user(old_user) else "", "new_user": "",
        "source": str(_row_get(row, "source", "") or "") or "old_memory_database",
        "review_summary": origin_label == "旧摘要",
        "original_id": int(_row_get(row, "original_id")) if _row_get(row, "original_id") else None,
        "provider": str(_row_get(row, "original_provider", "")
                        or _row_get(row, "model", "") or _row_get(row, "channel", "") or ""),
        "state": str(_row_get(row, "state", "active") or "active"),
        "text_kind": "legacy_summary" if origin_label == "旧摘要" else "legacy_history",
    }
    return _paginate(_operator_mapping_metadata(base, item), offset, limit)


def _query_terms(messages, target, query=None):
    text = str(query or "")
    if not text:
        text = " ".join(str(item.get("text", "")) for item in (messages or [])[-6:])
        text += " " + str((target or {}).get("text", ""))
    try:
        from hakimi.memory.text import terms
        return terms(text[:1200])
    except Exception:
        return {word for word in re.findall(r"[\w\u4e00-\u9fff]{2,}", text.lower())}


def _note_relevant(text, terms):
    if not terms:
        return True
    lowered = str(text).lower()
    return any(str(term).lower() in lowered for term in terms)


_RECALL_QUERY = re.compile(r"记得|认得|认识我|我是谁|以前|之前|见过|称呼|叫我|名字|昵称")


def _recall_query(text):
    return bool(_RECALL_QUERY.search(str(text or "")))


def _legacy_label(item):
    if item.get("review_summary"):
        return "旧摘要"
    if item.get("text_kind") == "legacy_handwritten" or item.get("source") == "memory.md":
        return "旧版手记"
    return "历史原文"


def _legacy_head(item):
    head = "[%s] %s·旧群%s→新群%s" % (
        item.get("id", ""), _legacy_label(item), item.get("old_group", ""),
        item.get("new_group", ""))
    if item.get("old_user"):
        head += "·人物已匹配"
    at = item.get("time") or 0
    if at:
        try:
            head += "·" + time.strftime("%Y-%m-%d", time.gmtime(float(at)))
        except (ValueError, OSError):
            pass
    return head


def _legacy_tail(item):
    if item.get("text_kind") == "legacy_handwritten" or item.get("source") == "memory.md":
        return "\n来源：旧版手记（历史看法，尚未重新确认，不是当前新话）"
    if item.get("review_summary"):
        return "\n来源：旧摘要（不是当前新话）"
    return "\n来源：旧记录（历史原文，不是当前新话）"


def _legacy_line(item):
    head = _legacy_head(item)
    line = head + "：" + str(item.get("text", "")).replace("\n", " ")[:220]
    line += _legacy_tail(item)
    return line


def retrieve_legacy_notes(base, session, focus, messages, *, target=None, query=None,
                          cross_groups=False, budget=600, limit=2, now=None):
    """Return at most two matched legacy notes within the caller's remaining budget."""
    session = str(session or "")
    private_session = session.startswith("private:")
    if private_session and not cross_groups:
        return []
    session_group = ""
    if session.startswith("group:"):
        session_group = session.split(":", 1)[1]
        if not _valid_group(session_group):
            return []
    elif not private_session:
        return []
    try:
        limit = max(0, min(int(limit), 2))
    except (TypeError, ValueError):
        limit = 2
    if limit <= 0:
        return []
    with _open_read(_runtime_path(base)) as conn:
        if conn is None:
            return []
        groups = _group_links(conn, old_root_key(base))
        if cross_groups:
            allowed = {item["new_group"]: item for item in groups}
        else:
            allowed = {item["new_group"]: item for item in groups
                       if item["new_group"] == session_group}
        if not allowed:
            return []
        focus_members = [item for item in _member_links(conn, old_root_key(base),
                                                        new_user=str(focus or ""))
                         if item["new_group"] in allowed]
        focus_members.sort(key=lambda item: item["new_group"] != session_group)
    query_text = str(query or "")
    if not query_text:
        query_text = " ".join(str(item.get("text", "")) for item in (messages or [])[-6:])
        query_text += " " + str((target or {}).get("text", ""))
    query_terms = _query_terms(messages, target, query)
    recall = _recall_query(query_text)
    chosen = []
    seen = set()

    def add_item(item):
        if item is None or item["id"] in seen:
            return
        if not recall and not _note_relevant(item.get("text", ""), query_terms):
            return
        seen.add(item["id"])
        chosen.append(item)

    # Focus person's matched notes first (at most two total).
    for member in focus_members:
        for preview in _group_note_preview(base, member["old_group"],
                                           old_user=member["old_user"], limit=12)["preview"]:
            item = read_legacy(base, preview["memory_id"], session,
                               cross_groups=cross_groups, limit=220)
            if item is not None:
                add_item(item)
            if len(chosen) >= limit:
                break
        if len(chosen) >= limit:
            break
    if len(chosen) < limit:
        for new_group, _link in allowed.items():
            if new_group != session_group and not cross_groups:
                continue
            preview = _group_note_preview(base, _link["old_group"], limit=12)
            for item_preview in preview["preview"]:
                legacy_id = item_preview["memory_id"]
                # Group-level review/db notes only; member notes (including
                # handwritten LM notes) are included through focus mapping.
                if "-m" in legacy_id.lower() or legacy_id.upper().startswith("LM"):
                    continue
                if legacy_id.upper().startswith("L"):
                    with _old_conn(base) as old:
                        row = _old_record_row(base, int(legacy_id[1:]), conn=old)
                    if row is None:
                        continue
                    old_user = str(_row_get(row, "user_id", "") or "")
                    if _valid_user(old_user) and old_user not in {"0", _link["old_group"]}:
                        continue
                item = read_legacy(base, legacy_id, session,
                                   cross_groups=cross_groups, limit=220)
                add_item(item)
                if len(chosen) >= limit:
                    break
            if len(chosen) >= limit:
                break

    result = []
    remaining = max(0, int(budget))
    for item in chosen[:limit]:
        line = _legacy_line(item)
        if len(line) > remaining:
            head = _legacy_head(item)
            tail = _legacy_tail(item)
            room = remaining - len(head) - len(tail) - 1
            if room < 16:
                continue
            line = head + "：" + str(item.get("text", "")).replace("\n", " ")[:room] + tail
        result.append(line)
        remaining -= len(line)
        if remaining <= 0:
            break
    return result


def identity_history_for_person(base, new_user, group=""):
    """Small history metadata for the current person panel."""
    new_user = str(new_user or "").strip()
    if not _valid_user(new_user):
        return None
    group_id = ""
    if str(group or "").startswith("group:"):
        group_id = str(group).split(":", 1)[1]
    elif group:
        group_id = str(group)
    with _open_read(_runtime_path(base)) as conn:
        if conn is None:
            return None
        members = _member_links(conn, old_root_key(base), new_user=new_user)
    if group_id:
        members = [item for item in members if item["new_group"] == group_id]
    if not members:
        return None
    old_names = {item["id"]: item["name"] for item in old_groups_catalog(base)}
    new_names = {item["id"]: item["name"] for item in new_groups_catalog(base)}
    rows = []
    for item in members:
        notes = []
        for preview in _group_note_preview(base, item["old_group"],
                                           old_user=item["old_user"], limit=3)["preview"]:
            notes.append({
                "memory_id": preview["memory_id"], "origin": preview["origin"],
                "preview": preview["preview"], "at": preview["at"],
            })
        rows.append({
            "old_group": item["old_group"],
            "old_group_name": old_names.get(item["old_group"], "旧群 " + item["old_group"]),
            "new_group": item["new_group"],
            "new_group_name": new_names.get(item["new_group"], "新群 " + item["new_group"]),
            "old_user": item["old_user"], "new_user": item["new_user"],
            "linked_at": float(item.get("created_at", 0) or 0),
            "notes": notes,
            "has_notes": bool(notes),
            "label": _mapping_label(item["old_group"], item["new_group"],
                                    item["old_user"], item["new_user"]),
        })
    return {
        "available": bool(rows),
        "old_root": {"id": _old_root_id(base), "label": old_root_label(base)},
        "links": rows,
        "labels": {"record": "历史原文", "review": "旧摘要",
                   "handwritten": "旧版手记（历史看法，尚未重新确认）",
                   "not_current": "旧记录，不是当前新发生的话"},
    }
