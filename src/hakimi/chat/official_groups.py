"""One-time adaptation of existing QQ rules/assets to official group sessions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

from hakimi.chat.official_base import fixed_json, OfficialError


def read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(default)


def rows(path):
    if not Path(path).exists():
        return []
    result = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                result.append(row)
        except ValueError:
            continue
    return result


def source_root(base):
    base = Path(base)
    return base.parent if base.name == "official_test_runtime" else base


def known_groups(base):
    root = source_root(base)
    return (set(read(root/"config.json", {}).get("group_weights", {}))
            | set(read(root/"group_profiles.json", {}).get("groups", {})))


def infer_profile(name, theme=""):
    text = f"{name or ''} {theme or ''}".strip()
    if re.search(r"学习|数学|物理|化学|科研|学宫|作业", text):
        return "study_calm"
    if re.search(r"原神|剧情|考据", text):
        return "lore_club"
    if re.search(r"代码|编程|技术|开发|AI|算法", text, re.I):
        return "tech_banter"
    if re.search(r"二次元|动漫|ACG|枫丹|提瓦特|璃月|稻妻|须弥|纳塔|至冬|挪德卡莱|摸鱼|涩图|整活", text, re.I):
        return "playful_acg"
    return "quiet_default"


def style_template(base, name):
    """A unique matching name can suggest tone, never group/member identity."""
    matches = [gid for gid, info in read(source_root(base)/"config.json", {}).get("group_weights", {}).items()
               if name and str(info.get("name", "")).strip() == name.strip()]
    return matches[0] if len(matches) == 1 else None


def reviewed_notes(root, old):
    review = read(root/"official_memory_review.json", {})
    if review.get("source_group") != old:
        review = review.get("group_reviews", {}).get(old, {})
    snapshot = root/review.get("source_snapshot", "state/memory_migration_sources/missing")
    if (review.get("source_group") != old or not snapshot.is_file()
            or hashlib.sha256(snapshot.read_bytes()).hexdigest() != review.get("source_block_sha256")):
        return []
    return [{**note, "kind": "preference", "created_at": snapshot.stat().st_mtime,
             "source": "reviewed_legacy_group_memory",
             "migration_key": "legacy:"+review["source_block_sha256"]+":"+str(i)}
            for i, note in enumerate(review.get("group_notes", [])) if note.get("session_key") == "group:"+old]


def adapt_group(base, route):
    """Change only the official snapshot; never touch the old transport/state."""
    base = Path(base)
    root = source_root(base)
    local, old = str(route["local_id"]), str(route.get("legacy_group") or "")
    template = old or route.get("style_source") or ""
    original = read(root/"config.json", {})
    originals = read(root/"group_profiles.json", {})
    original_group = originals.get("groups", {}).get(template, {})
    group = deepcopy(original_group)
    if not old:
        group = {k: v for k, v in group.items() if k == "profile"}
    if not group:
        group = {"profile": infer_profile(route.get("name", ""), route.get("theme", "")), "basis": "官方群资料的初始适配"}
    if route.get("profile"):
        group["profile"] = route["profile"]
    config = read(base/"config.json", {})
    if not config:  # Adapter-only fixtures need no bridge configuration.
        return {"notes": 0, "style_samples": 0}
    profile = config.setdefault("bot_profile", {})
    for key in ("active_groups", "random_peek_groups", "low_frequency_groups"):
        values = [str(v) for v in profile.get(key, []) if str(v) != local]
        enabled = route.get("active", True) and (key != "low_frequency_groups"
                  or not template or template in map(str, original.get("bot_profile", {}).get(key, [])))
        profile[key] = values + ([local] if enabled else [])
    for key in ("study_groups", "study_attention_groups"):
        policy = config.setdefault("participation_policy", {})
        values = [str(v) for v in policy.get(key, []) if str(v) != local]
        is_study = group.get("profile") == "study_calm" or template in map(str, original.get("participation_policy", {}).get(key, []))
        policy[key] = values + ([local] if is_study and route.get("active", True) else [])
    if not route.get("active", True):
        fixed_json(base/"config.json", config)
        return {"notes": 0, "style_samples": 0}
    weight = deepcopy(original.get("group_weights", {}).get(template, {}))
    weight.setdefault("name", route.get("name") or "新加入的群")
    weight.setdefault("weight", 1)
    weight.setdefault("min_score", .5)
    config.setdefault("group_weights", {})[local] = weight
    sensitivity = original.get("conversation_scheduler", {}).get("busy_group_sensitivity", {})
    config.setdefault("conversation_scheduler", {}).setdefault("busy_group_sensitivity", {})[local] = sensitivity.get(template, 1)
    fixed_json(base/"config.json", config)
    profiles = read(base/"group_profiles.json", originals)
    profiles.setdefault("groups", {})[local] = group
    fixed_json(base/"group_profiles.json", profiles)
    counts = {"notes": 0, "style_samples": 0}
    if old not in known_groups(base):
        return counts
    # Import only records explicitly belonging to this source group. Original
    # dates and provenance survive; old QQ numbers are not new member OpenIDs.
    session, old_session = "group:"+local, "group:"+old
    for filename, kind in (("state/session_memory.jsonl", "notes"),
                           ("dsh_style_examples.jsonl", "style_samples")):
        target = base/filename
        existing = rows(target)
        keys = {r.get("official_migration_key") for r in existing}
        old_keys = {r.get("migration_key") for r in existing if r.get("migration_key") and r.get("session_key") == session}
        records = rows(root/filename) + (reviewed_notes(root, old) if kind == "notes" else [])
        for row in records:
            if row.get("session_key") != old_session:
                continue
            if kind == "notes" and row.get("kind", "preference") not in {"preference", "lore"}:
                continue
            key = local+":"+hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            copied = {**row, "session_key": session, "legacy_session_key": old_session,
                      "official_migration_key": key}
            if kind == "notes" and local != old:
                copied["note"] = "[旧群笔记；原QQ号尚未对应当前成员] " + str(row.get("note", ""))
            if kind == "style_samples" and local != old:
                copied["id"] = str(row.get("id", "sample"))+":official:"+local
            if key in keys or row.get("migration_key") in old_keys or any(all(r.get(k) == copied.get(k) for k in
                    ("session_key", "kind", "note", "created_at", "id", "text_segments")) for r in existing):
                continue
            existing.append(copied)
            keys.add(key)
            counts[kind] += 1
        if counts[kind]:
            # Append, so model-written memory in this runtime is never replaced.
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as output:
                for row in existing[-counts[kind]:]:
                    output.write(json.dumps(row, ensure_ascii=False)+"\n")
    return counts


def associate_group(base, route, old):
    if old not in known_groups(base):
        raise OfficialError("UNKNOWN_LEGACY_GROUP")
    if route.get("legacy_group") not in (None, "", old):
        raise OfficialError("GROUP_ALREADY_ASSOCIATED")
    route["legacy_group"] = old
    result = adapt_group(base, route)
    return {"legacy_group": old, **result, "at": time.time()}
