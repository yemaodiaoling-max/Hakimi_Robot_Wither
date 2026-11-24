"""Unified daily Terra budget shared by V5 scheduling and health checks.

The authoritative ledger is the already-deployed
``state/terra_watch/budget.json``.  V5 no longer keeps a second independent
counter: existing same-day ``tokens``/``style_calls``/``fault_calls`` are
migrated in place, while legacy top-level fields and richer subtotals remain
readable by older report consumers.

Policy (priority user update):

* 600k input+output tokens/day baseline;
* the effective ceiling grows by 200k for a real requested reservation, up to
  an absolute 2M/day;
* 100k tokens are protected for health; styles may not consume that reserve;
* 96 style/structured calls and 12 health/fault calls per day;
* health may use the whole effective ceiling (no separate 60k/360k ceiling).

Reservations are persisted before a model starts.  Valid usage settles to the
exact input+output count; failed/unknown/partial/invalid usage consumes the full
reservation.  Settlement is idempotent and a receipt remains attached to the
day it was reserved, including across midnight.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import uuid

DAILY_BASE_TOKENS = 600_000
DAILY_GROWTH_TOKENS = 200_000
DAILY_MAX_TOKENS = 2_000_000
HEALTH_RESERVE_TOKENS = 100_000
TOKEN_RESERVATION = 12_000
BATCH_RESERVE = 24_000  # Structured review includes the CLI base prompt and bounded samples.
STYLE_CALL_LIMIT = 96
FAULT_CALL_LIMIT = 12
MAX_SCHEDULED_CALLS = STYLE_CALL_LIMIT
MAX_HEALTH_CALLS = FAULT_CALL_LIMIT
DEFAULT_HEALTH_RESERVE = TOKEN_RESERVATION
MAX_RESERVATIONS = 256
MAX_HISTORY_DAYS = 7

# Backwards-compatible aliases used by older callers/tests.
DAILY_TOTAL = DAILY_BASE_TOKENS
HEALTH_RESERVE = HEALTH_RESERVE_TOKENS
SCHEDULED_MAX = DAILY_MAX_TOKENS


def _day(now):
    return datetime.fromtimestamp(float(now), timezone.utc).strftime("%Y-%m-%d")


def _nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def _blank(day):
    return {
        "version": 2, "day": day,
        "tokens": 0, "used_tokens": 0, "reserved_tokens": 0,
        "style_calls": 0, "fault_calls": 0,
        "ceiling": DAILY_BASE_TOKENS, "expansions": 0,
        "scheduled": {"reserved": 0, "actual": 0, "calls": 0, "batches": 0, "reservations": []},
        "health": {"reserved": 0, "actual": 0, "calls": 0, "batches": 0, "reservations": []},
        "total_actual": 0, "total_reserved": 0,
        "history": {},
    }


def _bucket(raw, legacy_calls=0):
    raw = raw if isinstance(raw, dict) else {}
    actual = _nonnegative_int(raw.get("actual"))
    reserved = _nonnegative_int(raw.get("reserved"))
    calls = max(_nonnegative_int(raw.get("calls")) or 0, int(legacy_calls or 0))
    batches = max(_nonnegative_int(raw.get("batches")) or 0, calls)
    reservations = [item for item in raw.get("reservations", [])
                    if isinstance(item, dict)][-MAX_RESERVATIONS:]
    return {"reserved": reserved or 0, "actual": actual or 0, "calls": calls,
            "batches": batches, "reservations": reservations}


def _normalize_day(value, day):
    """Migrate one day's legacy fields without resetting valid same-day usage."""
    if not isinstance(value, dict):
        return _blank(day)
    scheduled = _bucket(value.get("scheduled"), _nonnegative_int(value.get("style_calls")) or 0)
    health = _bucket(value.get("health"), _nonnegative_int(value.get("fault_calls")) or 0)
    has_bucket_data = any(
        _nonnegative_int((value.get(key) or {}).get(field)) is not None
        for key in ("scheduled", "health") if isinstance(value.get(key), dict)
        for field in ("actual", "reserved")
    ) if isinstance(value.get("scheduled"), dict) or isinstance(value.get("health"), dict) else False

    tokens = _nonnegative_int(value.get("tokens"))
    used = _nonnegative_int(value.get("used_tokens"))
    reserved = _nonnegative_int(value.get("reserved_tokens"))
    bucket_sum = (scheduled["actual"] + scheduled["reserved"]
                  + health["actual"] + health["reserved"])
    top_valid = (tokens is not None and used is not None and reserved is not None
                 and used + reserved == tokens)
    if not top_valid and not has_bucket_data and tokens is not None:
        # Pre-hotfix files stored only consumed tokens.
        used, reserved = tokens, 0
        top_valid = True
    if not top_valid:
        if has_bucket_data or bucket_sum:
            used = scheduled["actual"] + health["actual"]
            reserved = scheduled["reserved"] + health["reserved"]
            tokens = used + reserved
        else:
            used = reserved = tokens = 0
    elif bucket_sum == 0 and tokens:
        # Legacy top-level-only usage belonged to the old style counter.
        scheduled["actual"], scheduled["reserved"] = used, reserved
        bucket_sum = used + reserved
    elif bucket_sum != tokens:
        if bucket_sum < tokens:
            scheduled["actual"] += tokens - bucket_sum
        else:
            used = scheduled["actual"] + health["actual"]
            reserved = scheduled["reserved"] + health["reserved"]
            tokens = used + reserved

    style_calls = max(_nonnegative_int(value.get("style_calls")) or 0, scheduled["calls"])
    fault_calls = max(_nonnegative_int(value.get("fault_calls")) or 0, health["calls"])
    scheduled["calls"] = style_calls
    health["calls"] = fault_calls
    scheduled["batches"] = max(scheduled["batches"], style_calls)
    health["batches"] = max(health["batches"], fault_calls)
    used = scheduled["actual"] + health["actual"]
    reserved = scheduled["reserved"] + health["reserved"]

    ceiling = _nonnegative_int(value.get("ceiling"))
    if ceiling is None or ceiling < DAILY_BASE_TOKENS:
        ceiling = DAILY_BASE_TOKENS
    ceiling = min(ceiling, DAILY_MAX_TOKENS)
    history = value.get("history") if isinstance(value.get("history"), dict) else {}
    history = {str(key): item for key, item in history.items()
               if isinstance(item, dict) and str(key)}
    if len(history) > MAX_HISTORY_DAYS:
        for key in sorted(history)[:-MAX_HISTORY_DAYS]:
            history.pop(key, None)
    return {
        "version": 2, "day": str(day or value.get("day") or ""),
        "tokens": used + reserved, "used_tokens": used, "reserved_tokens": reserved,
        "style_calls": style_calls, "fault_calls": fault_calls,
        "ceiling": ceiling,
        "expansions": max(0, _nonnegative_int(value.get("expansions")) or 0),
        "scheduled": scheduled, "health": health,
        "total_actual": used, "total_reserved": reserved,
        "history": history,
    }


