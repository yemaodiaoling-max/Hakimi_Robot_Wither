"""Low-frequency opportunities to join recent human group conversation.

A reservation counts an *attempt*, even if the model later stays silent. This
module neither generates topics nor sends messages. Callers must pass the time
of the latest genuine human message, excluding bot/system/self events.
"""
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path


DAY_SECONDS = 86400
MAX_STATE_BYTES = 65536
MAX_STATE_ATTEMPTS = 256
MAX_OUTBOX_BYTES = 8 * 1024 * 1024
MAX_OUTBOX_RECORD_BYTES = 131072
MAX_OUTBOX_RECORDS = 20000
DEFAULT_POLICY = {
    "enabled": True,
    "min_group_interval_seconds": 7200,
    "max_group_attempts_24h": 3,
    "max_global_attempts_24h": 6,
    "max_human_age_seconds": 600,
    "study_min_group_interval_seconds": 1800,
    "study_min_reply_interval_seconds": 7200,
}
_GROUP = re.compile(r"group:[1-9][0-9]{0,19}\Z")
_SESSION = re.compile(r"(?:group|private):[1-9][0-9]{0,19}\Z")


def _number(value):
    try:
        return (isinstance(value, (float, int)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except (OverflowError, ValueError):
        return False


def _policy_and_context(session, last_human_at, now, cfg):
    if not isinstance(session, str) or not _GROUP.fullmatch(session):
        return None
    if not _number(now) or not _number(last_human_at) or last_human_at > now:
        return None
    if not isinstance(cfg, dict):
        return None
    supplied = cfg.get("participation_policy", {})
    profile = cfg.get("bot_profile", {})
    if not isinstance(supplied, dict) or not isinstance(profile, dict):
        return None
    policy = {**DEFAULT_POLICY, **supplied}
    if policy.get("enabled") is not True:
        return None
    for key in DEFAULT_POLICY.keys() - {"enabled"}:
        if not _number(policy.get(key)):
            return None
    for key in ("max_group_attempts_24h", "max_global_attempts_24h"):
        if not isinstance(policy[key], int) or isinstance(policy[key], bool):
            return None
        policy[key] = min(policy[key], DEFAULT_POLICY[key])
    study_groups = supplied.get("study_groups", [])
    if (not isinstance(study_groups, (list, tuple)) or len(study_groups) > 10000
            or any(not _GROUP.fullmatch(f"group:{gid}") for gid in study_groups)):
        return None
    policy["is_study"] = session.split(":", 1)[1] in {str(gid) for gid in study_groups}
    policy["study_min_group_interval_seconds"] = max(1800, policy["study_min_group_interval_seconds"])
    policy["study_min_reply_interval_seconds"] = max(7200, policy["study_min_reply_interval_seconds"])
    if policy["study_min_reply_interval_seconds"] > DAY_SECONDS:
        return None
    # Config may make participation quieter; it cannot lift these bounds.
    policy["min_group_interval_seconds"] = (policy["study_min_group_interval_seconds"] if policy["is_study"]
                                             else max(7200, policy["min_group_interval_seconds"]))
    if policy["min_group_interval_seconds"] > DAY_SECONDS:
        return None  # This checkpoint deliberately retains only the past day.
    policy["max_human_age_seconds"] = min(600, policy["max_human_age_seconds"])
    if now - last_human_at > policy["max_human_age_seconds"]:
        return None
    active = profile.get("active_groups", [])
    ignored = profile.get("ignored_groups", [])
    if not isinstance(active, (list, tuple)) or not isinstance(ignored, (list, tuple)):
        return None
    if len(active) > 10000 or len(ignored) > 10000:
        return None
    group_id = session.split(":", 1)[1]
    if group_id in {str(v) for v in ignored} or (active and group_id not in {str(v) for v in active}):
        return None
    return policy


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate participation state key")
        result[key] = value
    return result


def _read_attempts(path, now):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return []
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("participation state exceeds size limit")
    state = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    if (not isinstance(state, dict) or type(state.get("version")) is not int
            or state["version"] != 1 or not isinstance(state.get("attempts"), list)
            or len(state["attempts"]) > MAX_STATE_ATTEMPTS):
        raise ValueError("invalid participation state")
    attempts = []
    for attempt in state["attempts"]:
        if (not isinstance(attempt, dict) or not isinstance(attempt.get("session"), str)
                or not _GROUP.fullmatch(attempt["session"]) or not _number(attempt.get("at"))
                or attempt["at"] > now):
            raise ValueError("invalid participation attempt or clock rollback")
        if attempt["at"] > now - DAY_SECONDS:
            attempts.append({"session": attempt["session"], "at": attempt["at"]})
    return attempts


def _within_budget(session, now, policy, attempts):
    group_attempts = [a for a in attempts if a["session"] == session]
    return (len(attempts) < policy["max_global_attempts_24h"]
            and len(group_attempts) < policy["max_group_attempts_24h"]
            and all(now - a["at"] >= policy["min_group_interval_seconds"] for a in group_attempts))


def read_recent_reply_times(base, now, required_window_seconds=7200):
    """Read bounded local commits as conservative evidence of having spoken.

    Missing history is empty. Malformed or insufficiently covered history raises,
    so callers cannot accidentally interpret uncertainty as permission to speak.
    Both current commits and the bridge's legacy timestamp format are supported.
    """
    if (not _number(now) or not _number(required_window_seconds)
            or not 0 < required_window_seconds <= DAY_SECONDS):
        raise ValueError("invalid reply history window")
    path = Path(base) / "state" / "outbox.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - MAX_OUTBOX_BYTES)
            stream.seek(start)
            raw = stream.read(MAX_OUTBOX_BYTES)
    except FileNotFoundError:
        return {}
    if start:
        boundary = raw.find(b"\n")
        if boundary < 0:
            raise ValueError("reply history has no complete boundary")
        raw = raw[boundary + 1:]
    if raw and not raw.endswith(b"\n"):
        raise ValueError("reply history append is incomplete")
    lines = raw.splitlines()
    if len(lines) > MAX_OUTBOX_RECORDS:
        raise ValueError("reply history exceeds record limit")
    replies = {}
    timestamps = []
    cutoff = now - required_window_seconds
    for line in lines:
        if not line.strip():
            continue
        if len(line) > MAX_OUTBOX_RECORD_BYTES:
            raise ValueError("reply record exceeds size limit")
        record = json.loads(line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(record, dict) or not _SESSION.fullmatch(str(record.get("session_key", ""))):
            raise ValueError("invalid reply session")
        segments = record.get("segments")
        if not isinstance(segments, list) or not segments or any(not isinstance(part, str) for part in segments):
            raise ValueError("invalid reply content envelope")
        at = record.get("created_at")
        if at is None:
            at = time.mktime(time.strptime(record.get("timestamp", ""), "%Y-%m-%d %H:%M:%S"))
        if not _number(at) or at > now:
            raise ValueError("invalid reply timestamp")
        timestamps.append(at)
        if at > cutoff:
            session = record["session_key"]
            replies[session] = max(replies.get(session, 0), at)
    if start and (not timestamps or timestamps[0] > cutoff
                  or any(a > b for a, b in zip(timestamps, timestamps[1:]))):
        raise ValueError("bounded reply history cannot cover required window")
    return replies


def _reply_interval_allows(session, now, policy, replies, last_replied_at=0):
    if not policy["is_study"]:
        return True
    if not _number(last_replied_at) or last_replied_at > now:
        return False
    last_reply = max(replies.get(session, 0), last_replied_at)
    return last_reply == 0 or now - last_reply >= policy["study_min_reply_interval_seconds"]


def study_reply_allowed(session, now, cfg, base, last_replied_at=0):
    """A committed reply/ACK keeps study-group unsolicited participation quiet."""
    policy = _policy_and_context(session, now, now, cfg)
    if policy is None:
        return False
    if not policy["is_study"]:
        return True
    try:
        replies = read_recent_reply_times(base, now, policy["study_min_reply_interval_seconds"])
        return _reply_interval_allows(session, now, policy, replies, last_replied_at)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return False


def exploration_context_allowed(session, last_human_at, now, cfg):
    """Recheck freshness/config without charging or rechecking a reserved quota."""
    return _policy_and_context(session, last_human_at, now, cfg) is not None


def rank_exploration_candidates(candidates, now, cfg, base, last_replied_at=None):
    """Read-only fair order of (session, latest-human-time) candidates.

    Serve groups with fewer attempts first, then the least recently attempted.
    Older still-fresh messages win the next tie; exact ties rotate by UTC day,
    rather than permanently favoring whichever group arrived first in a dict.
    """
    if (not isinstance(candidates, (list, tuple)) or len(candidates) > 10000
            or not _number(now)):
        return []
    path = Path(base) / "state" / "participation_state.json"
    try:
        if path.with_suffix(".lock").exists():
            return []
        attempts = _read_attempts(path, now)
        replies = None
        replies_checked = False
        ranked = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, (list, tuple)) or len(candidate) != 2:
                return []
            session, last_human_at = candidate
            policy = _policy_and_context(session, last_human_at, now, cfg)
            if policy is None or session in seen:
                continue
            seen.add(session)
            if not _within_budget(session, now, policy, attempts):
                continue
            if policy["is_study"]:
                if not replies_checked:
                    replies_checked = True
                    try:
                        replies = read_recent_reply_times(base, now, policy["study_min_reply_interval_seconds"])
                    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                        replies = None
                memory_reply = (last_replied_at or {}).get(session, 0)
                if replies is None or not _reply_interval_allows(session, now, policy, replies, memory_reply):
                    continue
            times = [a["at"] for a in attempts if a["session"] == session]
            daily_tie = hashlib.sha256(f"{int(now // DAY_SECONDS)}:{session}".encode()).digest()
            ranked.append(((len(times), max(times, default=0), last_human_at, daily_tie), session))
        return [session for _, session in sorted(ranked)]
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return []


def exploration_allowed(session, last_human_at, now, cfg, base):
    """Read-only eligibility check. Times are finite Unix seconds, not ISO text."""
    policy = _policy_and_context(session, last_human_at, now, cfg)
    if policy is None:
        return False
    path = Path(base) / "state" / "participation_state.json"
    try:
        if path.with_suffix(".lock").exists():
            return False
        return (_within_budget(session, now, policy, _read_attempts(path, now))
                and study_reply_allowed(session, now, cfg, base))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False


def reserve_exploration(session, last_human_at, now, cfg, base):
    """Atomically record an attempt before waking the model; False means do not wake.

    Lock contention, invalid state, and disk errors fail closed. A lock left by a
    process crash deliberately blocks exploration until the operator reviews it.
    """
    policy = _policy_and_context(session, last_human_at, now, cfg)
    if policy is None:
        return False
    path = Path(base) / "state" / "participation_state.json"
    lock = path.with_suffix(".lock")
    locked = False
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        locked = True
        os.close(descriptor)
        attempts = _read_attempts(path, now)
        if (not _within_budget(session, now, policy, attempts)
                or not study_reply_allowed(session, now, cfg, base)):
            return False
        attempts.append({"session": session, "at": now})
        payload = json.dumps({"version": 1, "attempts": attempts}, ensure_ascii=False)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="participation_", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if locked:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
