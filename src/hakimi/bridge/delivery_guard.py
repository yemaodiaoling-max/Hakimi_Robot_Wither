"""Durable, conservative outbox delivery. No network calls or runtime files at import.

An ambiguous send is never retried automatically: OneBot does not provide an
idempotency key. The state file is an operational checkpoint, not chat memory.
"""

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from hakimi.bridge.reply_format import MAX_REPLY_SEGMENTS, MAX_SEGMENT_CHARS


DEFAULT_POLICY = {
    "global_min_interval_seconds": 3,
    "session_min_interval_seconds": 15,
    "max_messages_per_minute": 8,
    "duplicate_window_seconds": 300,
    "ack_timeout_seconds": 12,
    "platform_error_pause_seconds": 1800,
    "network_error_pause_seconds": 15,
    "max_queue_age_seconds": 600,
    "max_segments_per_reply": MAX_REPLY_SEGMENTS,
    "max_segment_chars": MAX_SEGMENT_CHARS,
}


def content_fingerprint(session_key, text):
    # Reply IDs differ even for the same accidentally regenerated answer.
    normalized = re.sub(r"\[CQ:reply,id=-?\d+\]", "", text)
    normalized = " ".join(normalized.split())
    return hashlib.sha256((session_key + "\0" + normalized).encode("utf-8")).hexdigest()


def _attempt_scope(pending):
    session = pending['session_key']
    if session.startswith('private:'):
        # Two separate questions can legitimately receive the same short answer.
        source = pending.get('reply_target_id') or pending.get('trigger_id') or pending['reply_id']
        return 'private_attempts', session + '/' + str(source)
    return 'attempts', session


