"""Deterministic, read-only admission for an already requested sticker.

No probability, model call, accumulated idle credits, or state writes. Outbox
entries count once per reply, including queued/unsent replies. This is a snapshot
check, not a concurrent reservation; the caller owns reply submission ordering.
"""
from hakimi import paths
import datetime
import hashlib
import json
import math
import re
import time
from pathlib import Path
from hakimi.core.runtime_io import read_derived_cached


DEFAULT_BASE = paths.base()
DEFAULT_POLICY = {
    "max_json_bytes": 262144,
    "max_outbox_bytes": 262144,
    "max_outbox_records": 256,
    "max_record_bytes": 131072,
    "history_seconds": 7200,
    "local_replies": 12,
    "local_half_life_seconds": 3600,
    "recency_half_life_seconds": 1800,
    "global_half_life_seconds": 180,
    "global_window_seconds": 900,
    "global_min_gap_seconds": 120,
    "global_burst_window_seconds": 600,
    "global_burst_max_replies": 2,
    "consecutive_sticker_seconds": 3600,
    "max_plain_reply_credit": 6,
    "plain_reply_credit": 0.12,
    "max_quiet_credit": 0.50,
    "incomplete_history_pressure": 0.80,
    "long_explanation_chars": 480,
}
PROFILE_THRESHOLDS = {
    "playful_acg": 0.85,
    "gentle_daily": 0.72,
    "tech_banter": 0.64,
    "lore_club": 0.60,
    "quiet_default": 0.52,
    "study_calm": 0.45,
}
_SESSION = re.compile(r"(?:group|private):[1-9][0-9]{0,19}\Z")
_IMAGE = re.compile(r"\[CQ:image(?:,|\])", re.I)
_CQ = re.compile(r"\[CQ:[^\]]*\]", re.I)
_SERIOUS_HINTS = ("推导", "证明", "实验", "诊断", "药物", "法律", "合同", "安全",
                  "故障", "报错", "堆栈", "公式", "单位", "论文", "参考文献", "证据",
                  "去世", "难过", "焦虑", "抑郁", "失业", "分手")


def _finite_time(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except (OverflowError, ValueError):
        return False


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)


