"""Personal review state, lock and audit files."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


REVIEW_INTERVAL = 2 * 3600
REVIEW_TTL_SECONDS = 190
RECENT_WINDOW = 48 * 3600
COOLDOWN = 2 * 3600
SIGNIFICANT_GAP = 4 * 3600
MAX_PEOPLE = 3
MAX_EVIDENCE = 8
MAX_NEW_EVIDENCE = 6
MAX_OLD_CONTEXT = 2
PUBLICATION_HISTORY_LIMIT = 64
PLAN_REFRESH_SECONDS = 20 * 60
RETRY_BACKOFF_SECONDS = 30 * 60
MAX_RETRIES_SAME_EVIDENCE_DAY = 2
MAX_SEEN_EVIDENCE = 64
MAX_EXCERPT = 220
MAX_AUDIT_BYTES = 256 * 1024
MAX_AUDIT_LINES = 200
MAX_STATE_BYTES = 512 * 1024
STATUSES = {"scheduled", "claimed", "completed", "expired", "no_change", "written"}
SIGNIFICANT = re.compile(r"约定|答应|更正|纠正|完成|解决|抱歉|对不起|谢谢|帮|记住|以后|下次|承诺")
PERSON = re.compile(r"[1-9][0-9]{0,19}")


class ReviewError(RuntimeError):
    pass


def _path(base):
    return Path(base) / "state" / "personal_review.json"


def _audit_path(base):
    return Path(base) / "state" / "personal_review_audit.jsonl"


@contextmanager
def _locked(base):
    """Serialise the supervisor tick and tool-receipt updates."""
    path = Path(base) / "state" / "personal_review.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if stream.read(1) == b"":
                stream.write(b"0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _load(base):
    try:
        value = json.loads(_path(base).read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("version") == 1:
            for key in ("people", "groups", "reviews"):
                if not isinstance(value.get(key), dict):
                    value[key] = {}
            if not isinstance(value.get("audit"), list):
                value["audit"] = []
            return value
    except (OSError, ValueError):
        pass
    return {"version": 1, "next_due": 0, "pending": None, "pending_plan": None,
            "people": {}, "groups": {}, "reviews": {}, "audit": []}


def _save(base, state):
    path = _path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["audit"] = [item for item in state.get("audit", []) if isinstance(item, dict)][-200:]
    for key in list(state.get("reviews", {})):
        if len(state["reviews"]) <= 120:
            break
        oldest = min(state["reviews"], key=lambda value: float(
            (state["reviews"][value] or {}).get("at", 0)))
        state["reviews"].pop(oldest, None)
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
        raise ReviewError("personal review checkpoint too large")
    temp = path.with_suffix(".tmp")
    temp.write_text(raw, encoding="utf-8")
    temp.replace(path)


def _audit(base, review, status, now, reason="", count=0):
    if status not in STATUSES:
        return
    path = _audit_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": float(now), "review_id": str(review.get("review_id", ""))[:80],
              "status": status, "reason": str(reason)[:120],
              "group": str(review.get("group", ""))[:40], "written": int(count)}
    try:
        if not path.exists():
            path.touch()
        # Read/write mode is required: the old append-only handle could append
        # but then failed on read(), so the audit file never rotated.
        with path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            stream.write((json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            if stream.tell() > MAX_AUDIT_BYTES:
                stream.seek(0)
                lines = stream.read().splitlines()[-MAX_AUDIT_LINES:]
                stream.seek(0)
                stream.truncate()
                stream.write(b"\n".join(lines) + (b"\n" if lines else b""))
    except OSError:
        pass


def _day(at):
    try:
        return datetime.fromtimestamp(float(at), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _evidence_signature(ids):
    values = sorted({str(value).upper().removeprefix("M") for value in ids
                     if str(value).upper().removeprefix("M").isdigit()})
    return hashlib.sha256(",".join(values).encode()).hexdigest()[:24]


def _ensure_day(state, now):
    day = _day(now)
    daily = state.get("daily")
    if not isinstance(daily, dict) or daily.get("day") != day:
        daily = {"day": day, "attempts": 0, "published": 0, "no_candidates": 0,
                 "deferred": 0, "expired": 0, "no_change": 0, "written": 0}
        state["daily"] = daily
    for key in ("attempts", "published", "no_candidates", "deferred", "expired",
                "no_change", "written"):
        daily.setdefault(key, 0)
    return daily


def _publications(state, now):
    values = []
    for value in state.get("publications", []):
        try:
            at = float(value)
        except (TypeError, ValueError):
            continue
        if now - at < 24 * 3600:
            values.append(at)
    state["publications"] = sorted(values)[-PUBLICATION_HISTORY_LIMIT:]
    return values
