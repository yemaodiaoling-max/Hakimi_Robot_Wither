"""Durable rolling per-group Gemini reply quota.

Only a Gemini reply that has been committed to the outbox is recorded.  One
turn is one entry regardless of how many QQ messages/segments it contains.
Private sessions and internal reflections are never limited here.
"""
from __future__ import annotations

import hashlib
import math
import re
import time

WINDOW_SECONDS = 30 * 60
MAX_REPLIES = 11
HISTORY_KEY = "group_reply_history"
TAKEOVER_REASON = "group_hourly_limit"
# Defensive bound for a pathological number of groups.  Normal traffic is
# already bounded by the rolling window and by the coordinator's turn flow.
MAX_HISTORY_ENTRIES = 4096

# Explicit aliases used by the coordinator/tests.
GROUP_REPLY_WINDOW_SECONDS = WINDOW_SECONDS
MAX_GROUP_GEMINI_REPLIES = MAX_REPLIES
GROUP_REPLY_HISTORY = HISTORY_KEY
GROUP_HOURLY_LIMIT_REASON = TAKEOVER_REASON

_GROUP_SESSION = re.compile(r"group:[1-9][0-9]{0,19}\Z")
_MAX_TRIGGER_CHARS = 200


def is_group_session(value):
    """Return True only for a canonical group session key."""
    return isinstance(value, str) and bool(_GROUP_SESSION.fullmatch(value))


def _safe_epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _normalize_trigger(value):
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= _MAX_TRIGGER_CHARS:
        return value
    return "hashed_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entry_epoch(item):
    if not isinstance(item, dict):
        return None
    for key in ("completed_at", "at", "timestamp"):
        value = _safe_epoch(item.get(key))
        if value is not None:
            return value
    return None


class GroupReplyQuota:
    """Pure, state-in/state-out helper for the coordinator's durable history."""

    def __init__(self, clock=time.time, *, window_seconds=WINDOW_SECONDS,
                 max_replies=MAX_REPLIES):
        self.clock = clock
        self.window_seconds = self._positive(window_seconds, "window_seconds")
        if (isinstance(max_replies, bool) or not isinstance(max_replies, int)
                or max_replies < 1):
            raise ValueError("max_replies must be a positive integer")
        self.max_replies = max_replies

    @staticmethod
    def _positive(value, name):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError(name + " must be a finite positive number")
        return float(value)

    def _now(self, now):
        value = self.clock() if now is None else now
        result = _safe_epoch(value)
        if result is None:
            raise ValueError("now must be a finite non-negative epoch")
        return result

    @classmethod
    def _iter_entries(cls, history):
        """Yield canonical (session, trigger, completed_at) entries.

        The canonical on-disk form is a list, but reading an older/dict-shaped
        history is supported so an upgrade never resets the quota.
        """
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                session = item.get("session_key")
                trigger = _normalize_trigger(item.get("trigger_id"))
                at = _entry_epoch(item)
                if is_group_session(session) and trigger and at is not None:
                    yield session, trigger, at
            return
        if not isinstance(history, dict):
            return
        for session, rows in history.items():
            if not is_group_session(session):
                continue
            if isinstance(rows, list):
                for index, item in enumerate(rows):
                    if isinstance(item, dict):
                        trigger = _normalize_trigger(item.get("trigger_id"))
                        at = _entry_epoch(item)
                    else:
                        trigger = None
                        at = _safe_epoch(item)
                    if trigger is None and at is not None:
                        trigger = "legacy:" + session + ":" + str(index) + ":" + str(at)
                    if trigger and at is not None:
                        yield session, trigger, at
            elif isinstance(rows, dict):
                for raw_trigger, item in rows.items():
                    trigger = _normalize_trigger(raw_trigger)
                    if isinstance(item, dict):
                        at = _entry_epoch(item)
                    else:
                        at = _safe_epoch(item)
                    if trigger and at is not None:
                        yield session, trigger, at
            else:
                at = _safe_epoch(rows)
                if at is not None:
                    yield session, "legacy:" + session + ":" + str(at), at

    def prune(self, history, now=None):
        """Return the canonical, sorted history with expired entries removed."""
        now = self._now(now)
        newest = {}
        for session, trigger, at in self._iter_entries(history):
            if at > now or now - at >= self.window_seconds:
                continue
            key = (session, trigger)
            if key not in newest or at > newest[key]:
                newest[key] = at
        entries = [{"session_key": session, "trigger_id": trigger, "completed_at": at}
                   for (session, trigger), at in newest.items()]
        entries.sort(key=lambda item: (item["completed_at"], item["session_key"],
                                       item["trigger_id"]))
        if len(entries) > MAX_HISTORY_ENTRIES:
            entries = entries[-MAX_HISTORY_ENTRIES:]
        return entries

    def needs_prune(self, history, now=None):
        return self.prune(history, now) != history

    def count(self, history, session_key, now=None):
        if not is_group_session(session_key):
            return 0
        return sum(1 for item in self.prune(history, now)
                   if item["session_key"] == session_key)

    def at_limit(self, history, session_key, now=None):
        return self.count(history, session_key, now) >= self.max_replies

    def record(self, history, session_key, trigger_id, completed_at=None, *, now=None):
        """Record one committed reply once; never update an existing trigger."""
        now = self._now(now)
        history = self.prune(history, now)
        if not is_group_session(session_key):
            return history
        trigger = _normalize_trigger(trigger_id)
        if trigger is None:
            return history
        if any(item["session_key"] == session_key and item["trigger_id"] == trigger
               for item in history):
            return history
        at = _safe_epoch(completed_at)
        if at is None or at > now:
            at = now
        if now - at >= self.window_seconds:
            return history
        history.append({"session_key": session_key, "trigger_id": trigger,
                        "completed_at": at})
        return self.prune(history, now)

    def initialize(self, events, pending_events=(), now=None):
        """Initialize from bounded audit events, including unflushed pending ones.

        The coordinator intentionally never scans the whole outbox.  Only audit
        records that are recent, Gemini, replied and group-scoped are eligible.
        """
        now = self._now(now)
        entries = []
        for source in (events, pending_events):
            if not source:
                continue
            for record in source:
                if not isinstance(record, dict):
                    continue
                if record.get("provider") != "gemini" or record.get("outcome") != "replied":
                    continue
                session = record.get("session_key")
                trigger = _normalize_trigger(record.get("trigger_id"))
                at = _entry_epoch(record)
                if is_group_session(session) and trigger and at is not None:
                    entries.append({"session_key": session, "trigger_id": trigger,
                                    "completed_at": at})
        return self.prune(entries, now)


_DEFAULT = GroupReplyQuota()


def prune(history, now=None):
    return _DEFAULT.prune(history, now)


def count(history, session_key, now=None):
    return _DEFAULT.count(history, session_key, now)


def at_limit(history, session_key, now=None):
    return _DEFAULT.at_limit(history, session_key, now)


def record(history, session_key, trigger_id, completed_at=None, now=None):
    return _DEFAULT.record(history, session_key, trigger_id, completed_at, now=now)


def initialize(events, pending_events=(), now=None):
    return _DEFAULT.initialize(events, pending_events, now)
