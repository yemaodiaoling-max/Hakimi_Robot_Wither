"""Validate, preview and apply operator identity links."""
from __future__ import annotations

import json
import sqlite3
import time

from hakimi.social.identity_base import IdentityError, LINK_ACTIONS, _REQUEST_FIELDS, _clean_group, _clean_user, _old_root_id, _runtime_path, old_root_key, old_root_label
from hakimi.social.identity_new_catalog import _public_member_link, new_groups_catalog, new_members_catalog
from hakimi.social.identity_old_catalog import _bias_nickname, _group_note_preview, old_groups_catalog, old_members_catalog
from hakimi.social.identity_store import _group_link, _member_link, _member_links, _open_read, _open_write, _revision_conn, ensure_schema


def _normalise_request(data, *, require_confirm=False, require_expected=False):
    if not isinstance(data, dict):
        raise IdentityError(400, "invalid_json", "请求必须是JSON对象")
    action = str(data.get("action", "") or "").strip()
    if action not in LINK_ACTIONS:
        raise IdentityError(400, "invalid_action", "action必须是link/unlink group或member")
    extra = set(data) - _REQUEST_FIELDS[action]
    if extra:
        raise IdentityError(400, "unknown_field", "拒绝无关字段：" + ",".join(sorted(extra)[:8]))
    request = {"action": action}
    request["old_group"] = _clean_group(data.get("old_group"), "old_group")
    request["new_group"] = _clean_group(data.get("new_group"), "new_group")
    if action in {"link_member", "unlink_member"}:
        request["old_user"] = _clean_user(data.get("old_user"), "old_user")
        request["new_user"] = _clean_user(data.get("new_user"), "new_user")
    if "expected_revision" in data:
        value = data.get("expected_revision")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IdentityError(400, "invalid_revision", "expected_revision必须是非负整数")
        request["expected_revision"] = value
    elif require_expected:
        raise IdentityError(400, "revision_required", "写操作必须提供expected_revision")
    if require_confirm and data.get("confirm") is not True:
        raise IdentityError(400, "confirm_required", "写操作必须明确confirm:true")
    if "confirm" in data and not isinstance(data.get("confirm"), bool):
        raise IdentityError(400, "invalid_confirm", "confirm只能是布尔值")
    return request


def _conflict(code, message):
    return {"code": code, "message": message}


def _preview_group_link(base, request, conn, old_groups, new_groups):
    old_group = request["old_group"]
    new_group = request["new_group"]
    old_item = old_groups.get(old_group)
    new_item = new_groups.get(new_group)
    conflicts = []
    if old_item is None:
        raise IdentityError(404, "old_group_not_found", "旧群不在旧数据目录中")
    if new_item is None:
        raise IdentityError(404, "new_group_not_found", "新群不在官方runtime记录或合法route中")
    existing = _group_link(conn, old_root_key(base), old_group=old_group)
    reverse = _group_link(conn, old_root_key(base), new_group=new_group)
    already = bool(existing and existing["new_group"] == new_group)
    if existing and existing["new_group"] != new_group:
        conflicts.append(_conflict("old_group_conflict", "该旧群已匹配到另一个新群"))
    if reverse and reverse["old_group"] != old_group:
        conflicts.append(_conflict("new_group_conflict", "该新群已匹配到另一个旧群"))
    notes = _group_note_preview(base, old_group, limit=5)
    return {
        "action": "link_group", "valid": not conflicts and not already,
        "can_apply": not conflicts and not already, "already_linked": already,
        "conflicts": conflicts, "old_group": old_group, "new_group": new_group,
        "old_group_name": old_item["name"], "new_group_name": new_item["name"],
        "old_notes": notes,
        "group_linked": already,
        "member_links_kept": len(_member_links(
            conn, old_root_key(base), old_group=old_group, new_group=new_group)),
        "summary": ("已匹配" if already else "将把旧群 %s 关联到新群 %s" % (old_group, new_group)),
    }


