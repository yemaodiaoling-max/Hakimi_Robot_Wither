"""Resolve a session's group style profile and per-group reply limits."""
import re
import sqlite3
from pathlib import Path

from hakimi import paths
from hakimi.core.jsonio import read_json


BASE_DIR = paths.base()
SESSION_RE = re.compile(r"(?:group|private):[1-9][0-9]{0,19}\Z")
_INITIAL_BASES = (None, "", "官方群资料的初始适配")


def _has_confident_social_row(base: Path, session_key: str) -> bool:
    db_path = base / "state/memory.sqlite3"
    if not db_path.exists():
        return False
    db = None
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1.0)
        row = db.execute(
            "SELECT 1 FROM social_assessments "
            "WHERE session=? AND subject_id='hakimi' AND state='active' AND confidence>=60 LIMIT 1",
            (session_key,),
        ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error, ValueError):
        return False
    finally:
        if db is not None:
            db.close()


def _dynamic_group_profile(base: Path, session_key: str, target: str, group: dict, config: dict) -> str:
    policy_study = config.get("participation_policy", {}).get("study_groups")
    study_ids = {str(v) for v in (policy_study or config.get("study_groups") or [])}
    if target in study_ids:
        return "study_calm"
    gw = config.get("group_weights", {}).get(target, {})
    group_name = str(group.get("name") or gw.get("name") or "")
    group_theme = str(group.get("theme") or gw.get("theme") or "")
    from hakimi.transport.official_groups import infer_profile
    inferred = infer_profile(group_name, group_theme)
    if inferred != "quiet_default":
        return inferred
    replies = read_json(base / "state/interaction_policy.json", {}).get("groups", {}).get(session_key, [])
    if isinstance(replies, list) and len(replies) >= 2:
        return "playful_acg"
    if _has_confident_social_row(base, session_key):
        return "playful_acg"
    return "quiet_default"


def resolve_profile(session_key, base=BASE_DIR):
    if not SESSION_RE.fullmatch(session_key):
        raise ValueError("invalid session_key")
    base = Path(base)
    cfg = read_json(base / "group_profiles.json", {})
    kind, target = session_key.split(":", 1)
    groups = cfg.get("groups", {})
    group = groups.get(target, {}) if kind == "group" else {}
    default_name = cfg.get("private_profile" if kind == "private" else "default_profile", "quiet_default")
    name = group.get("profile", default_name)
    from hakimi.core.runtime_io import read_json_cached
    from hakimi.bridge.reply_format import group_reply_rules
    config = read_json_cached(base / "config.json", {})
    profiles = cfg.get("profiles", {})
    if kind == "group" and (
        (target not in groups and name == "quiet_default")
        or (name == "quiet_default" and group.get("basis") in _INITIAL_BASES)
    ):
        dynamic_name = _dynamic_group_profile(base, session_key, target, group, config)
        if dynamic_name in profiles:
            name = dynamic_name
    profile = profiles.get(name, profiles.get(default_name, profiles.get("quiet_default", {})))
    rules = group_reply_rules(session_key, config)
    limits = []
    if "max_chars" in rules:
        limits.append(f"合计不超过{rules['max_chars']}字")
    if "max_messages" in rules:
        limits.append(f"最多{rules['max_messages']}条消息（图片、附件也计条数）")
    if limits:
        profile = dict(profile, length="本群每轮" + "，".join(limits) + "。被@或专业讨论也遵守；保持原来的口吻，优先简短完整地回答。")
    return {"name": name, **profile}, group.get("memory_notes", [])
