"""Read-only quote gate based on recent, same-group daemon receipts.

Old HH:MM:SS-only records never establish activity. A model's claim that a
group is busy is not an input. Missing, redirected, or damaged inputs deny.
"""
from hakimi.core import bot_identity
from hakimi import paths
import json
import math
import re
import time
from pathlib import Path
from hakimi.core.runtime_io import read_derived_cached


BASE_DIR = paths.base()
MAX_HISTORY_BYTES = 256 * 1024
MAX_HISTORY_RECORDS = 512
MAX_RECORD_BYTES = 64 * 1024
MAX_CONFIG_BYTES = 128 * 1024
DEFAULT_POLICY = {
    "short_window_seconds": 15,
    "short_min_messages": 3,
    "long_window_seconds": 60,
    "long_min_messages": 6,
    "min_unique_users": 2,
    "latest_human_age_seconds": 60,
    "max_target_age_seconds": 180,
}
_BOUNDS = {
    "short_window_seconds": (1, 30),
    "short_min_messages": (3, 100),
    "long_window_seconds": (10, 120),
    "long_min_messages": (6, 200),
    "min_unique_users": (2, 100),
    "latest_human_age_seconds": (1, 60),
    "max_target_age_seconds": (10, 600),
}
KNOWN_BOT_IDS = bot_identity.KNOWN_BOT_IDS
_GROUP = re.compile(r"group:[1-9][0-9]{0,19}\Z")
_USER = re.compile(r"[1-9][0-9]{0,19}\Z")
_MESSAGE = re.compile(r"-?[0-9]{1,20}\Z")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (ValueError, OverflowError):
        return False


def _fixed_path(base, *parts):
    path = base.joinpath(*parts)
    if path.resolve() != path.absolute() or path.is_symlink():
        raise ValueError("redirected local input")
    return path


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object)


def _settings(base):
    path = _fixed_path(base, "config.json")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        config = {}
    else:
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("configuration too large")
        config = _json(raw)
    if not isinstance(config, dict):
        raise ValueError("invalid configuration")
    options, bot = config.get("quote_policy", {}), config.get("bot_profile", {})
    if not isinstance(options, dict) or not isinstance(bot, dict):
        raise ValueError("invalid quote policy")
    if options.get("enabled", True) is not True:
        raise ValueError("quotes disabled")
    policy = dict(DEFAULT_POLICY)
    for key, (low, high) in _BOUNDS.items():
        value = options.get(key, policy[key])
        if type(value) is not int or not low <= value <= high:
            raise ValueError("invalid quote threshold")
        policy[key] = value
    if policy["short_window_seconds"] > policy["long_window_seconds"]:
        raise ValueError("quote windows out of order")
    ignored = bot.get("ignored_user_ids", [])
    if not isinstance(ignored, list) or len(ignored) > 1024:
        raise ValueError("invalid ignored-user list")
    excluded = set(KNOWN_BOT_IDS)
    excluded.update(str(uid) for uid in ignored)
    excluded.add(str(bot.get("self_qq", str(bot_identity.SELF_QQ))))
    return policy, excluded


def _parsed_tail(path):
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - MAX_HISTORY_BYTES)
        boundary = True
        if start:
            handle.seek(start - 1)
            boundary = handle.read(1) == b"\n"
        handle.seek(start)
        raw = handle.read(size - start)
    if not boundary:
        raw = raw.partition(b"\n")[2]
    # An in-progress append is not evidence. Keep complete preceding records.
    if raw and not raw.endswith(b"\n"):
        raw = raw.rpartition(b"\n")[0]
    records = []
    for line in raw.splitlines()[-MAX_HISTORY_RECORDS:]:
        if not 0 < len(line) <= MAX_RECORD_BYTES:
            continue
        try:
            record = _json(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _tail(base):
    path = _fixed_path(base, "state", "chat_history.jsonl")
    return iter(read_derived_cached(path, _parsed_tail,
        ("quotes", MAX_HISTORY_BYTES, MAX_HISTORY_RECORDS, MAX_RECORD_BYTES, _json)))


def _message_id(value):
    if type(value) not in (str, int):
        return None
    try:
        value = str(value)
        return str(int(value)) if _MESSAGE.fullmatch(value) else None
    except ValueError:
        return None


def quote_candidates(session_key, visible_ids, base=BASE_DIR, now=None):
    """Return up to eight eligible IDs from the caller's visible message IDs.

    Read history once. Activity windows end at the latest human receipt, giving
    the model a bounded generation grace period after a busy conversation.
    These are options for disambiguation, never an automatically chosen target.
    """
    now = time.time() if now is None else now
    if (not isinstance(session_key, str) or not _GROUP.fullmatch(session_key)
            or not isinstance(visible_ids, (list, tuple)) or len(visible_ids) > 64
            or not _finite(now)):
        return []
    requested = list(dict.fromkeys(mid for value in visible_ids
                                   if (mid := _message_id(value)) is not None))
    if not requested:
        return []
    try:
        base = Path(base).resolve()
        policy, excluded = _settings(base)
        humans, conflicts = {}, set()
        for record in _tail(base):
            if record.get("session_key") != session_key:
                continue
            uid = str(record.get("user_id", ""))
            mid, at = _message_id(record.get("message_id")), record.get("received_at")
            if (not _USER.fullmatch(uid) or uid in excluded or record.get("is_bot") is True
                    or mid is None or not _finite(at) or at > now):
                continue
            age = now - at
            if age > max(policy["long_window_seconds"] + policy["latest_human_age_seconds"],
                         policy["max_target_age_seconds"]):
                continue
            if mid in humans:
                previous_uid, previous_at = humans[mid]
                if previous_uid != uid:
                    conflicts.add(mid)
                else:
                    humans[mid] = (uid, min(at, previous_at))
            else:
                humans[mid] = (uid, at)
        for mid in conflicts:
            humans.pop(mid, None)
        if not humans:
            return []
        latest = max(at for _, at in humans.values())
        if now - latest > policy["latest_human_age_seconds"]:
            return []
        busy = False
        for window, minimum in (("short_window_seconds", "short_min_messages"),
                                ("long_window_seconds", "long_min_messages")):
            active = [uid for uid, at in humans.values() if latest - at <= policy[window]]
            if len(active) >= policy[minimum] and len(set(active)) >= policy["min_unique_users"]:
                busy = True
                break
        if busy:
            return [mid for mid in requested if mid in humans
                    and now - humans[mid][1] <= policy["max_target_age_seconds"]][-8:]
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return []
    return []


def allow_quote(session_key, reply_to, base=BASE_DIR, now=None):
    """Recheck one requested target immediately before adding CQ:reply."""
    return bool(quote_candidates(session_key, [reply_to], base=base, now=now))