def _preview_member_link(base, request, conn, old_groups, new_groups):
    old_group = request["old_group"]
    new_group = request["new_group"]
    old_user = request["old_user"]
    new_user = request["new_user"]
    conflicts = []
    if old_group not in old_groups:
        raise IdentityError(404, "old_group_not_found", "旧群不在旧数据目录中")
    if new_group not in new_groups:
        raise IdentityError(404, "new_group_not_found", "新群不在官方runtime记录或合法route中")
    group_link = _group_link(conn, old_root_key(base), old_group=old_group,
                             new_group=new_group)
    if not group_link:
        conflicts.append(_conflict("group_not_linked", "请先保存这组旧群→新群匹配"))
    old_members = {item["old_user"]: item for item in old_members_catalog(base, old_group)}
    old_known = old_members.get(old_user)
    candidates = {item["new_user"]: item for item in
                  new_members_catalog(base, new_group, conn=conn)}
    new_known = candidates.get(new_user)
    if new_known is None:
        # actors is only a supplemental observation catalogue.  It does not
        # prove that the selected new id belongs to the selected new group.
        conflicts.append(_conflict(
            "new_member_not_observed",
            "新成员ID必须在该新群的官方记录中出现；其他群或只存在于actors的ID不能代替本群成员证据"))
    existing = _member_link(conn, old_root_key(base), old_group=old_group, old_user=old_user)
    reverse = _member_link(conn, old_root_key(base), old_group=old_group, new_user=new_user)
    cross_old = _member_link(conn, old_root_key(base), old_user=old_user)
    cross_new = _member_link(conn, old_root_key(base), new_user=new_user)
    already = bool(existing and existing["active"] and existing["new_user"] == new_user
                   and existing["new_group"] == new_group)
    if existing and existing["active"] and (existing["new_user"] != new_user
                                            or existing["new_group"] != new_group):
        conflicts.append(_conflict("member_old_conflict", "该旧QQ在本群已匹配到另一个新成员"))
    if reverse and reverse["active"] and reverse["old_user"] != old_user:
        conflicts.append(_conflict("member_new_conflict", "该新成员在本群已匹配到另一个旧QQ"))
    if cross_old and cross_old["active"] and cross_old["new_user"] != new_user:
        conflicts.append(_conflict("cross_group_old_conflict",
                                  "同一旧QQ已在其他已匹配群对应另一个新成员，不能跨群改绑"))
    if cross_new and cross_new["active"] and cross_new["old_user"] != old_user:
        conflicts.append(_conflict("cross_group_new_conflict",
                                  "同一新成员已在其他已匹配群对应另一个旧QQ，不能跨群改绑"))
    notes = _group_note_preview(base, old_group, old_user=old_user, limit=5)
    if old_known is None:
        notes["manual_old_user"] = True
    return {
        "action": "link_member", "valid": not conflicts and not already,
        "can_apply": not conflicts and not already, "already_linked": already,
        "conflicts": conflicts, "old_group": old_group, "new_group": new_group,
        "old_user": old_user, "new_user": new_user,
        "old_nickname": (old_known or {}).get("old_nickname") or _bias_nickname(base, old_user),
        "new_nickname": (new_known or {}).get("new_nickname") or ("用户 " + new_user),
        "old_member_known": old_known is not None,
        "new_member_observed_in_group": new_known is not None,
        "group_linked": bool(group_link),
        "old_notes": notes,
        "summary": ("已匹配" if already else
                    ("将把旧QQ %s 关联到新成员 %s" % (old_user, new_user))),
    }


def _preview_unlink(base, request, conn, old_groups, new_groups):
    action = request["action"]
    old_group = request["old_group"]
    new_group = request["new_group"]
    if action == "unlink_group":
        link = _group_link(conn, old_root_key(base), old_group=old_group, new_group=new_group)
        conflicts = [] if link else [_conflict("group_link_not_found", "没有这条群匹配")]
        members = _member_links(conn, old_root_key(base), old_group=old_group,
                                new_group=new_group) if link else []
        return {
            "action": action, "valid": bool(link), "can_apply": bool(link),
            "already_linked": False, "conflicts": conflicts,
            "old_group": old_group, "new_group": new_group,
            "old_group_name": (old_groups.get(old_group) or {}).get("name", "旧群 " + old_group),
            "new_group_name": (new_groups.get(new_group) or {}).get("name", "新群 " + new_group),
            "member_links_to_invalidate": len(members),
            "member_links": [_public_member_link(item) for item in members],
            "warning": ("解除群匹配会让 %d 条成员匹配失效；重新绑定同一组可恢复" % len(members)
                        if members else "解除群匹配；当前没有成员匹配会失效"),
            "summary": "将解除旧群 %s 与新群 %s 的匹配" % (old_group, new_group),
        }
    old_user = request["old_user"]
    new_user = request["new_user"]
    link = _member_link(conn, old_root_key(base), old_group=old_group, old_user=old_user)
    conflict = []
    if not link or link["new_group"] != new_group or link["new_user"] != new_user:
        conflict.append(_conflict("member_link_not_found", "没有这条成员匹配"))
    return {
        "action": action, "valid": bool(link), "can_apply": bool(link),
        "already_linked": False, "conflicts": conflict,
        "old_group": old_group, "new_group": new_group,
        "old_user": old_user, "new_user": new_user,
        "old_group_name": (old_groups.get(old_group) or {}).get("name", "旧群 " + old_group),
        "new_group_name": (new_groups.get(new_group) or {}).get("name", "新群 " + new_group),
        "old_notes": _group_note_preview(base, old_group, old_user=old_user, limit=5),
        "summary": "将解除旧QQ %s 与新成员 %s 的匹配" % (old_user, new_user),
    }


