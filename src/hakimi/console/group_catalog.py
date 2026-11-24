"""Read-only group labels for every operator panel; never returns route credentials."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

GROUP = re.compile(r"[1-9][0-9]{0,19}\Z")


def _json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _name(value):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())[:120]
    # Numeric local IDs and generated labels are not observed group names.
    if value.isdecimal() or re.fullmatch(r"(?:新群|旧群|群)(?:\s*ID)?\s*[:：]?\s*[0-9]+", value):
        return ""
    return value


def _configured(root):
    result = {}
    for filename, key in (("config.json", "group_weights"), ("group_profiles.json", "groups")):
        values = _json(root / filename).get(key, {})
        if isinstance(values, dict):
            for gid, item in values.items():
                if GROUP.fullmatch(str(gid)) and isinstance(item, dict):
                    name = _name(item.get("name"))
                    if name:
                        result.setdefault(str(gid), (name, filename))
    review = _json(root / "official_memory_review.json")
    blocks = dict(review.get("group_reviews", {})) if isinstance(review.get("group_reviews"), dict) else {}
    if GROUP.fullmatch(str(review.get("source_group", ""))):
        blocks[str(review["source_group"])] = review
    for gid, item in blocks.items():
        if GROUP.fullmatch(str(gid)) and isinstance(item, dict) and _name(item.get("name")):
            result.setdefault(str(gid), (_name(item["name"]), "official_memory_review"))
    try:
        from hakimi.memory.legacy_md_notes import groups
        for item in groups(root):
            gid = str(item.get("group_id", ""))
            if GROUP.fullmatch(gid) and _name(item.get("name")):
                result.setdefault(gid, (_name(item["name"]), "memory_md"))
    except (ImportError, OSError, ValueError):
        pass
    return result


def group_metadata(base, sessions):
    """Return group-only metadata. Sessions remain internal local filter keys.

    Resolve names only through a local-ID match or an active explicit legacy
    mapping. Similar names/style_source must never imply group identity.
    """
    valid = sorted({str(s) for s in sessions if re.fullmatch(r"group:[1-9][0-9]{0,19}", str(s))})
    if not valid:
        return {}
    base = Path(base).resolve()
    root = base.parent if base.name == "official_test_runtime" else base
    names = _configured(base)
    old_names = names if root == base else _configured(root)
    links = {}
    route_path = base / "state/official_routes.sqlite3"
    if route_path.is_file():
        try:
            with closing(sqlite3.connect(route_path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                row = db.execute("SELECT value FROM settings WHERE key='group_routes'").fetchone()
                routes = json.loads(row[0]) if row else {}
            if isinstance(routes, dict):
                for item in routes.values():
                    if not isinstance(item, dict):
                        continue
                    gid = str(item.get("local_id") or "")
                    if not GROUP.fullmatch(gid):
                        continue
                    name = _name(item.get("name"))
                    if name:
                        names[gid] = (name, "official_route")
                    old = str(item.get("legacy_group") or "")
                    if GROUP.fullmatch(old):
                        links[gid] = old
        except (sqlite3.Error, ValueError, TypeError):
            pass
    memory_path = base / "state/memory.sqlite3"
    if memory_path.is_file():
        try:
            from hakimi.social.identity_base import old_root_key
            with closing(sqlite3.connect(memory_path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                for old, new in db.execute(
                    "SELECT old_group,new_group FROM identity_group_links WHERE active=1 AND old_root=?",
                    (old_root_key(base),),
                ):
                    if GROUP.fullmatch(str(old)) and GROUP.fullmatch(str(new)):
                        links[str(new)] = str(old)
        except (sqlite3.Error, ImportError):
            pass
    for gid, old in links.items():
        if gid not in names and old in old_names:
            names[gid] = (old_names[old][0], "legacy_mapping")
    counts = Counter(name for name, source in names.values() if name)
    result = {}
    for session in valid:
        gid = session.split(":", 1)[1]
        name, source = names.get(gid, ("", "unknown"))
        # A stable non-ID token distinguishes unknown and identically named groups.
        token = hashlib.sha256(session.encode()).hexdigest()[:8].upper()
        label = name if name and counts[name] == 1 else ((name or "未命名群") + " · " + token)
        result[session] = {"name": name, "name_source": source, "label": label,
                           "title": label, "kind": "group", "display_token": token}
    return result


def decorate_groups(base, rows):
    """Preserve statistics, filter private/invalid sessions, add shared labels."""
    rows = [dict(row) for row in rows]
    metadata = group_metadata(base, [row.get("session", "") for row in rows])
    return [{**row, **metadata[row["session"]]} for row in rows if row.get("session") in metadata]



def identity_display(base, payload):
    """Console-only projection; leaves underlying keys and model data untouched."""
    def source_root(value):
        value = Path(value).resolve()
        return value.parent if value.name == "official_test_runtime" else value
    old_ids, new_ids = set(), set()

    def collect(value):
        if isinstance(value, dict):
            for key in ("old_group",):
                if GROUP.fullmatch(str(value.get(key, ""))):
                    old_ids.add("group:" + str(value[key]))
            if GROUP.fullmatch(str(value.get("new_group", ""))):
                new_ids.add("group:" + str(value["new_group"]))
            for key, items in value.items():
                if key in ("old_groups", "new_groups") and isinstance(items, list):
                    target = old_ids if key == "old_groups" else new_ids
                    target.update("group:" + str(item["id"]) for item in items
                                  if isinstance(item, dict) and GROUP.fullmatch(str(item.get("id", ""))))
                collect(items)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    if source_root(base) == Path(base).resolve():
        combined = group_metadata(base, old_ids | new_ids)
        old = {key: value for key, value in combined.items() if key in old_ids}
        new = {key: value for key, value in combined.items() if key in new_ids}
    else:
        old = group_metadata(source_root(base), old_ids)
        new = group_metadata(base, new_ids)

    def project(value):
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: project(item) for key, item in value.items()}
        for key, metadata in (("old_groups", old), ("new_groups", new)):
            if isinstance(result.get(key), list):
                for item in result[key]:
                    info = metadata.get("group:" + str(item.get("id", "")))
                    if info:
                        item.update(info, real_name=info["name"], name=info["label"])
        labels = {}
        for side, metadata in (("old", old), ("new", new)):
            info = metadata.get("group:" + str(result.get(side + "_group", "")))
            if info:
                labels[side] = info["label"]
                result[side + "_group_name"] = info["label"]
                result[side + "_name"] = info["label"]
        if labels and ("identity_label" in result or "label" in result):
            text = "旧群 " + labels["old"] if "old" in labels else ""
            if "new" in labels:
                text += (" → " if text else "") + "新群 " + labels["new"]
            elif "old" in labels:
                text += "（尚未确认匹配）"
            if "identity_label" in result:
                result["identity_label"] = text
            if "label" in result:
                result["label"] = text
        return result

    return project(payload)