def _optional_object(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(DEFAULT_POLICY["max_json_bytes"] + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > DEFAULT_POLICY["max_json_bytes"]:
        raise ValueError("configuration too large")
    result = _json(raw)
    if not isinstance(result, dict):
        raise ValueError("configuration must be an object")
    return result


def _profile_and_access(base, session):
    config = _optional_object(base / "config.json")
    profiles = _optional_object(base / "group_profiles.json")
    options = config.get("sticker_policy", {})
    bot = config.get("bot_profile", {})
    groups = profiles.get("groups", {})
    if not all(isinstance(v, dict) for v in (options, bot, groups)):
        raise ValueError("invalid policy configuration")
    if options.get("enabled", True) is not True:
        return "quiet_default", "disabled"
    kind, target = session.split(":", 1)
    if kind == "private":
        if bot.get("allow_private_chat", True) is not True:
            return "gentle_daily", "session_not_allowed"
        profile = profiles.get("private_profile", "gentle_daily")
    else:
        active, ignored = bot.get("active_groups", []), bot.get("ignored_groups", [])
        if not isinstance(active, list) or not isinstance(ignored, list):
            raise ValueError("group lists must be arrays")
        if target in {str(g) for g in ignored} or (active and target not in {str(g) for g in active}):
            return "quiet_default", "session_not_allowed"
        group = groups.get(target, {})
        if not isinstance(group, dict):
            raise ValueError("group profile must be an object")
        profile = group.get("profile", profiles.get("default_profile", "quiet_default"))
    if not isinstance(profile, str):
        raise ValueError("profile name must be text")
    return profile if profile in PROFILE_THRESHOLDS else "quiet_default", None


def _timestamp(record, now):
    stamp = record.get("created_at")
    if stamp is None:
        legacy = record.get("timestamp")
        if not isinstance(legacy, str) or len(legacy) > 40:
            raise ValueError("reply timestamp missing")
        stamp = datetime.datetime.fromisoformat(legacy).timestamp()
    if not _finite_time(stamp) or stamp > now + 5:
        raise ValueError("invalid reply time or clock rollback")
    return min(stamp, now)


def _parsed_history(path):
    """Read a bounded EOF snapshot, dropping only a known partial first line."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - DEFAULT_POLICY["max_outbox_bytes"])
            starts_at_boundary = True
            if start:
                stream.seek(start - 1)
                starts_at_boundary = stream.read(1) == b"\n"
            stream.seek(start)
            raw = stream.read(size - start)
    except FileNotFoundError:
        return [], False
    if not raw:
        return [], False
    if not raw.endswith(b"\n"):
        raise ValueError("outbox has an unfinished reply")
    if not starts_at_boundary:
        boundary = raw.find(b"\n")
        if boundary < 0:
            raise ValueError("no complete reply in bounded tail")
        raw = raw[boundary + 1:]
    lines = raw.splitlines()
    clipped = start > 0 or len(lines) > DEFAULT_POLICY["max_outbox_records"]
    lines = lines[-DEFAULT_POLICY["max_outbox_records"]:]
    records = []
    for line in lines:
        if not line or len(line) > DEFAULT_POLICY["max_record_bytes"]:
            raise ValueError("invalid reply record size")
        record = _json(line)
        if not isinstance(record, dict):
            raise ValueError("outbox reply must be an object")
        fallback_identity = "legacy:" + hashlib.sha256(line).hexdigest() if record.get("reply_id") is None else None
        records.append((record, fallback_identity))
    return records, clipped


def _history(path, now):
    records, clipped = read_derived_cached(path, _parsed_history, ("stickers",
        DEFAULT_POLICY["max_outbox_bytes"], DEFAULT_POLICY["max_outbox_records"], DEFAULT_POLICY["max_record_bytes"], _json))
    replies, seen = [], {}
    for record, fallback_identity in records:
        session = record.get("session_key")
        if not isinstance(session, str) or not _SESSION.fullmatch(session):
            raise ValueError("invalid outbox session")
        segments = record.get("segments")
        if segments is None and isinstance(record.get("text"), str):
            segments = [record["text"]]
        if (not isinstance(segments, list) or not 1 <= len(segments) <= 16
                or any(not isinstance(part, str) for part in segments)):
            raise ValueError("invalid reply segments")
        at = _timestamp(record, now)
        identity = record.get("reply_id")
        if identity is None:
            identity = fallback_identity
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise ValueError("invalid reply ID")
        if identity in seen:
            if record != seen[identity]:
                raise ValueError("conflicting duplicate reply ID")
            continue
        seen[identity] = record
        replies.append({"session": session, "at": at,
                        "sticker": any(_IMAGE.search(part) for part in segments)})
    replies.sort(key=lambda item: item["at"])
    return replies, clipped


def _decay(age, half_life):
    return 2.0 ** (-max(0, age) / half_life)


def evaluate_sticker(session_key, text="", base=DEFAULT_BASE, now=None):
    """Return permission/reason for a requested sticker; never request one itself.

    Empty ``text`` is suitable for prompt hints. The CLI must check again using
    its actual reply text. Absent files mean fresh defaults; damaged files deny.
    """
    now = time.time() if now is None else now
    if (not isinstance(session_key, str) or not _SESSION.fullmatch(session_key)
            or not isinstance(text, str) or len(text) > 20000 or not _finite_time(now)):
        return {"allowed": False, "reason": "invalid_input"}
    try:
        base = Path(base)
        profile, denied = _profile_and_access(base, session_key)
        if denied:
            return {"allowed": False, "reason": denied, "profile": profile}
        replies, clipped = _history(base / "state" / "outbox.jsonl", now)
        local = [r for r in replies if r["session"] == session_key
                 and now - r["at"] <= DEFAULT_POLICY["history_seconds"]][-DEFAULT_POLICY["local_replies"]:]
        if clipped and (not replies or now - replies[0]["at"] < DEFAULT_POLICY["global_window_seconds"]):
            return {"allowed": False, "reason": "history_window_incomplete", "profile": profile}
        # A busy other group can push old local replies out of the bounded tail.
        # Treat that as a decaying uncertainty cost, not permanent exclusion.
        incomplete_local = (clipped and len(local) < DEFAULT_POLICY["local_replies"]
                            and now - replies[0]["at"] < DEFAULT_POLICY["history_seconds"])
        uncertainty_pressure = (DEFAULT_POLICY["incomplete_history_pressure"]
                                * _decay(now - replies[0]["at"], DEFAULT_POLICY["recency_half_life_seconds"])
                                if incomplete_local else 0.0)
        images = [r for r in replies if r["sticker"]]
        local_images = [r for r in local if r["sticker"]]
        info = {"profile": profile, "recent_replies": len(local),
                "recent_sticker_replies": len(local_images)}
        if (local and local[-1]["sticker"]
                and now - local[-1]["at"] < DEFAULT_POLICY["consecutive_sticker_seconds"]):
            return {"allowed": False, "reason": "consecutive_sticker", **info}
        if images and now - images[-1]["at"] < DEFAULT_POLICY["global_min_gap_seconds"]:
            return {"allowed": False, "reason": "global_sticker_gap", **info}
        if sum(now - r["at"] < DEFAULT_POLICY["global_burst_window_seconds"] for r in images) >= DEFAULT_POLICY["global_burst_max_replies"]:
            return {"allowed": False, "reason": "global_sticker_burst", **info}

        plain_rounds = 0
        for reply in reversed(local):
            if reply["sticker"]:
                break
            plain_rounds += 1
        weighted_total, weighted_images = 0.0, 0.0
        for rank, reply in enumerate(reversed(local)):
            weight = (0.86 ** rank) * _decay(now - reply["at"], DEFAULT_POLICY["local_half_life_seconds"])
            weighted_total += weight
            weighted_images += weight * reply["sticker"]
        share = weighted_images / (weighted_total + 2.0)
        last_age = now - local_images[-1]["at"] if local_images else None
        recency = _decay(last_age, DEFAULT_POLICY["recency_half_life_seconds"]) if last_age is not None else 0.0
        cross_pressure = sum(_decay(now - r["at"], DEFAULT_POLICY["global_half_life_seconds"])
                             for r in images if r["session"] != session_key
                             and now - r["at"] <= DEFAULT_POLICY["global_window_seconds"])
        visible = _CQ.sub("", text).strip()
        if len(visible) >= DEFAULT_POLICY["long_explanation_chars"]:
            return {"allowed": False, "reason": "long_explanation", **info}
        content_pressure = min(0.8, max(0, len(visible) - 100) / 450)
        content_pressure += min(0.5, 0.14 * sum(hint in visible for hint in _SERIOUS_HINTS))
        pressure = 1.5 * share + recency + 0.5 * cross_pressure + content_pressure + uncertainty_pressure
        # Quiet time is recomputed from the last sticker, never banked. The next
        # submitted sticker immediately resets it, including when still queued.
        quiet_credit = (DEFAULT_POLICY["max_quiet_credit"] * (1 - _decay(last_age, 3600))
                        if last_age is not None else 0.0)
        threshold = (PROFILE_THRESHOLDS[profile] + quiet_credit
                     + DEFAULT_POLICY["plain_reply_credit"] * min(plain_rounds, DEFAULT_POLICY["max_plain_reply_credit"]))
        return {"allowed": pressure <= threshold,
                "reason": "budget_available" if pressure <= threshold else "sticker_pressure",
                **info, "plain_replies_since_sticker": plain_rounds,
                "pressure": round(pressure, 3), "threshold": round(threshold, 3)}
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return {"allowed": False, "reason": "history_unavailable"}