def _preview_with_conn(base, request, conn):
    old_groups = {item["id"]: item for item in old_groups_catalog(base)}
    new_groups = {item["id"]: item for item in new_groups_catalog(base, conn=conn)}
    action = request["action"]
    if action == "link_group":
        payload = _preview_group_link(base, request, conn, old_groups, new_groups)
    elif action == "link_member":
        payload = _preview_member_link(base, request, conn, old_groups, new_groups)
    else:
        payload = _preview_unlink(base, request, conn, old_groups, new_groups)
    payload["revision"] = _revision_conn(conn)
    payload["current_revision"] = payload["revision"]
    payload["old_root"] = {"id": _old_root_id(base), "label": old_root_label(base)}
    return payload


def preview(base, data):
    """Read-only preview; it never creates the database or mapping tables."""
    request = _normalise_request(data, require_confirm=False, require_expected=False)
    with _open_read(_runtime_path(base)) as conn:
        return _preview_with_conn(base, request, conn)


def _audit(conn, revision_value, action, old_root, *, old_group="", old_user="",
           new_group="", new_user="", detail=None, now=None):
    conn.execute(
        "INSERT INTO identity_audit(revision,at,action,old_root,old_group,old_user,new_group,"
        "new_user,operator,operator_source,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(revision_value), float(time.time() if now is None else now), action,
         str(old_root), str(old_group), str(old_user), str(new_group), str(new_user),
         "local_operator", "memory_console",
         json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)))


def _member_conflict_message(conflicts):
    return conflicts[0]["message"] if conflicts else "匹配冲突"


