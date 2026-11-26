"""Read-only restart recovery of recent, unprocessed study-group messages.

Only daemon history receipts and trusted local configuration establish inputs.
No image is opened/downloaded, no event handler is called, and no state is
written. Unread watermarks use turn creation, never completion, so messages
arriving during generation survive a silent turn's completion.
"""
from hakimi.core import bot_identity
import json
import math
import re
from pathlib import Path

from hakimi.bridge.participation_policy import read_recent_reply_times

MAX_HISTORY_BYTES = 512 * 1024
MAX_HISTORY_ROWS = 2000
MAX_ROW_BYTES = 64 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_AUDIT_BYTES = 512 * 1024
MAX_PER_GROUP = 20
MAX_MESSAGES = 500
_SESSION = re.compile(r"(?:group|private):[1-9][0-9]{0,19}\Z")
_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_MESSAGE = re.compile(r"-?[0-9]{1,20}\Z")
_KNOWN_BOTS = bot_identity.KNOWN_BOT_IDS


def _epoch(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= now


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate recovery state key")
        result[key] = value
    return result


def _path(base, name):
    path = base / "state" / name
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("redirected recovery input")
    return path


def _stamp(path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino
    except FileNotFoundError:
        return None


def _json_file(path, maximum):
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except FileNotFoundError:
        return None
    if not raw or len(raw) > maximum:
        raise ValueError("invalid recovery state size")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)


def _rows(path, *, maximum, count, tail=False):
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            if size > maximum and not tail:
                raise ValueError("recovery journal too large")
            start = max(0, size - maximum)
            if start:
                stream.seek(start - 1)
                boundary = stream.read(1) == b"\n"
            else:
                boundary = True
            stream.seek(start)
            raw = stream.read(maximum + 1)
    except FileNotFoundError:
        return []
    if len(raw) > maximum or (raw and not raw.endswith(b"\n")):
        raise ValueError("incomplete recovery journal")
    if not boundary:
        raw = raw.partition(b"\n")[2]
    lines = raw.splitlines()
    if len(lines) > count and not tail:
        raise ValueError("too many recovery journal records")
    result = []
    for line in lines[-count:]:
        if not line or len(line) > MAX_ROW_BYTES:
            raise ValueError("invalid recovery row size")
        row = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(row, dict):
            raise ValueError("invalid recovery journal object")
        result.append(row)
    return result


def _ids(value):
    if not isinstance(value, (list, tuple)) or len(value) > 10000:
        raise ValueError("invalid configured identity list")
    if any(type(item) not in (str, int) or not _ID.fullmatch(str(item)) for item in value):
        raise ValueError("invalid configured identity")
    return {str(item) for item in value}


def _audit_watermark(row, now):
    required = {"version", "trigger_id", "session_key", "created_at", "claimed_at", "completed_at",
                "wake_mode", "provider", "outcome"}
    if (not isinstance(row, dict) or not required <= set(row) or type(row.get("version")) is not int
            or row["version"] != 1 or not isinstance(row.get("session_key"), str)
            or not _SESSION.fullmatch(row["session_key"])
            or not isinstance(row.get("trigger_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", row["trigger_id"])
            or not _epoch(row.get("created_at"), now)
            or not _epoch(row.get("completed_at"), now)
            or row["completed_at"] < row["created_at"]
            or row.get("provider") not in (None, "gemini", "dsh")
            or any(not isinstance(row.get(key), str) or len(row[key]) > 120
                   for key in ("wake_mode", "outcome"))):
        raise ValueError("invalid recovery turn audit")
    claimed = row["claimed_at"]
    if claimed is not None and (not _epoch(claimed, now) or not row["created_at"] <= claimed <= row["completed_at"]):
        raise ValueError("invalid recovery claim time")
    return row["session_key"], row["created_at"]


def recover_study_messages(base, cfg, now, *, reply_watermarks=True):
    """Return bounded messages/reply times, or None when inputs are uncertain."""
    from hakimi.chat.interaction_policy import ignored_text
    try:
        if not _epoch(now, now) or not isinstance(cfg, dict):
            return None
        policy, profile = cfg.get("participation_policy", {}), cfg.get("bot_profile", {})
        if not all(isinstance(item, dict) for item in (policy, profile)):
            return None
        groups = _ids(policy.get("study_groups", []))
        active, ignored = _ids(profile.get("active_groups", [])), _ids(profile.get("ignored_groups", []))
        groups = (groups & active if active else groups) - ignored
        if policy.get("enabled", True) is False or not groups:
            return {"messages": [], "last_replied_at": {}}
        if policy.get("enabled", True) is not True:
            return None
        sessions = {"group:" + group for group in groups}
        excluded = set(_KNOWN_BOTS) | _ids([profile.get("self_qq", bot_identity.SELF_QQ)])
        for key in ("ignored_user_ids", "known_bot_ids", "ignored_bot_ids"):
            excluded |= _ids(profile.get(key, [])) | _ids(cfg.get(key, []))
        base = Path(base).resolve()
        from hakimi.chat.turn_lanes import LANES
        turn_names = ("turn_state.json",) + tuple("turn_lanes/"+lane+".json" for lane in LANES if lane)
        names = ("participation_state.json", "turn_events.jsonl", "outbox.jsonl", "chat_history.jsonl") + turn_names
        paths = {name: _path(base, name) for name in names}
        lock = _path(base, "participation_state.lock")
        if lock.exists():
            return None
        stamps = {name: _stamp(path) for name, path in paths.items()}
        watermarks = {session: 0 for session in sessions}

        attempts = _json_file(paths["participation_state.json"], 65536)
        if attempts is not None:
            if (not isinstance(attempts, dict) or type(attempts.get("version")) is not int
                    or attempts["version"] != 1 or not isinstance(attempts.get("attempts"), list)
                    or len(attempts["attempts"]) > 256):
                raise ValueError("invalid recovery attempts")
            for attempt in attempts["attempts"]:
                if (not isinstance(attempt, dict) or not isinstance(attempt.get("session"), str)
                        or not re.fullmatch(r"group:[1-9][0-9]{0,19}", attempt["session"])
                        or not _epoch(attempt.get("at"), now)):
                    raise ValueError("invalid recovery attempt")
                if attempt["session"] in sessions:
                    watermarks[attempt["session"]] = max(watermarks[attempt["session"]], attempt["at"])

        pending = []
        for name in turn_names:
            state = _json_file(paths[name], MAX_STATE_BYTES)
            if state is not None:
                if (not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1
                        or state.get("status") not in {"idle", "queued", "primary_active", "fallback_active", "completed"}):
                    raise ValueError("invalid recovery turn state")
                if state["status"] != "idle" or "payload" in state:
                    payload = state.get("payload")
                    if (not isinstance(payload, dict) or not isinstance(payload.get("session_key"), str)
                            or not _SESSION.fullmatch(payload["session_key"])):
                        raise ValueError("invalid recovery turn creation")
                    session = payload["session_key"]
                    if session in sessions:
                        created = [record["created_at"] for record in (state, payload) if "created_at" in record]
                        if not created or any(not _epoch(at, now) for at in created):
                            raise ValueError("invalid recovery study turn creation")
                        watermarks[session] = max(watermarks[session], *created)
                lane_pending = state.get("pending_turn_events", [])
                if not isinstance(lane_pending, list) or len(lane_pending) > 1000:
                    raise ValueError("invalid pending recovery audit")
                pending.extend(lane_pending)

        for row in _rows(paths["turn_events.jsonl"], maximum=MAX_AUDIT_BYTES, count=1000) + pending:
            session, created = _audit_watermark(row, now)
            if session in sessions:
                watermarks[session] = max(watermarks[session], created)
        replied = read_recent_reply_times(base, now, required_window_seconds=7200)
        if not isinstance(replied, dict) or any(not isinstance(session, str) or not _SESSION.fullmatch(session)
                                               or not _epoch(at, now) for session, at in replied.items()):
            raise ValueError("invalid recovered reply times")
        replied = {session: at for session, at in replied.items() if session in sessions}
        if reply_watermarks:
            for session, at in replied.items():
                watermarks[session] = max(watermarks[session], at)

        recovered, seen = [], set()
        for row in _rows(paths["chat_history.jsonl"], maximum=MAX_HISTORY_BYTES, count=MAX_HISTORY_ROWS, tail=True):
            session, message = row.get("session_key"), row.get("message_id")
            if session not in sessions or type(message) not in (str, int) or not _MESSAGE.fullmatch(str(message)):
                continue
            message = str(int(message))
            key = (session, message)
            if key in seen:
                continue
            seen.add(key)
            at, uid, text = row.get("received_at"), row.get("user_id"), row.get("text")
            if (not _epoch(at, now) or not 0 <= now - at <= 600 or at <= watermarks[session]
                    or type(uid) not in (str, int) or not _ID.fullmatch(str(uid)) or str(uid) in excluded
                    or row.get("is_bot", False) is not False or not isinstance(text, str)
                    or not text.strip() or ignored_text(text, cfg)):
                continue
            recovered.append({"time": str(row.get("time", ""))[:40], "message_id": message,
                              "session_key": session, "user_id": str(uid), "nickname": str(row.get("nickname", ""))[:120],
                              "text": text[:1500], "received_at": at, "image_paths": []})
        # Prefer newest receipts under caps, then return chronological order.
        counts, bounded = {}, []
        for row in sorted(recovered, key=lambda item: item["received_at"], reverse=True):
            session = row["session_key"]
            if counts.get(session, 0) >= MAX_PER_GROUP:
                continue
            counts[session] = counts.get(session, 0) + 1
            bounded.append(row)
            if len(bounded) >= MAX_MESSAGES:
                break
        if lock.exists() or any(_stamp(path) != stamps[name] for name, path in paths.items()):
            return None
        return {"messages": list(reversed(bounded)), "last_replied_at": replied}
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return None
