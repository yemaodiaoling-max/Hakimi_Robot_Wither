"""Track estimated upstream spend and enforce the local monthly cap."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hakimi.gateway.protocol import MONTHLY_EXHAUSTED, TIMEZONE

LEDGER_NAME = "gateway_ledger.sqlite3"
_LEDGER_LOCK = threading.Lock()


def _zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        return timezone(timedelta(hours=-8))


def period(now=None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, _zone()).strftime("%Y-%m")


def next_reset(now=None) -> datetime:
    local = datetime.fromtimestamp(time.time() if now is None else now, _zone())
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=_zone()).astimezone(timezone.utc)


def _ledger(base: Path):
    db = sqlite3.connect(str(base / LEDGER_NAME), timeout=5)
    db.execute("""CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, at REAL, period TEXT, scope TEXT,
                  model TEXT, upstream_model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                  cached_tokens INTEGER, cost_nano INTEGER, status INTEGER)""")
    return db


def _price(config, upstream_model):
    row = (config.get("prices") or {}).get(upstream_model) or {}
    try:
        return Decimal(str(row.get("input_usd_per_million", "0") or "0")), Decimal(str(row.get("output_usd_per_million", "0") or "0"))
    except InvalidOperation:
        return Decimal(0), Decimal(0)


def record(base, config, *, request_id, model, upstream_model, usage, status, scope="qq"):
    usage = usage or {}
    prompt = int(usage.get("promptTokenCount") or 0)
    output = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
    cached = int(usage.get("cachedContentTokenCount") or 0)
    pin, pout = _price(config, upstream_model)
    cost = (Decimal(prompt) * pin + Decimal(output) * pout) / Decimal(1_000_000)
    with _LEDGER_LOCK:
        db = _ledger(base)
        try:
            now = time.time()
            db.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (request_id, now, period(now), scope, model, upstream_model, prompt, output, cached,
                        int(cost * 1_000_000_000), int(status)))
            db.commit()
        finally:
            db.close()


def month_spend(base: Path, now=None) -> Decimal:
    if not (base / LEDGER_NAME).exists():
        return Decimal(0)
    db = _ledger(base)
    try:
        nano = db.execute("SELECT COALESCE(SUM(cost_nano),0) FROM usage WHERE period=?", (period(now),)).fetchone()[0]
    finally:
        db.close()
    return Decimal(int(nano)) / Decimal(1_000_000_000)


def month_tokens(base: Path, now=None) -> dict:
    if not (base / LEDGER_NAME).exists():
        return {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    db = _ledger(base)
    try:
        row = db.execute("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM usage "
                         "WHERE period=?", (period(now),)).fetchone()
    finally:
        db.close()
    return {"requests": row[0], "input_tokens": row[1], "output_tokens": row[2]}


def _limit(config):
    value = config.get("monthly_limit_usd")
    if value in (None, "", 0, "0"):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number > 0 else None


def monthly_budget_exhausted(base: Path, config: dict, now=None) -> bool:
    """Check the cap before forwarding a billable request."""
    limit = _limit(config)
    return limit is not None and month_spend(base, now) >= limit


def budget_payload(base: Path, config: dict, now=None) -> dict:
    """Document accepted by ``hakimi.models.vertex_route.validate_budget_payload``."""
    from hakimi.gateway.protocol import PROJECT
    now = time.time() if now is None else now
    spent = month_spend(base, now)
    limit = _limit(config)
    blocked = bool(limit is not None and spent >= limit)
    iso = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "schema_version": 1, "project": PROJECT, "model": "gemini-3.8-flash", "currency": "USD",
        "timezone": TIMEZONE, "period": period(now),
        "limit_usd": None, "remaining_usd": None,  # the cap is enforced here, reported through `blocked`
        "spent_usd": format(spent.quantize(Decimal("0.000000001")), "f"), "reserved_usd": "0",
        "blocked": blocked, "block_reason": MONTHLY_EXHAUSTED if blocked else None,
        "measurement": "gateway_token_estimate", "price_verified_at": "2026-01-01",
        "official_spend_cap": {"configured": limit is not None,
                               "name": None if limit is None else f"本地月度上限 {limit} USD"},
        "credits": {"official_remaining_usd": None, "source": "local_gateway",
                    "note": "本地网关按配置单价估算；以服务商账单为准"},
        "as_of": iso(datetime.fromtimestamp(now, timezone.utc)), "next_reset_at": iso(next_reset(now)),
    }