def apply(base, data):
    """Apply one explicit local-operator mapping change under a serialised lock."""
    request = _normalise_request(data, require_confirm=True, require_expected=True)
    ensure_schema(base)
    old_root = old_root_key(base)
    now = time.time()
    with _open_write(base) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = _revision_conn(conn)
            expected = int(request["expected_revision"])
            if current != expected:
                raise IdentityError(409, "revision_conflict",
                                    "匹配列表已变化，请刷新后重试",
                                    current_revision=current, expected_revision=expected)
            state = _preview_with_conn(base, request, conn)
            if state.get("already_linked"):
                conn.rollback()
                return {"changed": False, "duplicate": True, "revision": current,
                        "action": request["action"], "preview": state}
            if not state.get("valid"):
                raise IdentityError(409, state["conflicts"][0]["code"],
                                    _member_conflict_message(state["conflicts"]),
                                    current_revision=current)
            action = request["action"]
            invalidated = 0
            restored = 0
            detail = {}
            if action == "link_group":
                old_group, new_group = request["old_group"], request["new_group"]
                previous = _group_link(conn, old_root, old_group=old_group, active_only=False)
                if previous and previous["new_group"] != new_group:
                    # A corrected group binding invalidates members under the old target.
                    cursor = conn.execute(
                        "UPDATE identity_member_links SET active=0,disabled_by='group',updated_at=? "
                        "WHERE old_root=? AND old_group=? AND new_group=? AND active=1",
                        (now, old_root, old_group, previous["new_group"]))
                    invalidated += cursor.rowcount
                conn.execute(
                    "INSERT INTO identity_group_links(old_root,old_group,new_group,active,"
                    "created_revision,created_at,updated_at) VALUES (?,?,?,1,?,?,?) "
                    "ON CONFLICT(old_root,old_group) DO UPDATE SET new_group=excluded.new_group,"
                    "active=1,updated_at=excluded.updated_at",
                    (old_root, old_group, new_group, current + 1, now, now))
                # Re-binding the same pair restores member links that were only
                # disabled by a previous group unlink.  Individually removed
                # members (disabled_by='self') stay removed.
                restore_rows = _member_links(conn, old_root, active_only=False,
                                             old_group=old_group, new_group=new_group)
                for item in restore_rows:
                    if item["active"] or item.get("disabled_by") != "group":
                        continue
                    other_old = _member_link(conn, old_root, old_user=item["old_user"])
                    other_new = _member_link(conn, old_root, new_user=item["new_user"])
                    if ((other_old and (other_old["new_user"] != item["new_user"]
                                        or other_old["new_group"] != new_group))
                            or (other_new and (other_new["old_user"] != item["old_user"]
                                               or other_new["new_group"] != new_group))):
                        continue
                    conn.execute(
                        "UPDATE identity_member_links SET active=1,disabled_by='',updated_at=? "
                        "WHERE old_root=? AND old_group=? AND old_user=?",
                        (now, old_root, old_group, item["old_user"]))
                    restored += 1
                detail = {"invalidated_members": invalidated, "restored_members": restored}
            elif action == "unlink_group":
                old_group, new_group = request["old_group"], request["new_group"]
                cursor = conn.execute(
                    "UPDATE identity_group_links SET active=0,updated_at=? "
                    "WHERE old_root=? AND old_group=? AND new_group=? AND active=1",
                    (now, old_root, old_group, new_group))
                if cursor.rowcount != 1:
                    raise IdentityError(409, "group_link_not_found", "没有这条群匹配")
                detached = conn.execute(
                    "UPDATE identity_member_links SET active=0,disabled_by='group',updated_at=? "
                    "WHERE old_root=? AND old_group=? AND new_group=? AND active=1",
                    (now, old_root, old_group, new_group))
                invalidated = detached.rowcount
                detail = {"invalidated_members": invalidated,
                          "recoverable_by_rebind": True}
            elif action == "link_member":
                old_group, new_group = request["old_group"], request["new_group"]
                old_user, new_user = request["old_user"], request["new_user"]
                conn.execute(
                    "INSERT INTO identity_member_links(old_root,old_group,old_user,new_group,"
                    "new_user,active,disabled_by,created_revision,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,1,'',?,?,?) ON CONFLICT(old_root,old_group,old_user) "
                    "DO UPDATE SET new_group=excluded.new_group,new_user=excluded.new_user,"
                    "active=1,disabled_by='',updated_at=excluded.updated_at",
                    (old_root, old_group, old_user, new_group, new_user,
                     current + 1, now, now))
                detail = {"old_member_known": bool(state.get("old_member_known")),
                          "new_member_observed_in_group": bool(
                              state.get("new_member_observed_in_group")),
                          "no_old_notes": bool(state.get("old_notes", {}).get("no_old_notes"))}
            else:  # unlink_member
                old_group, new_group = request["old_group"], request["new_group"]
                old_user, new_user = request["old_user"], request["new_user"]
                cursor = conn.execute(
                    "UPDATE identity_member_links SET active=0,disabled_by='self',updated_at=? "
                    "WHERE old_root=? AND old_group=? AND old_user=? AND new_group=? "
                    "AND new_user=? AND active=1",
                    (now, old_root, old_group, old_user, new_group, new_user))
                if cursor.rowcount != 1:
                    raise IdentityError(409, "member_link_not_found", "没有这条成员匹配")
                detail = {"recoverable_by_rebind": True}
            new_revision = current + 1
            conn.execute("UPDATE identity_revision SET revision=? WHERE id=1", (new_revision,))
            _audit(conn, new_revision, action, old_root,
                   old_group=request["old_group"], old_user=request.get("old_user", ""),
                   new_group=request["new_group"], new_user=request.get("new_user", ""),
                   detail=detail, now=now)
            conn.commit()
            return {
                "changed": True, "duplicate": False, "revision": new_revision,
                "action": action, "invalidated_members": int(invalidated),
                "restored_members": int(restored), "detail": detail,
                "old_group": request["old_group"], "new_group": request["new_group"],
                "old_user": request.get("old_user", ""),
                "new_user": request.get("new_user", ""),
            }
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
