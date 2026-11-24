"""Cross-process, single-owner generation turns and idempotent reply commits.

The primary provider is Gemini; dsh can claim after a reported failure, a
confirmed stall, or an absolute ceiling. Silence never implies quota exhaustion.
No model invocation or network access occurs here.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from hakimi.bridge.group_reply_quota import (GROUP_REPLY_HISTORY, GROUP_HOURLY_LIMIT_REASON,
                               GroupReplyQuota, is_group_session)
from hakimi.bridge.reply_format import MAX_REPLY_SEGMENTS, MAX_SEGMENT_CHARS
from hakimi.bridge import user_reply_quota
from hakimi.core.runtime_io import read_json_cached, atomic_replace
from hakimi.models.provider_health import ProviderHealth, availability

MAX_TURN_EVENTS = 1000
MAX_TURN_EVENTS_BYTES = 512 * 1024
MAX_TURN_EVENT_BYTES = 2048
# Internal reflections are bounded background work, never a 10-minute primary
# turn.  The native agent additionally uses a <=180s generate timeout.
INTERNAL_REVIEW_TTL_SECONDS = 190
_AUDIT_OUTCOMES = frozenset({"replied", "no_reply", "expired", "fallback_timeout",
                            "fallback_failed", "fallback_error", "review_failed", "written", "superseded", "other"})
_AUDIT_WAKE_MODES = frozenset({"即时唤醒(被@/私聊)", "随机时间窗窥屏",
                              "低频尝试参与", "冷场主动抛话题", "上下文积累判断", "相关话题续聊", "被@后监听窗口",
                              "个人记忆反思"})
_AUDIT_TAKEOVERS = frozenset({"primary_grace_expired", "primary_timeout", "primary_stalled",
                             "primary_hard_timeout", "operator_mode_dsh", "group_hourly_limit",
                             "reported:other",
                             *("reported:" + reason for reason in
                               ("quota", "auth", "model_error", "unavailable", "prompt_render_error",
                                "vertex_budget", "gateway_unavailable"))})

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _reply_sources(payload):
    """QQ input identities, distinct from the outgoing optional quotation."""
    if (not isinstance(payload.get('wake_mode'), str) or payload['wake_mode'] not in _AUDIT_WAKE_MODES
            or payload.get('internal_review') or payload.get('is_proactive') or payload.get('cold_topic_request')
            or str(payload.get('trigger_id', '')).startswith('operator-')):
        return set()
    window = payload.get('listening_window')
    window = window if isinstance(window, dict) else {}
    ids = (payload.get('pending_explicit_question_ids') or window.get('selected_message_ids')
           or [payload.get('reply_target_message_id')])
    session = payload.get('session_key')
    if not isinstance(ids, (list, tuple)):
        return set()
    return {f'{session}/{mid}' for mid in ids if session and re.fullmatch(r'-?\d{1,20}', str(mid))}


class CoordinatorError(RuntimeError):
    pass


class TurnCoordinator:
    def __init__(self, base_dir, *, grace_seconds=90, primary_timeout_seconds=180,
                 fallback_timeout_seconds=120, ttl_seconds=600, clock=time.time,
                 confirmation_seconds=30, primary_max_seconds=420,
                 lock_timeout_seconds=10):
        self.base_dir = Path(base_dir).resolve()
        self.state_dir = self.base_dir / "state"
        from hakimi.bridge.turn_lanes import lane_id
        self.lane_id = lane_id()
        self.root_state_path = self.state_dir / "turn_state.json"
        self.state_path = self.root_state_path if not self.lane_id else self.state_dir / 'turn_lanes' / (self.lane_id+'.json')
        self.lock_path = self.state_dir / "turn_state.lock"
        self.outbox_path = self.state_dir / "outbox.jsonl"
        self.events_path = self.state_dir / "turn_events.jsonl"
        self.clock = clock
        self.grace_seconds = self._positive(grace_seconds)
        self.primary_timeout_seconds = self._positive(primary_timeout_seconds)
        self.confirmation_seconds = self._positive(confirmation_seconds)
        self.primary_max_seconds = self._positive(primary_max_seconds)
        self.fallback_timeout_seconds = self._positive(fallback_timeout_seconds)
        self.ttl_seconds = self._positive(ttl_seconds)
        self.lock_timeout_seconds = self._positive(lock_timeout_seconds)
        self.group_quota = GroupReplyQuota(clock=self.clock)

    @staticmethod
    def lane_ids():
        from hakimi.bridge.turn_lanes import LANES
        return LANES

    def for_lane(self, lane):
        from copy import copy
        if lane not in self.lane_ids():raise ValueError('unknown reply lane')
        result = copy(self)
        result.lane_id = lane
        result.state_path = result.root_state_path if not lane else result.state_dir/'turn_lanes'/(lane+'.json')
        return result

    def snapshots(self):
        return [self.for_lane(lane).snapshot() for lane in self.lane_ids()]

    def any_lane_active(self):
        from hakimi.bridge.turn_lanes import ACTIVE
        return any(state.get('status') in ACTIVE for state in self.snapshots())

    def was_superseded(self, trigger_id):
        if not trigger_id:
            return False
        hint = self._poll_hint()
        if hint.get('trigger_id') == trigger_id and hint.get('outcome') == 'superseded':
            return True
        from hakimi.core.jsonio import tail_jsonl
        from hakimi.core.runtime_io import read_derived_cached
        rows = read_derived_cached(self.events_path, lambda p: tail_jsonl(p, count=1000, max_bytes=524288),
                                   'superseded-turns')
        return any(r.get('trigger_id') == trigger_id and r.get('outcome') == 'superseded' for r in rows)

    def available_lane(self, payload):
        from hakimi.bridge.turn_lanes import ACTIVE, explicit_at
        preferred = payload.get('conversation_lane', '')
        if preferred not in self.lane_ids():return None
        states = {lane: self.for_lane(lane)._poll_hint() for lane in self.lane_ids()}
        if explicit_at(payload):
            return next((lane for lane in self.lane_ids() if states[lane].get('status') not in ACTIVE), None)
        if str(payload.get('wake_mode', '')) in ('被@后监听窗口', '相关话题续聊'):
            # Continuations reuse the lane assigned to their preceding reply.
            if states[preferred].get('status') not in ACTIVE:
                return preferred
            return None
        return '' if not any(state.get('status') in ACTIVE for state in states.values()) else None

    @staticmethod
    def _positive(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("coordinator durations must be finite positive numbers")
        return float(value)

    @contextmanager
    def _locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            if self.lock_path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            started = time.monotonic()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() - started >= self.lock_timeout_seconds:
                        raise CoordinatorError("turn lock busy; retry without submitting an uncoordinated reply")
                    time.sleep(0.02)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read(self):
        if not self.state_path.exists():
            state = {"version": 1, "status": "idle", "recent_trigger_ids": []}
            if self.lane_id and self.root_state_path.exists():
                root = json.loads(self.root_state_path.read_text(encoding='utf-8'))
                for key in (GROUP_REPLY_HISTORY, user_reply_quota.HISTORY_KEY, 'recent_trigger_ids'):
                    if key in root:state[key] = root[key]
            return state
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("version") != 1:
                raise ValueError("unsupported state")
            if state.get("status") not in {"idle", "queued", "primary_active", "fallback_active", "completed"}:
                raise ValueError("unknown turn state")
            if self.lane_id and self.root_state_path.exists():
                root = json.loads(self.root_state_path.read_text(encoding='utf-8'))
                for key in (GROUP_REPLY_HISTORY, user_reply_quota.HISTORY_KEY, 'recent_trigger_ids'):
                    if key in root:state[key] = root[key]
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise CoordinatorError("turn checkpoint unreadable; manual review required") from exc

    def _save(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lane_id:
            root = self.for_lane('')
            shared = root._read()
            for key in (GROUP_REPLY_HISTORY, user_reply_quota.HISTORY_KEY, 'recent_trigger_ids'):
                if key in state:shared[key] = state[key]
            root._save(shared)
        temporary = self.state_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temporary,self.state_path)

    def _read_turn_events_bounded(self):
        """Read only the bounded tail used for first-run quota migration."""
        try:
            with self.events_path.open("rb") as stream:
                stream.seek(0, 2)
                end = stream.tell()
                start = max(0, end - MAX_TURN_EVENTS_BYTES)
                boundary = True
                if start:
                    stream.seek(start - 1)
                    boundary = stream.read(1) == b"\n"
                stream.seek(start)
                raw = stream.read(end - start)
        except FileNotFoundError:
            return []
        if not boundary:
            raw = raw.partition(b"\n")[2]
        if raw and not raw.endswith(b"\n"):
            raw = raw.rpartition(b"\n")[0]
        records = []
        for line in raw.splitlines()[-MAX_TURN_EVENTS:]:
            if len(line) > MAX_TURN_EVENT_BYTES:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    records.append(event)
            except (ValueError, UnicodeError):
                continue
        return records

    def _ensure_group_history(self, state):
        """Upgrade an old checkpoint once, then keep the durable history pruned."""
        now = self.clock()
        if GROUP_REPLY_HISTORY not in state:
            state[GROUP_REPLY_HISTORY] = self.group_quota.initialize(
                self._read_turn_events_bounded(), state.get("pending_turn_events", []), now)
            self._save(state)
            return
        history = state.get(GROUP_REPLY_HISTORY)
        if not isinstance(history, (list, dict)):
            state[GROUP_REPLY_HISTORY] = self.group_quota.initialize(
                self._read_turn_events_bounded(), state.get("pending_turn_events", []), now)
            self._save(state)
            return
        pruned = self.group_quota.prune(history, now)
        if pruned != history:
            state[GROUP_REPLY_HISTORY] = pruned
            self._save(state)

    def _ensure_user_history(self, state):
        now = self.clock()
        history = state.get(user_reply_quota.HISTORY_KEY)
        if not isinstance(history, list) or any(isinstance(row, dict) and row.get('scope') != 'group' for row in history):
            # Upgrade using bounded existing outbox metadata, never chat contents.
            history = user_reply_quota.prune(history, now)
            private_turns = set()
            try:
                with self.outbox_path.open('rb') as stream:
                    stream.seek(0, os.SEEK_END)
                    start = max(0, stream.tell() - 4*1024*1024)
                    stream.seek(start)
                    raw = stream.read()
                if start:
                    raw = raw.partition(b'\n')[2]
                for line in raw.splitlines():
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                        interaction = row.get('interaction')
                        user = row.get('reply_user_id') or (interaction.get('user_id') if isinstance(interaction, dict) else None)
                        trigger = row.get('trigger_id')
                        if not isinstance(user, str) or not isinstance(trigger, str):
                            continue
                        if user_reply_quota.is_private_session(row.get('session_key')):
                            private_turns.add((user, trigger))
                            continue
                        history.append({'user_id': user, 'trigger_id': trigger,
                                        'completed_at': row.get('created_at')})
                    except (ValueError, UnicodeError):
                        continue
            except FileNotFoundError:
                pass
            history = [row for row in history if (row.get('user_id'), row.get('trigger_id')) not in private_turns]
        history = user_reply_quota.prune(history, now)
        if history != state.get(user_reply_quota.HISTORY_KEY):
            state[user_reply_quota.HISTORY_KEY] = history
            self._save(state)

    def _user_reply_wait(self, state, payload=None, reply_to=None):
        payload = state.get('payload', {}) if payload is None else payload
        if user_reply_quota.is_private_session(payload.get('session_key')):
            return 0
        user = user_reply_quota.recipient(payload, reply_to)
        anchors = {str(mid) for mid in payload.get('pending_explicit_question_ids', ())}
        if user and anchors:
            from hakimi.core.runtime_io import read_json_cached
            owner = read_json_cached(self.state_dir / 'feeding.json', {}, readonly=True).get('owner_user_id')
            if user == owner and any(str(row.get('message_id')) in anchors and
                                    str(row.get('user_id')) == owner
                                    for row in payload.get('recent_context_messages', ())):
                return 0
        return user_reply_quota.retry_after(state.get(user_reply_quota.HISTORY_KEY, []), user, self.clock())

    def _group_reply_limited(self, state, now=None):
        payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
        if isinstance(payload.get("internal_review"), dict):
            return False
        session = payload.get("session_key")
        if not is_group_session(session):
            return False
        return self.group_quota.at_limit(state.get(GROUP_REPLY_HISTORY, []), session, now)

    def _queued_group_reply_limited(self, state, now=None):
        return state.get("status") == "queued" and self._group_reply_limited(state, now)

    def _record_group_reply(self, state):
        """Add only a committed, non-review Gemini group reply; trigger-idempotent."""
        payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
        if state.get("provider") != "gemini" or state.get("outcome") != "replied":
            return
        if isinstance(payload.get("internal_review"), dict):
            return
        session = payload.get("session_key")
        if not is_group_session(session):
            return
        now = self.clock()
        state[GROUP_REPLY_HISTORY] = self.group_quota.record(
            state.get(GROUP_REPLY_HISTORY, []), session, state.get("trigger_id"),
            state.get("reply_committed_at", state.get("completed_at")), now=now)

    @staticmethod
    def _record_bytes(record):
        return (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

    def _append_bytes(self, data):
        with self.outbox_path.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _safe_epoch(value):
        try:
            return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        except (ValueError, OverflowError):
            return None

    @classmethod
    def _audit_record(cls, record):
        """Rebuild from a fixed schema; never copy free text, tokens or replies."""
        trigger = str(record.get("trigger_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", trigger):
            trigger = "hashed_" + hashlib.sha256(trigger.encode("utf-8")).hexdigest()
        session = record.get("session_key")
        reason = record.get("takeover_reason")
        if reason is not None and (not isinstance(reason, str) or reason not in _AUDIT_TAKEOVERS):
            reason = "reported:other" if isinstance(reason, str) and reason.startswith("reported:") else "other"
        wake_mode, outcome = record.get("wake_mode"), record.get("outcome")
        return {
            "version": 1, "trigger_id": trigger,
            "session_key": session if isinstance(session, str) and re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}", session) else "",
            "wake_mode": wake_mode if isinstance(wake_mode, str) and wake_mode in _AUDIT_WAKE_MODES else "other",
            "provider": record.get("provider") if record.get("provider") in ("gemini", "dsh") else None,
            "outcome": outcome if isinstance(outcome, str) and outcome in _AUDIT_OUTCOMES else "other",
            "created_at": cls._safe_epoch(record.get("created_at")),
            "claimed_at": cls._safe_epoch(record.get("claimed_at")),
            "completed_at": cls._safe_epoch(record.get("completed_at")),
            "takeover_reason": reason,
        }

    @classmethod
    def _bounded_events(cls, records):
        retained, seen, size = [], set(), 0
        for record in reversed(records):
            if not isinstance(record, dict):
                continue
            event = cls._audit_record(record)
            raw_size = len(cls._record_bytes(event))
            if event["trigger_id"] in seen or raw_size > MAX_TURN_EVENT_BYTES:
                continue
            if len(retained) >= MAX_TURN_EVENTS or size + raw_size > MAX_TURN_EVENTS_BYTES:
                break
            retained.append(event)
            seen.add(event["trigger_id"])
            size += raw_size
        return list(reversed(retained))

    def _queue_turn_event(self, state):
        if state.get("audit_event_recorded"):
            return False
        payload = state.get("payload", {})
        event = self._audit_record({**{key: state.get(key) for key in
            ("trigger_id", "provider", "outcome", "created_at", "claimed_at", "completed_at", "takeover_reason")},
            "session_key": payload.get("session_key"), "wake_mode": payload.get("wake_mode")})
        pending = state.get("pending_turn_events", [])
        pending = [item for item in pending if isinstance(item, dict)] if isinstance(pending, list) else []
        if any(item.get("trigger_id") == event["trigger_id"] for item in pending):
            return False
        state["pending_turn_events"] = self._bounded_events(pending + [event])
        return True

    def _write_turn_events(self, pending):
        """Atomic bounded rotation, called only while the coordinator lock is held."""
        if self.events_path.resolve() != self.events_path.absolute() or self.events_path.is_symlink():
            raise OSError("redirected audit path")
        try:
            with self.events_path.open("rb") as stream:
                stream.seek(0, 2)
                end = stream.tell()
                start = max(0, end - MAX_TURN_EVENTS_BYTES)
                boundary = True
                if start:
                    stream.seek(start - 1)
                    boundary = stream.read(1) == b"\n"
                stream.seek(start)
                raw = stream.read(end - start)
        except FileNotFoundError:
            raw, boundary = b"", True
        if not boundary:
            raw = raw.partition(b"\n")[2]
        if raw and not raw.endswith(b"\n"):
            raw = raw.rpartition(b"\n")[0]
        existing = []
        for line in raw.splitlines()[-MAX_TURN_EVENTS:]:
            if len(line) > MAX_TURN_EVENT_BYTES:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    existing.append(event)
            except (ValueError, UnicodeError):
                continue
        seen = {event.get("trigger_id") for event in existing if isinstance(event.get("trigger_id"), str)}
        events = self._bounded_events(existing + [event for event in pending if event["trigger_id"] not in seen])
        temporary = self.events_path.with_suffix(".jsonl.tmp")
        with temporary.open("wb") as stream:
            for event in events:
                stream.write(self._record_bytes(event))
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(temporary,self.events_path)

    def _flush_turn_events(self, state):
        pending = state.get("pending_turn_events", [])
        if not pending:
            return
        try:
            self._write_turn_events(pending)
            state["pending_turn_events"] = []
            if state.get("status") == "completed":
                state["audit_event_recorded"] = True
            self._save(state)
        except Exception:
            # The completed state already durably contains pending metadata.
            # Retry after restart / before the next publish; never retry a reply.
            pass

    def _finish(self, state, outcome, reply_id=None, *, committed=False):
        outcome = outcome if isinstance(outcome, str) and outcome in _AUDIT_OUTCOMES else "other"
        state.update(status="completed", outcome=outcome, completed_at=self.clock())
        if reply_id:
            state["reply_id"] = reply_id
        if committed and outcome == "replied":
            # Save the quota entry atomically with the completion/audit state.
            self._record_group_reply(state)
        state.pop("reply_intent", None)
        state.pop('selected_mention', None)
        try:
            self._queue_turn_event(state)
        except Exception:
            pass  # Audit preparation must not turn a committed reply into a retry.
        self._save(state)
        self._flush_turn_events(state)
        payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
        # Internal reflection is not a real user turn; it must not mark the
        # primary provider healthy/failed or poison future fallback decisions.
        if (state.get("provider") == "gemini" and outcome in {"replied", "no_reply"}
                and not payload.get("internal_review")):
            try:ProviderHealth(self.base_dir,self.clock).primary_completed()
            except (OSError,sqlite3.Error):pass

    def enable_post_reply(self, token):
        """Opt the current owner into bounded work after its one reply."""
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token) or state.get("reply_id"):
                return False
            state["post_reply_enabled"] = True
            self._save(state)
            return True

    def _reply_committed(self, state, reply_id):
        record = state.get('reply_intent', {}).get('record', {})
        payload = state['payload']
        from hakimi.models.web_sources import commit as commit_web_sources
        commit_web_sources(self.base_dir, record)
        sources = _reply_sources(payload)
        if sources:
            # Under the shared coordinator lock, after the outbox append. A
            # silent, failed or still-rendering turn cannot consume an input.
            history = state.get('answered_sources', [])
            state['answered_sources'] = [key for key in history if key not in sources][-256:] + sorted(sources)
            visible = set()
            if str(payload.get('wake_mode', '')).startswith('即时唤醒'):
                visible = {f"{payload['session_key']}/{r.get('message_id')}" for r in payload.get('recent_context_messages', [])
                           if r.get('user_id') != 'SELF' and r.get('session_key', payload['session_key']) == payload['session_key']}
            for lane in self.lane_ids():
                if lane == self.lane_id:
                    continue
                other = self.for_lane(lane)
                pending = other._poll_hint()
                if (pending.get('status') not in ('queued', 'primary_active', 'fallback_active')
                        or pending.get('reply_id') or pending.get('reply_intent')):
                    continue
                old = pending['payload']
                old_sources = _reply_sources(old)
                background = old.get('wake_mode') in ('被@后监听窗口', '相关话题续聊', '上下文积累判断')
                if old_sources and (old_sources <= sources or
                        (background and old_sources <= visible and pending['created_at'] <= state['created_at'])):
                    other._finish(other._read(), 'superseded')
        if not user_reply_quota.is_private_session(record.get('session_key') or state.get('payload', {}).get('session_key')):
            user = record.get('reply_user_id') or user_reply_quota.recipient(state.get('payload', {}), record.get('reply_target_id'))
            state[user_reply_quota.HISTORY_KEY] = user_reply_quota.record(
                state.get(user_reply_quota.HISTORY_KEY, []), user, state.get('trigger_id'), self.clock())
        if not state.get("post_reply_enabled"):
            self._finish(state, "replied", reply_id, committed=True)
            return
        state.update(reply_id=reply_id, reply_committed_at=self.clock(), outcome="replied")
        state.pop("reply_intent", None)
        self._record_group_reply(state)
        self._save(state)

    def _reconcile(self, state):
        """Recover a commit intent before accepting any new owner or turn.

        The offset plus canonical bytes distinguish a missing append from a
        partially written record. Unexpected bytes are never removed.
        """
        intent = state.get("reply_intent")
        if not intent:
            return
        raw = self._record_bytes(intent["record"])
        offset = intent["start_offset"]
        size = self.outbox_path.stat().st_size if self.outbox_path.exists() else 0
        if size < offset:
            raise CoordinatorError("outbox shrank during a reply commit; manual review required")
        tail = b""
        if size > offset:
            with self.outbox_path.open("rb") as stream:
                stream.seek(offset)
                tail = stream.read()
        if tail.startswith(raw):
            self._reply_committed(state, intent["record"]["reply_id"])
            return
        if len(tail) < len(raw) and raw.startswith(tail):
            self._append_bytes(raw[len(tail):])
            self._reply_committed(state, intent["record"]["reply_id"])
            return
        # Another (older) writer may have appended complete records first.
        # Reconcile our ID before considering another append at the new EOF.
        if tail and not tail.endswith(b"\n"):
            raise CoordinatorError("outbox commit conflicts with a partial external record")
        for line in tail.splitlines():
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise CoordinatorError("outbox commit conflicts with malformed external data") from exc
            if not isinstance(record, dict):
                raise CoordinatorError("outbox commit conflicts with a non-object record")
            if record.get("reply_id") == intent["record"]["reply_id"]:
                if record != intent["record"]:
                    raise CoordinatorError("conflicting reply ID requires manual review")
                self._reply_committed(state, record["reply_id"])
                return
        intent["start_offset"] = size
        self._save(state)
        self._append_bytes(raw)
        self._reply_committed(state, intent["record"]["reply_id"])

    def _load_ready(self):
        state = self._read()
        self._ensure_group_history(state)
        self._ensure_user_history(state)
        self._reconcile(state)
        if state["status"] in {"queued", "primary_active", "fallback_active"}:
            now = self.clock()
            if now >= state["expires_at"]:
                self._finish(state, "replied" if state.get("reply_id") else "expired")
            elif state.get("reply_id") and now >= state["deadline"]:
                self._finish(state, "replied")
            elif state["status"] == "fallback_active" and now >= state["deadline"]:
                self._finish(state, "fallback_timeout")
        try:
            if state.get("status") == "completed" and self._queue_turn_event(state):
                self._save(state)
        except Exception:
            pass
        self._flush_turn_events(state)
        return state

    @staticmethod
    def _provider(provider):
        if not isinstance(provider, str):
            return None
        return {"primary": "gemini", "fallback": "dsh"}.get(provider, provider)

    def _matches(self, state, token, provider=None, enforce_deadline=True):
        if state["status"] not in {"primary_active", "fallback_active"}:
            return False
        if not token or token != state.get("owner_token"):
            return False
        if provider is not None and self._provider(provider) != state.get("provider"):
            return False
        if state.get("failure"):
            return False
        return not enforce_deadline or self.clock() < state["deadline"]

    def snapshot(self):
        hint = self._poll_hint()
        if not self._needs_recovery(hint):
            # Observation only. All ownership/commit methods still lock and reread.
            return deepcopy(hint)
        with self._locked():
            return self._load_ready()

    def _poll_hint(self):
        try:
            hint = read_json_cached(self.state_path,
                {"version": 1, "status": "idle", "recent_trigger_ids": []}, readonly=True)
            if (not isinstance(hint, dict) or hint.get("version") != 1
                    or hint.get("status") not in {"idle", "queued", "primary_active", "fallback_active", "completed"}):
                raise ValueError('invalid state')
            return hint
        except (OSError, ValueError, TypeError) as exc:
            raise CoordinatorError('turn checkpoint unreadable; manual review required') from exc

    def _needs_recovery(self, state):
        if state.get('reply_intent') or state.get('pending_turn_events'):
            return True
        if user_reply_quota.HISTORY_KEY not in state:
            return self.state_path.exists()
        if GROUP_REPLY_HISTORY not in state:
            # A missing file is a fresh lane, not an old checkpoint to upgrade.
            return self.state_path.exists()
        if self.group_quota.needs_prune(state.get(GROUP_REPLY_HISTORY), self.clock()):
            return True
        if state['status'] == 'completed' and not state.get('audit_event_recorded'):
            return True
        if state['status'] in {'queued', 'primary_active', 'fallback_active'}:
            now = self.clock()
            return now >= state.get('expires_at', 0) or (
                (state['status'] == 'fallback_active' or state.get('reply_id')) and now >= state.get('deadline', 0))
        return False

    def _primary_timing(self):
        """Learn only successful primary latency metadata; never inspect chat text.

        Store the result in the turn so differently configured CLI processes use
        the same deadlines. Recent slow successes widen patience, never shorten it.
        """
        samples, waits = [], []
        try:
            with self.events_path.open("rb") as stream:
                stream.seek(0, 2)
                start = max(0, stream.tell() - MAX_TURN_EVENTS_BYTES)
                stream.seek(start)
                raw = stream.read(MAX_TURN_EVENTS_BYTES)
            if start:
                raw = raw.partition(b"\n")[2]
            for line in raw.splitlines()[-100:]:
                if len(line) > MAX_TURN_EVENT_BYTES:
                    continue
                try:
                    event = json.loads(line)
                    if (not isinstance(event, dict) or event.get("provider") != "gemini"
                            or event.get("outcome") not in {"replied", "no_reply"}):
                        continue
                    created, claimed, completed = (self._safe_epoch(event.get(key))
                        for key in ("created_at", "claimed_at", "completed_at"))
                    if (None in (created, claimed, completed) or not created <= claimed <= completed <= self.clock()
                            or self.clock() - completed > 86400):
                        continue
                    if 0 < completed - claimed <= 600:
                        samples.append(completed - claimed)
                    if 0 <= claimed - created <= 600:
                        waits.append(claimed - created)
                except (ValueError, TypeError):
                    continue
        except OSError:
            pass
        def p90(values):
            values = sorted(values[-30:])
            return values[math.ceil(len(values) * .9) - 1] if len(values) >= 3 else 0
        # Whole-turn latency includes tool execution, so it must not turn into
        # several minutes of patience for an entirely inactive request.
        latency, queue_wait = p90(samples), p90(waits)
        idle = min(110, max(min(self.primary_timeout_seconds, 80), latency * .5 + 15 if latency else 0))
        grace = min(60, max(min(self.grace_seconds, 40), queue_wait * .3 + 10 if queue_wait else 0))
        health = availability(self.base_dir,self.clock)
        confirmation = min(self.confirmation_seconds, 10)
        hard = max(idle + confirmation, self.primary_max_seconds)
        if health["fast"] and health.get("mode","auto")!="gemini":
            grace = max(3,min(8,grace*.06))
            idle = max(10,min(18,idle*.07))
            confirmation = max(1,min(3,confirmation*.07))
        return {"idle_seconds": idle, "grace_seconds": grace,
                "confirmation_seconds": confirmation,"hard_seconds":hard,
                "fast_fallback":health["fast"],"availability_epoch":health["epoch"]}

    def _refresh_queued_grace(self, state, policy):
        ready_at = self._safe_epoch(state.get("ready_at"))
        deadline = state["primary_grace_deadline"]
        expires_at = state["expires_at"]
        if ready_at is None:
            # Older queued records have no publication time. Recover it from
            # their saved grace only when that deadline was not clipped by TTL.
            prior_grace = self._safe_epoch((state.get("primary_policy") or {}).get("grace_seconds"))
            if prior_grace is None or deadline >= expires_at:
                return
            ready_at = deadline - prior_grace
        if state["created_at"] <= ready_at <= self.clock():
            state["ready_at"] = ready_at
            state["primary_grace_deadline"] = min(ready_at + policy["grace_seconds"], expires_at)

    def publish(self, payload):
        if not isinstance(payload, dict):raise ValueError('turn payload must be an object')
        lane = self.available_lane(payload)
        if lane is None:return {'accepted': False, 'reason': 'busy', 'status': 'busy'}
        target = self.for_lane(lane)
        result = target._publish_current(dict(payload, conversation_lane=lane))
        if result.get('accepted'):result['conversation_lane'] = lane
        return result

    def _publish_current(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("turn payload must be an object")
        payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        with self._locked():
            state = self._load_ready()
            if state["status"] not in {"idle", "completed"}:
                return {"accepted": False, "status": state["status"], "reason": "busy", "trigger_id": state["trigger_id"]}
            trigger_id = str(payload.get("trigger_id") or uuid.uuid4().hex)
            recent = state.get("recent_trigger_ids", [])
            if trigger_id in recent:
                return {"accepted": False, "status": state["status"], "reason": "duplicate_trigger", "trigger_id": trigger_id}
            sources = _reply_sources(payload)
            if sources:
                answered = set()
                for lane in self.lane_ids():
                    peer = self.for_lane(lane)
                    prior = state if lane == self.lane_id else peer._poll_hint()
                    if prior.get('reply_intent'):
                        prior = peer._load_ready()
                    answered.update(prior.get('answered_sources', []))
                    prior_sources = _reply_sources(prior.get('payload', {}))
                    if prior.get('reply_id'):
                        answered.update(prior_sources)
                    elif prior.get('status') in ('queued', 'primary_active', 'fallback_active') and sources <= prior_sources:
                        return {'accepted': False, 'reason': 'busy'}
                if sources <= answered:
                    from hakimi.bridge.message_lifecycle import record
                    record(self.base_dir, payload, 'cancelled', reason='source_already_answered')
                    return {'accepted': False, 'reason': 'source_already_answered'}
            now = self.clock()
            created_at = payload.get("created_at", now)
            if (not isinstance(created_at, (int, float)) or isinstance(created_at, bool)
                    or not math.isfinite(created_at)):
                raise ValueError("created_at must be a finite epoch timestamp")
            created_at = min(created_at, now)
            internal_review = isinstance(payload.get("internal_review"), dict)
            ttl_seconds = min(self.ttl_seconds, INTERNAL_REVIEW_TTL_SECONDS) if internal_review else self.ttl_seconds
            if now >= created_at + ttl_seconds:
                return {"accepted": False, "status": "expired", "reason": "expired", "trigger_id": trigger_id}
            wait = self._user_reply_wait(state, payload)
            if wait:
                from hakimi.bridge.message_lifecycle import record
                record(self.base_dir,payload,'cancelled',reason='user_reply_limit',retry_after=wait)
                return {'accepted': False, 'status': state['status'],
                        'reason': user_reply_quota.LIMIT_REASON, 'retry_after': wait,
                        'trigger_id': trigger_id}
            payload.update(trigger_id=trigger_id, created_at=created_at)
            policy = self._primary_timing()
            pending_events = state.get("pending_turn_events", [])
            group_history = self.group_quota.prune(
                state.get(GROUP_REPLY_HISTORY, []), now)
            state = {"version": 1, "status": "queued", "trigger_id": trigger_id, "payload": payload,
                     "created_at": created_at, "ready_at": now, "expires_at": created_at + ttl_seconds,
                     "original_expires_at": created_at + ttl_seconds,
                     "primary_grace_deadline": min(now + policy["grace_seconds"], created_at + ttl_seconds),
                     "primary_policy": policy,
                     "recent_trigger_ids": (recent + [trigger_id])[-1000:],
                     "answered_sources": state.get('answered_sources', []),
                     "pending_turn_events": pending_events,
                     GROUP_REPLY_HISTORY: group_history,
                     user_reply_quota.HISTORY_KEY: state.get(user_reply_quota.HISTORY_KEY, [])}
            self._save(state)
            from hakimi.bridge.message_lifecycle import record
            record(self.base_dir,payload,'published',trigger_id=trigger_id)
            return {"accepted": True, "status": "queued", "trigger_id": trigger_id}

    def _original_expiry(self, state):
        original = self._safe_epoch(state.get("original_expires_at"))
        if original is not None:
            return original
        # The configured TTL recovers old Carbon rows whose rolling lease
        # already grew. Keep any shorter deadline already saved on the row.
        return min(state["expires_at"], state["created_at"] + self.ttl_seconds)

    def _claim_result(self, state):
        return {"payload": state["payload"], "token": state["owner_token"], "trigger_id": state["trigger_id"],
                "provider": state["provider"], "deadline": state["deadline"],
                "expires_at": self._original_expiry(state), "takeover_reason": state.get("takeover_reason")}

    def claim_primary(self, trigger_id=None):
        hint = self._poll_hint()
        if hint['status'] != 'queued' and not self._needs_recovery(hint):
            return None
        health = availability(self.base_dir,self.clock)
        if health.get("mode")=="dsh":return None
        with self._locked():
            state = self._load_ready()
            if state["status"] != "queued" or (trigger_id is not None and trigger_id != state["trigger_id"]):
                return None
            now = self.clock()
            health = availability(self.base_dir,self.clock)
            if health.get("mode")=="dsh":return None
            if self._user_reply_wait(state):
                self._finish(state, 'no_reply')
                return None
            if self._group_reply_limited(state, now):return None
            policy = state.get("primary_policy") or self._primary_timing()
            if health.get("mode")=="gemini" or policy.get("availability_epoch")!=health["epoch"]:
                policy = self._primary_timing()
            payload = state.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("internal_review"), dict):
                # Internal reflection is Gemini-only and never has a fallback
                # request, so its hard deadline must not reserve fallback time.
                hard_deadline = min(now + 180, state["expires_at"])
            else:
                # Preserve the normal fallback reserve. When a late group turn
                # has less than two such windows left, split the remainder from
                # the actual claim so waiting for a social batch cannot use it up.
                expires_at = state["expires_at"]
                remaining = expires_at - now
                if (is_group_session((payload or {}).get("session_key"))
                        and now - state["created_at"] >= policy["grace_seconds"]
                        and remaining < 2 * self.fallback_timeout_seconds):
                    primary_until = now + remaining / 2
                else:
                    primary_until = max(now + 1, expires_at - self.fallback_timeout_seconds)
                hard_deadline = min(now + policy["hard_seconds"], primary_until, expires_at)
            state.pop("takeover_suspected_at", None)
            state.update(status="primary_active", provider="gemini", owner_token=uuid.uuid4().hex,
                         claimed_at=now, deadline=hard_deadline, primary_policy=policy,
                         primary_last_progress_at=now,
                         primary_idle_deadline=min(now + policy["idle_seconds"], hard_deadline))
            self._save(state)
            return self._claim_result(state)

    def claim_fallback(self, *, private_only=False):
        hint = self._poll_hint()
        if private_only and not user_reply_quota.is_private_session((hint.get('payload') or {}).get('session_key')):
            return None
        # Internal memory reflection is Gemini-only.  A failed/expired review
        # must expire silently; DSH must never turn it into a QQ reply.
        if isinstance(hint.get("payload"), dict) and hint["payload"].get("internal_review"):
            return None
        now = self.clock()
        if hint.get('status') == 'queued' and self._user_reply_wait(hint):
            with self._locked():
                state = self._load_ready()
                if state.get('status') == 'queued' and self._user_reply_wait(state):
                    self._finish(state, 'no_reply')
            return None
        quota_hint = self._queued_group_reply_limited(hint, now)
        health = availability(self.base_dir,self.clock)
        # A group that already used its rolling Gemini replies may switch this
        # queued turn to dsh even under the operator's forced-Gemini mode.
        if health.get("mode")=="gemini" and not quota_hint and not self._needs_recovery(hint):
            return None
        if hint["status"] in {"queued","primary_active"}:
            if hint.get("primary_policy",{}).get("availability_epoch") != health["epoch"]:
                with self._locked():
                    current = self._load_ready()
                    if current["status"] in {"queued","primary_active"}:
                        policy = self._primary_timing()
                        if current["status"] == "queued":
                            self._refresh_queued_grace(current, policy)
                        else:
                            current["primary_idle_deadline"] = min(current.get("primary_last_progress_at",current["claimed_at"])+policy["idle_seconds"],current["deadline"])
                        current["primary_policy"] = policy
                        current.pop("takeover_suspected_at",None)
                        self._save(current)
                    hint = current
        now = self.clock()
        immediate = health.get("mode")=="dsh"
        quota_hint = self._queued_group_reply_limited(hint, now)
        due = (quota_hint
               or (immediate and hint['status'] in {'queued','primary_active'})
               or (hint['status'] == 'queued' and now >= hint.get('primary_grace_deadline', 0))
               or (hint['status'] == 'primary_active' and (hint.get('failure') or now >= min(
                   self._primary_wait_deadline(hint), hint.get('deadline', 0)))))
        if not due and not self._needs_recovery(hint):
            return None
        with self._locked():
            state = self._load_ready()
            if private_only and not user_reply_quota.is_private_session((state.get('payload') or {}).get('session_key')):
                return None
            if isinstance(state.get("payload"), dict) and state["payload"].get("internal_review"):
                return None
            now = self.clock()
            if state.get("reply_id"):
                if state.get("failure") or now >= self._primary_wait_deadline(state):
                    self._finish(state, "replied")
                return None
            if state.get('status') == 'queued' and self._user_reply_wait(state):
                self._finish(state, 'no_reply')
                return None
            # A queued group at its rolling Gemini ceiling is the only reason
            # allowed to bypass forced-Gemini mode.  An active owner is never
            # stolen by this quota path.
            quota_limited = self._queued_group_reply_limited(state, now)
            # Mode changes affect the next owner; an active dsh reply is never stolen.
            health = availability(self.base_dir,self.clock)
            if health.get("mode")=="gemini" and not quota_limited:
                return None
            if quota_limited:
                reason = GROUP_HOURLY_LIMIT_REASON
            elif state["status"] in {"queued","primary_active"} and health.get("mode")=="dsh":
                reason = "operator_mode_dsh"
            elif state["status"] == "primary_active" and state.get("failure"):
                reason = "reported:" + state["failure"]["reason"]
            elif state["status"] == "queued" and now >= state["primary_grace_deadline"]:
                reason = "primary_grace_expired"
            elif state["status"] == "primary_active" and now >= state["deadline"]:
                reason = "primary_hard_timeout" if state.get("primary_policy") else "primary_timeout"
            elif (state["status"] == "primary_active"
                  and now >= self._primary_wait_deadline(state)):
                reason = "primary_stalled"
            else:
                return None
            # A missing claim or stale activity is suspicion, not proof of quota
            # exhaustion. Give the real owner another chance to claim/progress.
            if reason in {"primary_grace_expired", "primary_stalled"}:
                confirmation = state.get("primary_policy", {}).get("confirmation_seconds", self.confirmation_seconds)
                if "takeover_suspected_at" not in state:
                    state["takeover_suspected_at"] = now
                    self._save(state)
                    return None
                if now < state["takeover_suspected_at"] + confirmation:
                    return None
            state.pop("failure", None)
            previous_primary_token = state.get("owner_token") if state.get("provider")=="gemini" else None
            state.pop('selected_mention', None)
            state.update(status="fallback_active", provider="dsh", owner_token=uuid.uuid4().hex,
                         claimed_at=now, takeover_reason=reason,
                         deadline=min(now + self.fallback_timeout_seconds, state["expires_at"]))
            self._save(state)
            if reason != GROUP_HOURLY_LIMIT_REASON:
                try:
                    ProviderHealth(self.base_dir,self.clock).takeover(state["trigger_id"],previous_primary_token,state["payload"].get("session_key"))
                except (OSError,sqlite3.Error):
                    pass
            return self._claim_result(state)

    def check_token(self, token, provider=None):
        with self._locked():
            return self._matches(self._load_ready(), token, provider)

    def drawing_deadline(self, token, provider, seconds):
        """Let an admitted fallback draw finish within the original turn lifetime."""
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token, provider) or state.get('reply_id'):
                return None
            if state['status'] == 'fallback_active':
                until = min(self.clock() + seconds, state['expires_at'], self._original_expiry(state))
                if until > state['deadline']:
                    state['deadline'] = until
                    self._save(state)
            return state['deadline']

    def select_mention(self, token, provider, uid):
        """Keep the tool's resolved recipient across model rounds and CLI processes."""
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token, provider):
                return {'accepted': False, 'reason': 'stale_owner'}
            if state.get('reply_id'):
                return {'accepted': False, 'reason': 'already_replied'}
            if not state['payload'].get('session_key', '').startswith('group:'):
                return {'accepted': False, 'reason': 'group_mention_only'}
            if uid is None:
                state.pop('selected_mention', None)
            elif isinstance(uid, str) and re.fullmatch(r'[1-9][0-9]{0,19}', uid):
                state['selected_mention'] = {'owner': token, 'uid': uid}
            else:
                return {'accepted': False, 'reason': 'invalid_mention_target'}
            self._save(state)
            return {'accepted': True}

    @staticmethod
    def _primary_wait_deadline(state):
        tool = state.get('primary_tool') or {}
        return min(state.get('deadline', 0), max(state.get('primary_idle_deadline', state.get('deadline', 0)), tool.get('deadline', 0), state.get('carbon_wait_until', 0)))

    def tool_activity(self, token, name=None, timeout=None):
        """Start/end a real synchronous tool call; never a periodic heartbeat."""
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token, 'gemini'):
                return False
            now = self.clock()
            if name is None:
                state.pop('primary_tool', None)
            else:
                until = state['deadline'] if timeout is None else min(state['deadline'], now + self._positive(timeout))
                state['primary_tool'] = {'name': str(name)[:80], 'started_at': now, 'deadline': until}
            state['primary_last_progress_at'] = now
            state['primary_idle_deadline'] = min(now + state['primary_policy']['idle_seconds'], state['deadline'])
            state.pop('takeover_suspected_at', None)
            self._save(state)
            return True

    def renew_carbon_wait(self, token, end, *, rolling=False, hard_expires_at=None):
        from hakimi.bridge.turn_lanes import active_context_path
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token, 'gemini'):
                return False
            now = self.clock()
            state.setdefault('original_expires_at', self._original_expiry(state))
            cap = state.get('carbon_hard_expires_at')
            if hard_expires_at is not None:
                limit = self._safe_epoch(hard_expires_at)
                if limit is None:
                    return False
                limit = min(limit, state['original_expires_at'], state['expires_at'])
                cap = limit if cap is None else min(limit, cap)
                state['carbon_hard_expires_at'] = cap
                state['expires_at'] = cap
                state['deadline'] = min(state['deadline'], cap)
                if now >= cap:
                    self._finish(state, 'expired')
                    return False
            if end is not None and now >= end:
                if hard_expires_at is not None:
                    self._save(state)
                return False
            until = end if rolling and end is not None else now + 120
            if end is not None:
                until = min(until, end)
            if cap is not None:
                until = min(until, cap)
            else:
                state['expires_at'] = max(state['expires_at'], until + self.fallback_timeout_seconds)
            state['carbon_wait_until'] = until
            state['deadline'] = until
            self._save(state)
            path = active_context_path(self.base_dir)
            if path.exists():
                context = json.loads(path.read_text())
                if context.get('owner_token') == token:
                    context.update(issued_at=now, expires_at=min(until, now + 600))
                    temp = path.with_suffix('.carbon.tmp')
                    temp.write_text(json.dumps(context, ensure_ascii=False))
                    temp.replace(path)
            return True

    def record_progress(self, token, *, wait_seconds=0):
        """Only genuine, token-authorized worker activity may renew the idle timer.

        Never call this from a background keepalive. It cannot renew the hard
        deadline, revive a failed task, or take ownership back from fallback.
        """
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token, "gemini"):
                return False
            now = self.clock()
            if wait_seconds or now - state.get("primary_last_progress_at", state["claimed_at"]) >= 10 or state.get("takeover_suspected_at") is not None:
                policy = state.get("primary_policy")
                if policy:
                    state["primary_last_progress_at"] = now
                    state["primary_idle_deadline"] = min(now + max(policy["idle_seconds"], wait_seconds), state["deadline"])
                    state.pop("takeover_suspected_at", None)
                    self._save(state)
            return True

    def report_failure(self, token, reason):
        reason = str(reason).strip()[:160]
        if not reason:
            raise ValueError("a failure reason is required")
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token, enforce_deadline=False):
                return False
            if state.get("reply_id"):
                self._finish(state, "replied")
                return True
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            if payload.get("internal_review"):
                # A failed reflection completes silently and immediately; it is
                # neither a user reply nor evidence that the primary is down.
                self._finish(state, "no_reply")
                return True
            from hakimi.bridge.message_lifecycle import record
            record(self.base_dir,payload,'generation_error',trigger_id=state.get('trigger_id'),reason=reason)
            state["failure"] = {"provider": state["provider"], "reason": reason, "at": self.clock()}
            if state["status"] == "fallback_active":
                self._finish(state, "fallback_failed")
            else:
                self._save(state)
            return True

    def complete(self, token, outcome="no_reply"):
        with self._locked():
            state = self._load_ready()
            if not self._matches(state, token):
                return False
            self._finish(state, "replied" if state.get("reply_id") else str(outcome)[:80])
            return True

    def submit_reply(self, token, segments, provider, files=None, reply_to=None, rendered_math=False, interaction=None, *, mention_user=None, generated_drawing=False):
        from hakimi.bridge.interaction_policy import blocked_outgoing_topic
        if isinstance(segments,list) and all(isinstance(text,str) for text in segments) and blocked_outgoing_topic(''.join(segments)):
            return {'accepted':False,'reason':'ignored_topic'}
        if (not isinstance(segments, list) or (not rendered_math and len(segments) > MAX_REPLY_SEGMENTS)
                or (not segments and not files)
                or any(not isinstance(text, str) or not text.strip() or len(text) > MAX_SEGMENT_CHARS for text in segments)):
            return {"accepted": False, "reason": "invalid_segments"}
        from hakimi.bridge.mention_segments import attach_recipient, leading_local_id, UNRESOLVED
        typed_uid = leading_local_id(segments)
        typed_member = False
        if typed_uid and mention_user is None:
            state = self.snapshot()
            if self._matches(state, token, provider) and state['payload'].get('session_key', '').startswith('group:'):
                from hakimi.transport.member_mentions import known_local_member
                typed_member = known_local_member(self.base_dir, state['payload'], typed_uid)
        if not files and not (rendered_math or generated_drawing):
            state=self.snapshot()
            if self._matches(state,token,provider) and not state.get('reply_id'):
                from hakimi.features.diary_store import requested_export,submit_export
                spec=requested_export(state['payload'],self.base_dir)
                if spec is not None:
                    return submit_export(self,token,provider,spec,self.base_dir,segments[:1])
        with self._locked():
            # Finish any interrupted peer append before deciding which source
            # is still ours. All lanes use this same lock and outbox.
            for lane in self.lane_ids():
                if lane != self.lane_id:
                    peer = self.for_lane(lane)
                    if peer._poll_hint().get('reply_intent'):
                        peer._load_ready()
            state = self._load_ready()
            if not self._matches(state, token, provider):
                if self._provider(provider)=="gemini":
                    try:ProviderHealth(self.base_dir,self.clock).late_reply(token)
                    except (OSError,sqlite3.Error):pass
                return {"accepted": False, "reason": "stale_owner"}
            if state.get("reply_id"):
                return {"accepted": False, "reason": "already_replied", "reply_id": state["reply_id"]}
            payload = state["payload"]
            if isinstance(payload, dict) and payload.get("internal_review"):
                # Defence in depth: no QQ outbox record may ever be created for
                # an internal reflection, even if a tool guard regresses.
                return {"accepted": False, "reason": "internal_review_no_output"}
            session_key = payload.get("session_key", "")
            if not isinstance(session_key, str) or not re.fullmatch(r"(?:group|private):[1-9]\d*", session_key):
                return {"accepted": False, "reason": "invalid_session"}
            pending = state.get('selected_mention') or {}
            uid = mention_user or (pending.get('uid') if pending.get('owner') == token and not generated_drawing else None)
            if typed_uid and session_key.startswith('group:'):
                if (uid and uid != typed_uid) or (not uid and not typed_member):
                    return {'accepted': False, 'reason': UNRESOLVED}
                uid = typed_uid
            if uid:
                if not session_key.startswith('group:') or not isinstance(uid, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', uid):
                    return {'accepted': False, 'reason': 'invalid_mention_target'}
                try:
                    segments = attach_recipient(segments, uid)
                except ValueError as exc:
                    return {'accepted': False, 'reason': str(exc)}
            wait = self._user_reply_wait(state, reply_to=reply_to)
            if wait:
                self._finish(state, 'no_reply')
                return {'accepted': False, 'reason': user_reply_quota.LIMIT_REASON, 'retry_after': wait}
            outbox_size = self.outbox_path.stat().st_size if self.outbox_path.exists() else 0
            if outbox_size:
                with self.outbox_path.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        return {"accepted": False, "reason": "outbox_incomplete_record"}
            if files:
                from hakimi.transport.file_delivery import stage
                if not isinstance(files, list) or (not rendered_math and len(segments)+len(files) > MAX_REPLY_SEGMENTS):
                    return {"accepted": False, "reason": "too_many_segments"}
                try:
                    segments = segments+stage(files, payload, self.base_dir)
                except (ValueError, OSError) as exc:
                    return {"accepted": False, "reason": "file_error: "+str(exc)[:180]}
            from hakimi.bridge.reply_format import limit_group_reply
            segments = limit_group_reply(segments, session_key, read_json_cached(self.base_dir / 'config.json', {}))
            if not segments:
                return {"accepted": False, "reason": "empty_group_reply"}
            from hakimi.bridge.bot_registry import target as bot_target,cooling,shape
            bot=bot_target(self.base_dir,payload,segments,reply_to)
            if bot:
                if cooling(self.base_dir,bot,self.clock()):return {'accepted':False,'reason':'bot_cooldown'}
                segments=shape(segments,bot)
                if not segments:return {'accepted':False,'reason':'bot_text_reply'}
            now = self.clock()
            reply_id = uuid.uuid4().hex
            record = {"reply_id": reply_id, "trigger_id": state["trigger_id"], "created_at": now,
                      "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                      "session_key": session_key, "segments": list(segments), "provider": state["provider"],
                      "wake_mode": payload.get("wake_mode", ""), "conversation_lane": self.lane_id,
                      "is_proactive": bool(payload.get("is_proactive", payload.get("wake_mode") == "冷场主动抛话题"))}
            from hakimi.models.model_trial_state import state as trial_state, read as trial_read, root as trial_root
            trial=trial_state(self.base_dir)
            if trial.get('phase') in {'active','completed'}:
                route=trial_read(trial_root(self.base_dir)/'state/vertex_route.json')
                record['model']=route.get('model','gemini-3.8-flash') if provider=='gemini' else 'dsh'
                record['model_trial_id']=trial.get('id')
            if rendered_math:record['rendered_math']=True
            target = str(payload.get('reply_target_message_id', ''))
            if reply_to is not None:
                from hakimi.core.context_window import select_context
                visible, _ = select_context(payload)
                explicit = str(reply_to)
                if any(str(row.get('message_id', '')) == explicit
                       and row.get('session_key', session_key) == session_key for row in visible):
                    target = explicit
            if not record['is_proactive'] and re.fullmatch(r'-?\d{1,20}', target):
                record['reply_target_id'] = target
            if not record['is_proactive']:
                from hakimi.bridge.interaction_policy import annotation
                try:record['interaction']=annotation(payload,interaction,self.base_dir,target)
                except ValueError as exc:return {'accepted':False,'reason':str(exc)}
            if bot:record['interaction']={'user_id':bot,'kind':'ai','bot_target':bot}
            user = user_reply_quota.recipient(payload, reply_to)
            if user:
                record['reply_user_id'] = user
            state["reply_intent"] = {"record": record, "start_offset": outbox_size}
            self._save(state)
            self._reconcile(state)
            return {"accepted": True, "reason": "queued", "reply_id": reply_id, "trigger_id": state["trigger_id"]}