def _sync(value):
    scheduled, health = value["scheduled"], value["health"]
    value["used_tokens"] = scheduled["actual"] + health["actual"]
    value["reserved_tokens"] = scheduled["reserved"] + health["reserved"]
    value["tokens"] = value["used_tokens"] + value["reserved_tokens"]
    value["style_calls"] = scheduled["calls"]
    value["fault_calls"] = health["calls"]
    value["total_actual"] = value["used_tokens"]
    value["total_reserved"] = value["reserved_tokens"]


def _read_raw(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _prepare(raw, now):
    """Return today's ledger, archiving a previous day on mutation."""
    today = _day(now)
    if not isinstance(raw, dict) or not raw:
        return _blank(today)
    old_day = str(raw.get("day") or today)
    state = _normalize_day(raw, old_day)
    history = state.get("history", {})
    if old_day != today:
        archived = _normalize_day(raw, old_day)
        archived["history"] = {}
        if (archived["tokens"] or archived["style_calls"] or archived["fault_calls"]
                or archived["scheduled"]["reservations"] or archived["health"]["reservations"]):
            history[old_day] = archived
        if len(history) > MAX_HISTORY_DAYS:
            for key in sorted(history)[:-MAX_HISTORY_DAYS]:
                history.pop(key, None)
        state = _blank(today)
        state["history"] = history
    else:
        state["history"] = history
    return state


def _report_state(value):
    used = int(value["used_tokens"])
    reserved = int(value["reserved_tokens"])
    tokens = used + reserved
    ceiling = int(value["ceiling"])
    style_remaining = max(0, ceiling - HEALTH_RESERVE_TOKENS - tokens)
    health_remaining = max(0, ceiling - tokens)
    scheduled, health = value["scheduled"], value["health"]
    return {
        "day": value["day"],
        "policy": {
            "daily_total": DAILY_BASE_TOKENS,
            "base_ceiling": DAILY_BASE_TOKENS,
            "growth_tokens": DAILY_GROWTH_TOKENS,
            "daily_max": DAILY_MAX_TOKENS,
            "absolute_ceiling": DAILY_MAX_TOKENS,
            "health_reserve": HEALTH_RESERVE_TOKENS,
            "batch_reserve": BATCH_RESERVE,
            "max_scheduled_calls": STYLE_CALL_LIMIT,
            "max_health_calls": FAULT_CALL_LIMIT,
            "scheduled_max": DAILY_MAX_TOKENS,
        },
        "base_ceiling": DAILY_BASE_TOKENS,
        "effective_ceiling": ceiling,
        "absolute_ceiling": DAILY_MAX_TOKENS,
        "ceiling": ceiling,
        "expansions": int(value["expansions"]),
        "tokens": tokens,
        "actual": used,
        "reserved": reserved,
        "used_tokens": used,
        "reserved_tokens": reserved,
        "remaining": max(0, ceiling - tokens),
        "style_calls": int(scheduled["calls"]),
        "style_call_limit": STYLE_CALL_LIMIT,
        "fault_calls": int(health["calls"]),
        "fault_call_limit": FAULT_CALL_LIMIT,
        "style_allowance": style_remaining,
        "style_allowance_tokens": style_remaining,
        "health_allowance": health_remaining,
        "health_allowance_tokens": health_remaining,
        "health_reserve_tokens": HEALTH_RESERVE_TOKENS,
        "scheduled": {
            "actual": int(scheduled["actual"]), "reserved": int(scheduled["reserved"]),
            "calls": int(scheduled["calls"]), "batches": int(scheduled["batches"]),
            "remaining": style_remaining, "call_limit": STYLE_CALL_LIMIT,
        },
        "health": {
            "actual": int(health["actual"]), "reserved": int(health["reserved"]),
            "calls": int(health["calls"]), "batches": int(health["batches"]),
            "remaining": health_remaining, "call_limit": FAULT_CALL_LIMIT,
        },
    }


def _report_day(raw, now):
    day = _day(now)
    if str(raw.get("day") or "") == day:
        return _report_state(_normalize_day(raw, day))
    history = raw.get("history") if isinstance(raw.get("history"), dict) else {}
    archived = history.get(day)
    if isinstance(archived, dict):
        return _report_state(_normalize_day(archived, day))
    return _report_state(_blank(day))


def _find_receipt(state, receipt_id):
    for name in ("scheduled", "health"):
        bucket = state.get(name) or {}
        for item in bucket.get("reservations", []):
            if isinstance(item, dict) and item.get("id") == receipt_id:
                return state, bucket, item
    history = state.get("history") if isinstance(state.get("history"), dict) else {}
    for day in sorted(history, reverse=True):
        archived = history.get(day)
        if not isinstance(archived, dict):
            continue
        for name in ("scheduled", "health"):
            bucket = archived.get(name) or {}
            for item in bucket.get("reservations", []):
                if isinstance(item, dict) and item.get("id") == receipt_id:
                    return archived, bucket, item
    return None, None, None


@contextmanager
def _locked(path):
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as stream:
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


class BudgetError(RuntimeError):
    pass


class BudgetExhausted(BudgetError):
    pass


class TerraBudget:
    def __init__(self, path):
        self.path = Path(path)

    def _save(self, value):
        _sync(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                             encoding="utf-8")
        os.replace(temporary, self.path)

    def report(self, now=None):
        now = time.time() if now is None else float(now)
        with _locked(self.path):
            raw = _read_raw(self.path)
        return _report_day(raw, now)

    def can_reserve(self, kind, now, estimate=DEFAULT_HEALTH_RESERVE):
        now = time.time() if now is None else float(now)
        kind = "scheduled" if kind in ("scheduled", "style") else "health"
        if type(estimate) is not int or estimate <= 0 or estimate > DAILY_MAX_TOKENS:
            return False, "invalid_reservation"
        report = self.report(now)
        limit = STYLE_CALL_LIMIT if kind == "scheduled" else FAULT_CALL_LIMIT
        calls = report[kind]["calls"]
        if calls >= limit:
            return False, ("scheduled_calls_exhausted" if kind == "scheduled"
                           else "health_calls_exhausted")
        protected = HEALTH_RESERVE_TOKENS if kind == "scheduled" else 0
        if report["tokens"] + estimate + protected > DAILY_MAX_TOKENS:
            return False, "daily_tokens_exhausted"
        return True, ""

    def reserve(self, kind, now=None, estimate=DEFAULT_HEALTH_RESERVE):
        """Persist a reservation before starting a model; returns its receipt."""
        now = time.time() if now is None else float(now)
        kind = "scheduled" if kind in ("scheduled", "style") else "health"
        if type(estimate) is not int or estimate <= 0 or estimate > DAILY_MAX_TOKENS:
            raise BudgetExhausted("invalid_reservation")
        with _locked(self.path):
            value = _prepare(_read_raw(self.path), now)
            bucket = value[kind]
            limit = STYLE_CALL_LIMIT if kind == "scheduled" else FAULT_CALL_LIMIT
            if bucket["calls"] >= limit:
                raise BudgetExhausted("scheduled_calls_exhausted" if kind == "scheduled"
                                      else "health_calls_exhausted")
            protected = HEALTH_RESERVE_TOKENS if kind == "scheduled" else 0
            while (value["tokens"] + estimate > value["ceiling"] - protected
                   and value["ceiling"] < DAILY_MAX_TOKENS):
                value["ceiling"] = min(DAILY_MAX_TOKENS,
                                       value["ceiling"] + DAILY_GROWTH_TOKENS)
                value["expansions"] += 1
            if value["tokens"] + estimate > value["ceiling"] - protected:
                raise BudgetExhausted("daily_tokens_exhausted")
            bucket["calls"] += 1
            bucket["batches"] = max(bucket["batches"], bucket["calls"])
            bucket["reserved"] += estimate
            receipt = {"id": uuid.uuid4().hex, "kind": kind, "amount": estimate,
                       "at": now, "day": value["day"], "status": "reserved"}
            bucket["reservations"] = bucket["reservations"][-(MAX_RESERVATIONS - 1):] + [receipt]
            _sync(value)
            self._save(value)
        return receipt

    def settle(self, receipt, usage=None, *, success=True, now=None):
        """Settle once; invalid/partial/failed usage keeps the full reservation."""
        if not isinstance(receipt, dict) or not receipt.get("id"):
            raise BudgetError("invalid_receipt")
        receipt_id = str(receipt["id"])
        now = time.time() if now is None else float(now)
        with _locked(self.path):
            raw = _read_raw(self.path)
            current = _prepare(raw, now)
            day_state, bucket, item = _find_receipt(current, receipt_id)
            if item is None or item.get("status") != "reserved":
                # Already settled/unknown: never charge the same receipt twice.
                return _report_day(_read_raw(self.path), now)
            amount = max(0, _nonnegative_int(item.get("amount")) or 0)
            input_tokens = output_tokens = None
            if isinstance(usage, dict):
                if type(usage.get("input_tokens")) is int and usage["input_tokens"] >= 0:
                    input_tokens = usage["input_tokens"]
                if type(usage.get("output_tokens")) is int and usage["output_tokens"] >= 0:
                    output_tokens = usage["output_tokens"]
            valid = success and input_tokens is not None and output_tokens is not None
            actual = input_tokens + output_tokens if valid else amount
            bucket["reserved"] = max(0, bucket["reserved"] - amount)
            bucket["actual"] += actual
            item["usage"] = actual
            item["status"] = "settled" if valid else (
                "failed_reserved" if not success else "unknown_reserved")
            item["settled_at"] = now
            _sync(day_state)
            history = current.get("history") if isinstance(current.get("history"), dict) else {}
            if day_state is not current:
                history[day_state["day"]] = day_state
            current["history"] = history
            _sync(current)
            self._save(current)
        return self.report(now)


def budget_for(base):
    base = Path(base)
    if base.name == 'official_test_runtime':
        base = base.parent
    return TerraBudget(base / "state" / "terra_watch" / "budget.json")


def report(base, now=None):
    return budget_for(base).report(now)