class DeliveryGuard:
    def __init__(self, state_path, outbox_path, policy=None, clock=time.time):
        self.state_path = Path(state_path)
        self.outbox_path = Path(outbox_path)
        self.clock = clock
        self.configure(policy)
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
                cursor = self.state.get("cursor")
                if (self.state.get("version") != 1 or not isinstance(cursor, int)
                        or isinstance(cursor, bool) or cursor < 0):
                    raise ValueError("unsupported checkpoint")
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError("delivery checkpoint unreadable; manual review required") from exc
            if self.state.get("pending", {}).get("inflight"):
                self.state["paused_until"] = self.clock() + self.policy["network_error_pause_seconds"]
                self._complete("unknown_after_restart")
        else:
            # Migration must never replay the historical outbox from an old daemon.
            cursor = self.outbox_path.stat().st_size if self.outbox_path.exists() else 0
            self.state = {"version": 1, "cursor": cursor, "pending": {}, "attempts": [],
                          "completed_ids": [], "paused_until": 0, "counts": {},
                          "last_event": {"status": "legacy_baseline", "at": self.clock(), "bytes": cursor}}
            self._save()

    def configure(self, policy=None):
        self.policy = dict(DEFAULT_POLICY)
        for key, value in (policy or {}).items():
            if (key in self.policy and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value > 0):
                self.policy[key] = value

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        from hakimi.core.runtime_io import atomic_replace
        atomic_replace(temporary,self.state_path)

    def _event(self, status, **extra):
        self.state["last_event"] = {"status": status, "at": self.clock(), **extra}
        counts = self.state.setdefault("counts", {})
        counts[status] = counts.get(status, 0) + 1

    def _complete(self, status):
        pending = self.state.get("pending") or {}
        segments = pending.get('segments')
        if isinstance(segments, list) and any(isinstance(text, str) and text.startswith('[CQ:file,name=_drawing_') for text in segments):
            from hakimi.tools.drawing_delivery import delivery_finished, drawing_in
            if drawing_id := drawing_in(segments):
                try:
                    delivery_finished(self.state_path.parent.parent, drawing_id, pending['reply_id'], status)
                except (OSError, sqlite3.Error):
                    from hakimi.tools.math_render import log_tool_call
                    log_tool_call(self.state_path.parent.parent, 'image.draw', 'delivery_receipt_error')
        if pending.get('rendered_math'):
            from hakimi.core.runtime_io import atomic_replace
            folder=self.state_path.parent/'math_receipts'
            folder.mkdir(exist_ok=True)
            receipt=folder/(pending['reply_id']+'.json')
            temporary=receipt.with_suffix('.tmp')
            temporary.write_text(json.dumps({'status':status,'at':self.clock()}),encoding='utf-8')
            atomic_replace(temporary,receipt)
        if pending.get("reply_id"):
            ids = self.state.setdefault("completed_ids", [])
            ids.append(pending["reply_id"])
            self.state["completed_ids"] = ids[-1000:]
        self._event(status, session_key=pending.get("session_key"), reply_id=pending.get("reply_id"),
                    acknowledged_segments=pending.get("next_segment", 0))
        from hakimi.features.diary_store import cleanup as cleanup_diary_pdf
        cleanup_diary_pdf(pending.get('segments',[]),self.state_path.parent.parent,
                          pending.get('reply_id',''),status)
        self.state["pending"] = {}
        self._save()
        if pending.get('provider') == 'gemini':
            from hakimi.models.carbon_timing import delivery_ack
            delivery_ack(self.state_path.parent, pending.get('trigger_id'), status)

    @property
    def pending(self):
        return self.state.get("pending") or {}

    def _load_next(self):
        if self.pending or not self.outbox_path.exists():
            return
        cursor = self.state["cursor"]
        if self.outbox_path.stat().st_size < cursor:
            # A manual truncation needs an operator to reconcile the checkpoint.
            if self.state.get("last_event", {}).get("status") != "outbox_replaced_review_required":
                self._event("outbox_replaced_review_required")
                self._save()
            return
        with self.outbox_path.open("rb") as stream:
            stream.seek(cursor)
            raw = stream.readline()
            if not raw or not raw.endswith(b"\n"):
                return  # Writer has not finished the JSONL record yet.
            self.state["cursor"] = stream.tell()
        try:
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict):
                raise ValueError("record must be an object")
        except (UnicodeError, ValueError):
            self._event("malformed_record", bytes=len(raw))
            self._save()
            return
        reply_id = str(record.get("reply_id") or hashlib.sha256(raw).hexdigest())
        if reply_id in self.state.get("completed_ids", []):
            self._event("duplicate_reply_id", reply_id=reply_id)
            self._save()
            return
        created_at = record.get("created_at")
        if (not isinstance(created_at, (float, int)) or isinstance(created_at, bool)
                or not math.isfinite(created_at)):
            try:
                created_at = time.mktime(time.strptime(record.get("timestamp", ""), "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError, OverflowError):
                created_at = self.clock()
        self.state["pending"] = {
            "reply_id": reply_id, "session_key": record.get("session_key", ""),
            "trigger_id": str(record.get("trigger_id", ""))[:100],
            "segments": record.get("segments", [record.get("text", "")]),
            "created_at": min(created_at, self.clock()), "next_segment": 0,
            "inflight": False, "is_proactive": bool(record.get("is_proactive", False)),
            "reply_target_id": str(record.get('reply_target_id') or ''),
            "rendered_math": bool(record.get('rendered_math', False)),
            "interaction": record.get('interaction'), "provider": record.get('provider'),
        }
        self._save()

    def next_message(self):
        """Return an eligible (session, text), or None without discarding a deferred item."""
        self._load_next()
        pending = self.pending
        if not pending:
            return None
        session = pending.get("session_key")
        segments = pending.get("segments")
        limit=self.policy['max_segments_per_reply']
        if (not isinstance(session, str) or not re.fullmatch(r"(?:group|private):[1-9]\d*", session)
                or not isinstance(segments, list) or not segments
                or (not pending.get('rendered_math') and len(segments) > limit)
                or any(not isinstance(s, str) or not s.strip() or len(s) > self.policy["max_segment_chars"] for s in segments)):
            self._complete("invalid_payload")
            return None
        from hakimi.bridge.interaction_policy import blocked_outgoing_topic
        if blocked_outgoing_topic(''.join(segments)):
            self._complete('ignored_topic')
            return None
        from hakimi.bridge.bot_registry import cooling
        interaction=pending.get('interaction') or {}
        if interaction.get('bot_target') and cooling(self.outbox_path.parent.parent,interaction['bot_target'],self.clock()):
            self._complete('bot_cooldown')
            return None
        now = self.clock()
        queue_age=self.policy['max_queue_age_seconds']
        if pending.get('rendered_math'):
            queue_age=max(queue_age,len(segments)*(self.policy['session_min_interval_seconds']+self.policy['ack_timeout_seconds'])+90)
        if now - pending["created_at"] > queue_age:
            self._complete("expired")
            return None
        if pending.get("inflight"):
            return None
        if now < self.state.get("paused_until", 0):
            return None
        text = segments[pending["next_segment"]]
        key, scope = _attempt_scope(pending)
        fingerprint = content_fingerprint(scope, text)
        attempts = self.state.get(key, [])
        if any(a["fingerprint"] == fingerprint and now - a["at"] < self.policy["duplicate_window_seconds"] for a in attempts):
            self._complete("duplicate_content")
            return None
        if key == 'private_attempts':
            return session, text
        # Checkpoints from before private delivery was separated can contain both.
        attempts = [a for a in attempts if not a['session_key'].startswith('private:')]
        if pending["next_segment"] == 0 and attempts and now - attempts[-1]["at"] < self.policy["global_min_interval_seconds"]:
            return None
        if pending["next_segment"] == 0 and any(a["session_key"] == session and now - a["at"] < self.policy["session_min_interval_seconds"] for a in attempts):
            return None
        if pending["next_segment"] == 0 and sum(now - a["at"] < 60 and a.get("round_start", True) for a in attempts) >= self.policy["max_messages_per_minute"]:
            return None
        return session, text

    def begin_attempt(self):
        """Persist BEFORE network I/O so crashes cannot cause an automatic duplicate."""
        pending = self.pending
        now = self.clock()
        text = pending["segments"][pending["next_segment"]]
        pending["inflight"] = True
        retention = max(3600, self.policy["duplicate_window_seconds"], self.policy["session_min_interval_seconds"])
        key, scope = _attempt_scope(pending)
        attempts = [a for a in self.state.get(key, []) if now - a["at"] < retention]
        attempts.append({"at": now, "session_key": pending["session_key"],
                         "round_start": pending["next_segment"] == 0,
                         "fingerprint": content_fingerprint(scope, text)})
        self.state[key] = attempts[-2000:]
        self._event("awaiting_ack", session_key=pending["session_key"], reply_id=pending["reply_id"])
        self._save()

    def finish_attempt(self, result):
        """success is acknowledged; offline is definitely unsent; all else stops this batch."""
        pending = self.pending
        if result == "file_error":
            # A rejected attachment must not pause unrelated text chat globally.
            self._complete("file_failed")
        elif result == "offline":
            pending["inflight"] = False
            key, _ = _attempt_scope(pending)
            self.state[key].pop()
            self._event("offline_queued", session_key=pending["session_key"])
            self._save()
        elif result == "success":
            pending["inflight"] = False
            pending["next_segment"] += 1
            if pending["next_segment"] >= len(pending["segments"]):
                self._complete("delivered")
            else:
                self._event("segment_acknowledged", session_key=pending["session_key"])
                self._save()
        else:
            pause = 'platform_error_pause_seconds' if result == 'rejected' else 'network_error_pause_seconds'
            self.state["paused_until"] = self.clock() + self.policy[pause]
            self._complete(result if result in {"rejected", "unknown", "unknown_after_restart"} else "unknown")

    def reject_pending(self, reason="session_not_allowed"):
        self._complete(reason)

    def note_offline(self):
        if self.pending and self.state.get("last_event", {}).get("status") != "offline_queued":
            self._event("offline_queued", session_key=self.pending["session_key"])
            self._save()
