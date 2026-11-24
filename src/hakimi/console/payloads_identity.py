"""Console API: identity link catalogue, preview/apply and linked legacy memory."""
from __future__ import annotations

import re

from hakimi.console.console_db import ApiError
from hakimi.social import identity_links
from hakimi.console.group_catalog import identity_display


def identity_catalog_payload(base):
    return identity_display(base, identity_links.catalog(base))


def identity_members_payload(base, values):
    side = str(values.get("side", "") or "").strip()
    group = str(values.get("group", "") or "").strip()
    query = str(values.get("q", "") or "").strip()[:120]
    if side == "old":
        return {"side": "old", "group": group,
                "members": identity_links.old_members_catalog(base, group, query)}
    if side == "new":
        return {"side": "new", "group": group,
                "members": identity_links.new_members_catalog(base, group, query)}
    raise ApiError(400, "invalid_side", "side必须是old或new")


def identity_links_payload(base):
    return identity_display(base, identity_links.links_payload(base))


def identity_preview_payload(base, data):
    try:
        return identity_display(base, identity_links.preview(base, data))
    except identity_links.IdentityError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc


def identity_apply_payload(base, data):
    try:
        return identity_display(base, identity_links.apply(base, data))
    except identity_links.IdentityError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc


def identity_memory_payload(base, values):
    raw = str(values.get("id", "") or "").strip()
    if not re.fullmatch(r"(?:L[1-9][0-9]{0,19}|LM[0-9a-f]{24}|R[1-9][0-9]{0,19}-[gm][0-9-]+)",
                        raw, re.IGNORECASE):
        raise ApiError(400, "invalid_legacy_id", "旧记忆编号格式无效")
    try:
        offset = max(0, int(values.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = max(100, min(int(values.get("limit", 2000)), 6000))
    except (TypeError, ValueError):
        limit = 2000
    # Admin/operator view: known old public source ids are readable before a
    # mapping exists (so the operator can verify what they are about to link).
    # The model-side MemoryStore.read still requires the active mapping.
    record = identity_links.read_for_operator(
        base, raw, offset=offset, limit=limit)
    if record is None:
        raise ApiError(404, "memory_not_found", "旧记忆不存在")
    return identity_display(base, record)
