"""Trusted local switch for the private Vertex gateway and its monthly budget.

The Antigravity native process (``agy``) stays the execution layer.  When the
operator switch is enabled this module only changes the child environment and
the per-conversation checkpoint directory, so no original-account session is
resumed.  It also reads the gateway's read-only ``/budget`` endpoint and keeps a
small, credential-free status cache for the console.

Nothing here is allowed to turn an unreadable budget into unlimited credit.
Any configuration, TLS, authentication, HTTP or schema failure is fail-closed.
"""
from __future__ import annotations
from hakimi import paths

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import runpy
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from hakimi.gateway.protocol import MONTHLY_EXHAUSTED, PROJECT, TIMEZONE
from hakimi.models.vertex_model import HYDROGEN, vertex_model

MODEL = "gemini-3.8-flash"
SUPPORTED_MODELS = frozenset({MODEL, HYDROGEN, "gemini-3.5-flash-lite", "gemini-4-argon", "gemini-4-carbon"})
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 18765
GATEWAY_URL = f"https://{GATEWAY_HOST}:{GATEWAY_PORT}"
PRODUCTION_DIRECTORY = paths.gateway_dir()
MONTH_LIMIT = Decimal(os.environ.get("HAKIMI_MONTH_LIMIT_USD", "110") or "110")
MONTH_LIMIT_TEXT = format(MONTH_LIMIT.quantize(Decimal("0.000000001")), "f")
MEASUREMENT = "gateway_token_estimate"
PRICE_VERIFIED_AT = "2026-10-27"
CREDITS_NOTE = "官方赠金余额尚未接入"
SCHEMA_VERSION = 1
CACHE_SECONDS = 15
STATUS_MAX_AGE_SECONDS = 60
KEY_NAME = "gateway.key"
CERT_NAME = "gateway-cert.pem"
ROUTE_STATE = "vertex_route.json"
BUDGET_STATE = "vertex_budget_status.json"
GATE_STATE = "vertex_primary_gate.json"
SESSION_DIRECTORY = "gemini_vertex_sessions_v1"
OFFICIAL_SPEND_CAP = 'vertex_official_spend_cap_breached'

_REQUIRED_CONFIG = {
    "enabled",
    "gateway_directory",
    "model",
    "month_limit_usd",
    "fallback",
}
_CLEARED_ENV = {
    "AGY_ADC_AUTH",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AGY_ACCOUNT",
}
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?")
_KEY = re.compile(rb"[A-Za-z0-9._~+/=-]{1,4096}")
_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SESSION = re.compile(r"(?:group|private):[1-9][0-9]{0,19}")


class VertexRouteError(RuntimeError):
    """Fixed-code failure; no response body, key or prompt is carried."""

    code = "gateway_unavailable"

    def __init__(self, message=None):
        super().__init__(message or self.code)
        self.code = message or self.code


class RouteConfigError(VertexRouteError):
    code = "gateway_unavailable"


class GatewayUnavailable(VertexRouteError):
    code = "gateway_unavailable"


class VertexBudgetExceeded(VertexRouteError):
    code = "vertex_budget"


def root_for(base=None):
    """Project root.  A runtime base stores state one directory above."""
    path = Path(base) if base is not None else paths.base()
    path = path.resolve()
    return path.parent if path.name == "official_test_runtime" else path


def _state_path(base, name):
    return root_for(base) / "state" / name


def _local_directory(value):
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise RouteConfigError("gateway_directory_invalid")
    if "://" in value or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise RouteConfigError("gateway_directory_invalid")
    if value.startswith(("\\\\", "//")):
        raise RouteConfigError("gateway_directory_invalid")
    path = Path(value)
    if not path.is_absolute() and not value.startswith(("/", "\\")):
        raise RouteConfigError("gateway_directory_invalid")
    try:
        return path.resolve()
    except OSError as exc:
        raise RouteConfigError("gateway_directory_invalid") from exc


def _limit(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise RouteConfigError("month_limit_invalid")
    if not isinstance(value, (str, int, float)):
        raise RouteConfigError("month_limit_invalid")
    text = str(value)
    if not _DECIMAL.fullmatch(text):
        raise RouteConfigError("month_limit_invalid")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise RouteConfigError("month_limit_invalid") from exc
    if not number.is_finite() or number != MONTH_LIMIT:
        raise RouteConfigError("month_limit_invalid")
    return MONTH_LIMIT


def _validate_config(value, trusted_directories, directory_override=None):
    if not isinstance(value, dict):
        raise RouteConfigError("route_schema_invalid")
    if set(value) != _REQUIRED_CONFIG:
        raise RouteConfigError("route_schema_invalid")
    if type(value["enabled"]) is not bool:
        raise RouteConfigError("route_schema_invalid")
    directory = _local_directory(value["gateway_directory"])
    trusted = {Path(PRODUCTION_DIRECTORY).resolve(), *trusted_directories}
    if directory_override is not None:
        # An explicit caller injection is trusted; tests can point production
        # metadata at an isolated fixture without weakening the config check.
        directory = directory_override
    elif directory not in trusted:
        raise RouteConfigError("gateway_directory_untrusted")
    if value["model"] not in SUPPORTED_MODELS:
        raise RouteConfigError("model_not_allowed")
    limit = _limit(value["month_limit_usd"])
    if value["fallback"] != "dsh":
        raise RouteConfigError("fallback_not_allowed")
    return {
        "enabled": value["enabled"],
        "gateway_directory": directory,
        "model": value["model"],
        "month_limit_usd": limit,
        "fallback": "dsh",
    }


class Route:
    """A validated user switch.  Errors are stored, never silently ignored."""

    def __init__(self, base, *, enabled, gateway_directory=None, model=MODEL,
                 month_limit=None, fallback="dsh", error=None,
                 config_path=None):
        self.base = Path(base).resolve()
        self.root = root_for(self.base)
        self.enabled = bool(enabled)
        self.gateway_directory = gateway_directory
        self.model = model
        self.month_limit = month_limit
        self.fallback = fallback
        self.error = error
        self.config_path = config_path or _state_path(self.base, ROUTE_STATE)

    @property
    def state_directory(self):
        return self.root / "state" / SESSION_DIRECTORY

    def _require_enabled_ok(self):
        if not self.enabled:
            raise RouteConfigError("route_disabled")
        if self.error:
            raise RouteConfigError(self.error)

    def credentials(self):
        """Read key/certificate from the trusted directory.  Never log either."""
        self._require_enabled_ok()
        directory = self.gateway_directory
        key_path = directory / KEY_NAME
        cert_path = directory / CERT_NAME
        try:
            key = key_path.read_bytes().strip()
        except OSError as exc:
            raise RouteConfigError("gateway_key_unreadable") from exc
        if not _KEY.fullmatch(key):
            raise RouteConfigError("gateway_key_invalid")
        try:
            if cert_path.stat().st_size < 32:
                raise RouteConfigError("gateway_ca_invalid")
            ssl.create_default_context(cafile=str(cert_path))
        except RouteConfigError:
            raise
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise RouteConfigError("gateway_ca_invalid") from exc
        return key.decode("ascii"), cert_path.resolve()

    def client_environment(self, original=None):
        """Return a child-only environment; the caller never logs the key."""
        original = dict(original or {})
        if not self.enabled:
            return original
        key, cert_path = self.credentials()
        env = dict(original)
        for name in list(env):
            if name.startswith("AGY_LLM_GATEWAY_") or name in _CLEARED_ENV:
                env.pop(name, None)
        env.update(
            AGY_LLM_GATEWAY_URL=GATEWAY_URL,
            AGY_LLM_GATEWAY_API_KEY=key,
            AGY_LLM_GATEWAY_CA_CERT=str(cert_path),
            AGY_LLM_GATEWAY_MODELS=vertex_model(self.model),
            AGY_LLM_GATEWAY_WIRE_PROTOCOL="genai",
            AGY_LLM_GATEWAY_TIER_FLASH=vertex_model(self.model),
            AGY_LLM_GATEWAY_TIER_FLASH_LITE=vertex_model(self.model),
            AGY_LLM_GATEWAY_TIER_PRO=vertex_model(self.model),
            AGY_CLI_DISABLE_AUTO_UPDATE="true",
        )
        return env

    def budget(self, *, now=None, force=False, fetcher=None):
        """Fetch /budget with a hard <=15s cache and full schema validation."""
        self._require_enabled_ok()
        now = time.time() if now is None else float(now)
        key = (str(self.config_path), str(self.gateway_directory), self.model)
        if not force:
            cached = _CACHE.get(key)
            if cached and 0 <= now - cached["at"] < CACHE_SECONDS:
                if cached.get("error"):
                    raise GatewayUnavailable(cached["error"])
                value = cached.get("value") or {}
                # A month boundary invalidates cached spend immediately.
                if value.get("period") == _pacific_month(now):
                    return dict(value)
        try:
            raw = (fetcher or _fetch_budget)(self)
            value = validate_budget_payload(raw, now=now)
        except VertexRouteError as exc:
            _CACHE[key] = {"at": now, "error": exc.code, "value": None}
            raise
        except Exception as exc:  # Never expose a response body or secret.
            _CACHE[key] = {"at": now, "error": "gateway_unavailable", "value": None}
            raise GatewayUnavailable("gateway_unavailable") from exc
        _CACHE[key] = {"at": now, "error": None, "value": dict(value)}
        return dict(value)

    def check(self, *, now=None, force=False, fetcher=None, write_status=True):
        """Evaluate the budget, persist a safe cache and apply/release the gate."""
        now = time.time() if now is None else float(now)
        if not self.enabled:
            # Removing/ disabling the trusted switch restores the legacy route,
            # but never clears a pause that this feature did not apply.
            release_primary_gate(self, now)
            return build_status(self, now=now)
        try:
            from hakimi.models import hydrogen_duty, video_hydrogen
            self._require_enabled_ok()
            backup = video_hydrogen.config(self.root)
            duty = hydrogen_duty.snapshot(self.root) if backup else {}
            active = duty.get('mode') == 'video_hydrogen'
            upstream_error = None
            if active:
                # Budget and operator controls are on this host. QQ dispatch
                # need not wait for an unhealthy gateway socket or a probe.
                budget = self._local_budget(now)
                upstream_error = duty.get('reason') or 'gateway_unavailable'
            else:
                try:
                    budget = self.budget(now=now, force=force, fetcher=fetcher)
                except GatewayUnavailable as exc:
                    if not backup or exc.code not in {'gateway_unavailable', 'gateway_http_408',
                                                     'gateway_http_429', 'gateway_http_500', 'gateway_http_502',
                                                     'gateway_http_503', 'gateway_http_504'}:
                        raise
                    # Admit a real model request so transport failures can be
                    # counted. Polling /budget by itself is never a failure vote.
                    budget = self._local_budget(now)
                    upstream_error = exc.code
            status = build_status(self, budget=budget, now=now)
            status['vertex_blocked'] = bool(status['blocked'] or upstream_error)
            status['active_provider'] = 'vertex'
            if backup and (status['block_reason'] in {MONTHLY_EXHAUSTED, OFFICIAL_SPEND_CAP}
                           or (active and not status['blocked'])):
                active_provider, active_model = video_hydrogen.duty_identity(backup)
                status.update(blocked=False, reason=None, active_provider=active_provider,
                              active_model=active_model)
            if upstream_error and not status['blocked'] and status['block_reason'] is None:
                status['block_reason'] = upstream_error
            if (not backup and self.model == 'gemini-4-argon' and status['blocked']
                    and status.get('block_reason') == MONTHLY_EXHAUSTED):
                from hakimi.models.argon_session import backup_config
                if backup_config(self.root):
                    status.update(blocked=False, active_provider='argon_backup', reason=None)
            if write_status:
                write_budget_status(self, status)
            if status["blocked"]:
                apply_primary_gate(self, "vertex_budget", now)
            else:
                release_primary_gate(self, now)
            return status
        except VertexRouteError as exc:
            code = "vertex_budget" if exc.code == "vertex_budget" else "gateway_unavailable"
            status = build_status(self, now=now, error=code)
            if write_status:
                write_budget_status(self, status)
            apply_primary_gate(self, code, now)
            return status
        except Exception:
            status = build_status(self, now=now, error="gateway_unavailable")
            if write_status:
                write_budget_status(self, status)
            apply_primary_gate(self, "gateway_unavailable", now)
            return status

    def _local_budget(self, now):
        # Use the installed gateway's own read-only ledger (including
        # vertex_budget_control.json), pricing and outstanding liabilities.
        directory = self.gateway_directory
        project = json.loads((directory / 'gateway_provision.json').read_text())['project']
        limit = int(self.month_limit * 1_000_000_000) if self.month_limit is not None else None
        budget = _ledger_class(directory)(directory, limit_nano=limit).budget_status(now=now)
        return validate_budget_payload(dict(budget, project=project), now=now)


@lru_cache(maxsize=4)
def _ledger_class(directory):
    # Route validation has already pinned this operator-owned directory.
    return runpy.run_path(str(directory / 'credit_ledger.py'))['CreditLedger']


def load(base=None, *, gateway_directory=None, allowed_directories=None):
    """Read root/state/vertex_route.json; missing means the legacy route.

    ``gateway_directory``/``allowed_directories`` are explicit trust injections
    for isolated tests.  Production accepts only the fixed local directory.
    """
    root = root_for(base)
    path = root / "state" / ROUTE_STATE
    override = _local_directory(gateway_directory) if gateway_directory is not None else None
    trusted = set()
    if override is not None:
        trusted.add(override)
    for item in allowed_directories or ():
        trusted.add(_local_directory(item))
    if not path.exists():
        return Route(root, enabled=False, config_path=path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Route(root, enabled=True, error="route_config_unreadable", config_path=path)
    try:
        value = _validate_config(raw, trusted, override)
    except RouteConfigError as exc:
        # Only an explicit, valid-looking false may fall back to legacy mode.
        enabled = not (isinstance(raw, dict) and raw.get("enabled") is False)
        return Route(root, enabled=enabled, error=exc.code, config_path=path)
    return Route(root, enabled=value["enabled"],
                 gateway_directory=value["gateway_directory"],
                 model=value["model"], month_limit=value["month_limit_usd"],
                 fallback=value["fallback"], config_path=path)


def client_environment(base=None, original=None, *, gateway_directory=None,
                       allowed_directories=None):
    """Convenience wrapper matching the documented ``(base, original)`` form."""
    if original is None and isinstance(base, dict):
        original, base = base, None
    route = load(base, gateway_directory=gateway_directory,
                 allowed_directories=allowed_directories)
    return route.client_environment(original or {})


def _pacific_month(now):
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(TIMEZONE)
    except Exception:
        # The gateway resets months authoritatively; this fallback only guards
        # cache invalidation on hosts without an IANA database.
        zone = timezone(timedelta(hours=-8))
    return datetime.fromtimestamp(now, zone).strftime("%Y-%m")


def _iso_utc(value, field):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise GatewayUnavailable(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayUnavailable(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise GatewayUnavailable(f"{field}_invalid")
    try:
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, ValueError) as exc:
        raise GatewayUnavailable(f"{field}_invalid") from exc


def _decimal(value, field, *, zero_ok=True):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise GatewayUnavailable(f"{field}_invalid")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise GatewayUnavailable(f"{field}_invalid") from exc
    if not number.is_finite() or number < 0 or (not zero_ok and number == 0):
        raise GatewayUnavailable(f"{field}_invalid")
    return number


def _money(value):
    return format(value.quantize(Decimal("0.000000001")), "f")


def _monthly_amounts(value, spent, reserved):
    """A manual gateway explicitly sends null for both cap and remainder."""
    if "limit_usd" not in value or "remaining_usd" not in value:
        raise GatewayUnavailable("limit_invalid")
    if value["limit_usd"] is None and value["remaining_usd"] is None:
        return None, None
    limit = _decimal(value["limit_usd"], "limit")
    if limit != MONTH_LIMIT:
        raise GatewayUnavailable("limit_not_allowed")
    remaining = _decimal(value["remaining_usd"], "remaining")
    if remaining > limit or remaining > max(Decimal(0), limit - spent - reserved) + Decimal("0.000000001"):
        raise GatewayUnavailable("remaining_invalid")
    return _money(limit), _money(remaining)


def _credit_as_of(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise GatewayUnavailable("credits_as_of_invalid")
    _iso_utc(value, "credits_as_of")
    # Preserve the gateway snapshot's exact observation time.
    return value


def _credit_amount(value, field):
    _decimal(value, field)
    return value


def _credit_grants(value):
    if not isinstance(value, list) or len(value) > 64:
        raise GatewayUnavailable("credits_grants_invalid")
    grants = []
    for grant in value:
        if not isinstance(grant, dict):
            raise GatewayUnavailable("credits_grants_invalid")
        name = grant.get("name")
        status = grant.get("status")
        expires = grant.get("expires_on")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise GatewayUnavailable("credits_grants_invalid")
        if not isinstance(status, str) or not status.strip() or len(status) > 64:
            raise GatewayUnavailable("credits_grants_invalid")
        if not isinstance(expires, str) or not _DATE.fullmatch(expires):
            raise GatewayUnavailable("credits_grants_invalid")
        try:
            date.fromisoformat(expires)
        except ValueError as exc:
            raise GatewayUnavailable("credits_grants_invalid") from exc
        grants.append({
            "name": name,
            "status": status,
            "remaining_usd": _credit_amount(grant.get("remaining_usd"),
                                            "credits_grant_remaining"),
            "expires_on": expires,
        })
    return grants


def _credits(value):
    if not isinstance(value, dict):
        raise GatewayUnavailable("credits_invalid")
    official = value.get("official_remaining_usd")
    if official is not None:
        _credit_amount(official, "official_remaining")
    source = value.get("source")
    note = value.get("note")
    if not isinstance(source, str) or not 0 < len(source) <= 80:
        raise GatewayUnavailable("credits_invalid")
    if not isinstance(note, str) or len(note) > 500:
        raise GatewayUnavailable("credits_invalid")
    result = {"official_remaining_usd": official, "source": source, "note": note}
    if value.get("as_of") is not None:
        result["as_of"] = _credit_as_of(value["as_of"])
    elif official is not None:
        # An official amount without its observation time is not auditable.
        raise GatewayUnavailable("credits_as_of_invalid")
    if "stale" in value:
        if type(value["stale"]) is not bool:
            raise GatewayUnavailable("credits_stale_invalid")
        result["stale"] = value["stale"]
    if value.get("grants") is not None:
        result["grants"] = _credit_grants(value["grants"])
    return result


def validate_budget_payload(value, *, now=None):
    """Validate one gateway /budget document; any doubt raises (fail closed)."""
    if not isinstance(value, dict):
        raise GatewayUnavailable("budget_schema_invalid")
    if value.get("schema_version") != SCHEMA_VERSION or type(value.get("schema_version")) is not int:
        raise GatewayUnavailable("budget_schema_invalid")
    if value.get("project") != PROJECT:
        raise GatewayUnavailable("project_not_allowed")
    if value.get("model") not in SUPPORTED_MODELS:
        raise GatewayUnavailable("model_not_allowed")
    if value.get("currency") != "USD":
        raise GatewayUnavailable("currency_not_allowed")
    if value.get("timezone") != TIMEZONE:
        raise GatewayUnavailable("timezone_not_allowed")
    period = value.get("period")
    if not isinstance(period, str) or not _PERIOD.fullmatch(period):
        raise GatewayUnavailable("period_invalid")
    now = time.time() if now is None else float(now)
    if period != _pacific_month(now):
        raise GatewayUnavailable("period_mismatch")
    spent = _decimal(value.get("spent_usd"), "spent")
    reserved = _decimal(value.get("reserved_usd"), "reserved")
    limit, remaining = _monthly_amounts(value, spent, reserved)
    if type(value.get("blocked")) is not bool:
        raise GatewayUnavailable("blocked_invalid")
    reason = value.get("block_reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 160):
        raise GatewayUnavailable("block_reason_invalid")
    blocked = value["blocked"] or (remaining is not None and Decimal(remaining) <= 0)
    if blocked and not reason:
        reason = "vertex_budget"
    if value.get("measurement") != MEASUREMENT:
        raise GatewayUnavailable("measurement_invalid")
    price = value.get("price_verified_at")
    if not isinstance(price, str) or not _DATE.fullmatch(price):
        raise GatewayUnavailable("price_verified_at_invalid")
    try:
        date.fromisoformat(price)
    except ValueError as exc:
        raise GatewayUnavailable("price_verified_at_invalid") from exc
    cap = value.get("official_spend_cap")
    if not isinstance(cap, dict) or type(cap.get("configured")) is not bool:
        raise GatewayUnavailable("official_spend_cap_invalid")
    name = cap.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 200):
        raise GatewayUnavailable("official_spend_cap_invalid")
    credits = _credits(value.get("credits"))
    as_of = _iso_utc(value.get("as_of"), "as_of")
    reset = _iso_utc(value.get("next_reset_at"), "next_reset_at")
    as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
    if reset_dt <= as_of_dt:
        raise GatewayUnavailable("next_reset_at_invalid")
    if as_of_dt.timestamp() - now > 300 or now - as_of_dt.timestamp() > 300:
        raise GatewayUnavailable("as_of_stale")
    if reset_dt.timestamp() < now - 1:
        raise GatewayUnavailable("next_reset_at_invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "project": PROJECT,
        "model": MODEL,
        "currency": "USD",
        "period": period,
        "timezone": TIMEZONE,
        "limit_usd": limit,
        "spent_usd": _money(spent),
        "reserved_usd": _money(reserved),
        "remaining_usd": remaining,
        "blocked": bool(blocked),
        "block_reason": reason,
        "next_reset_at": reset,
        "as_of": as_of,
        "measurement": MEASUREMENT,
        "price_verified_at": PRICE_VERIFIED_AT,
        "official_spend_cap": {"configured": cap["configured"], "name": name},
        "credits": credits,
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_budget(route):
    key, cert_path = route.credentials()
    context = ssl.create_default_context(cafile=str(cert_path))
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect())
    request = Request(
        GATEWAY_URL + "/budget",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=6) as response:
            if getattr(response, "status", 200) != 200:
                raise GatewayUnavailable("gateway_http_error")
            body = response.read(128 * 1024 + 1)
    except HTTPError as exc:
        raise GatewayUnavailable(f"gateway_http_{int(getattr(exc, 'code', 0) or 0)}") from exc
    except (URLError, TimeoutError, OSError, ValueError, ssl.SSLError) as exc:
        raise GatewayUnavailable("gateway_unavailable") from exc
    if len(body) > 128 * 1024:
        raise GatewayUnavailable("budget_response_too_large")
    try:
        value = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GatewayUnavailable("budget_schema_invalid") from exc
    if not isinstance(value, dict):
        raise GatewayUnavailable("budget_schema_invalid")
    return value


_CACHE = {}


def reset_cache():
    _CACHE.clear()


def _now_iso(now):
    return datetime.fromtimestamp(float(now), timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def build_status(route, *, budget=None, now=None, error=None):
    """Fixed, credential-free status document.  Unknown never means zero."""
    now = time.time() if now is None else float(now)
    as_of = _now_iso(now)
    if budget is None:
        enabled = bool(route.enabled)
        reason = error or (route.error and "gateway_unavailable") or "route_disabled"
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unknown",
            "fresh": False,
            "route_enabled": enabled,
            "blocked": bool(enabled),
            "reason": reason,
            "block_reason": None,
            "project": PROJECT,
            "model": route.model,
            "currency": "USD",
            "period": _pacific_month(now),
            "timezone": TIMEZONE,
            "limit_usd": _money(route.month_limit) if route.month_limit is not None else None,
            "spent_usd": None,
            "reserved_usd": None,
            "remaining_usd": None,
            "measurement": MEASUREMENT,
            "price_verified_at": PRICE_VERIFIED_AT,
            "official_spend_cap": {"configured": False, "name": None},
            "credits": {"official_remaining_usd": None, "source": "not_connected",
                        "note": CREDITS_NOTE},
            "next_reset_at": None,
            "gateway_as_of": None,
            "as_of": as_of,
        }
    blocked = bool(budget["blocked"])
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "fresh": True,
        "route_enabled": True,
        "blocked": blocked,
        "reason": "vertex_budget" if blocked else None,
        "block_reason": budget.get("block_reason") if blocked else None,
        "project": PROJECT,
        "model": route.model,
        "currency": "USD",
        "period": budget["period"],
        "timezone": TIMEZONE,
        "limit_usd": budget["limit_usd"],
        "spent_usd": budget["spent_usd"],
        "reserved_usd": budget["reserved_usd"],
        "remaining_usd": budget["remaining_usd"],
        "measurement": MEASUREMENT,
        "price_verified_at": PRICE_VERIFIED_AT,
        "official_spend_cap": dict(budget["official_spend_cap"]),
        "credits": dict(budget["credits"]),
        "next_reset_at": budget["next_reset_at"],
        "gateway_as_of": budget["as_of"],
        "as_of": as_of,
    }


def atomic_json(path, value):
    """Atomic write; fixed caller schemas contain no key, prompt or chat."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp." + str(os.getpid()))
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"),
                  allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_budget_status(route, status, *, now=None):
    if not route.enabled:
        return None
    path = _state_path(route.base, BUDGET_STATE)
    atomic_json(path, status)
    return path


def _read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError, TypeError):
        return default


def _health(route):
    from hakimi.models.provider_health import ProviderHealth
    return ProviderHealth(route.base)


def apply_primary_gate(route, reason, now=None):
    """Pause primary once per state change, recording any prior user pause."""
    if not route.enabled:
        return None
    now = time.time() if now is None else float(now)
    path = _state_path(route.base, GATE_STATE)
    current = _read_json(path, {})
    if current.get("active") and current.get("reason") == reason and current.get("health_known"):
        # Repeated checks must not write provider health again.
        return current
    try:
        health = _health(route)
        before = health.snapshot()
        health_known = True
    except Exception:
        health = None
        health_known = False
        before = {"primary_paused": False, "user_paused": False, "mode": "unknown",
                  "signal": "unavailable", "epoch": None}
    if current.get("active"):
        after = before
        changed = False
        if health_known:
            try:
                changed = health.set_primary_gate(True, reason, owner="vertex_route",
                                                  now=now)
                after = health.snapshot()
            except Exception:
                health_known = False
        if current.get("reason") == reason and not changed and health_known:
            return current
        current.update(reason=reason, updated_at=now, health_known=health_known,
                       health_epoch=after.get("epoch") if health_known else None,
                       health_signal=after.get("signal") if health_known else None)
        if not health_known:
            current["paused_by_this_feature"] = False
        atomic_json(path, current)
        return current
    previous_user_pause = bool(before.get("user_paused", before.get("primary_paused")))
    gate = {
        "schema_version": SCHEMA_VERSION,
        "active": True,
        "applied_by": "vertex_route",
        "reason": reason,
        "at": now,
        "as_of": _now_iso(now),
        "previous_primary_paused": bool(before.get("primary_paused")),
        "previous_user_pause": previous_user_pause,
        "previous_mode": before.get("mode"),
        "previous_signal": before.get("signal"),
        "previous_epoch": before.get("epoch"),
        "previous_fast": bool(before.get("fast")),
        "previous_streak": before.get("streak", 0),
        "paused_by_this_feature": health_known and not previous_user_pause,
        "health_known": False,  # Set true only after the durable DB gate succeeds.
        "restored": False,
        "restored_at": None,
    }
    atomic_json(path, gate)
    if health_known:
        try:
            health.set_primary_gate(True, reason, owner="vertex_route", now=now)
            after = health.snapshot()
            gate.update(health_epoch=after.get("epoch"), health_signal=after.get("signal"),
                        health_known=True)
        except Exception:
            gate.update(health_epoch=None, health_signal=None, health_known=False,
                        paused_by_this_feature=False)
    else:
        gate.update(health_epoch=None, health_signal=None, health_known=False)
    atomic_json(path, gate)
    return gate


def release_primary_gate(route, now=None):
    """Release only the owned Vertex gate; never clear a user pause or mode."""
    now = time.time() if now is None else float(now)
    path = _state_path(route.base, GATE_STATE)
    gate = _read_json(path, {})
    if not gate.get("active"):
        return gate
    if not route.enabled and gate.get("applied_by") != "vertex_route":
        return gate
    health_known = False
    released = False
    after = {}
    try:
        health = _health(route)
        health.set_primary_gate(False, owner="vertex_route", now=now)
        after = health.snapshot()
        health_known = True
        released = not bool(after.get("vertex_gate_active"))
    except Exception:
        health_known = False
        released = False
    gate.update(active=not released, restored=released,
                restored_at=now if released else None, updated_at=now,
                health_known=health_known,
                health_epoch=after.get("epoch") if health_known else None,
                health_signal=after.get("signal") if health_known else None)
    atomic_json(path, gate)
    return gate


def check(base=None, *, route=None, gateway_directory=None, now=None,
          force=False, write_status=True, fetcher=None):
    route = route or load(base, gateway_directory=gateway_directory)
    return route.check(now=now, force=force, write_status=write_status, fetcher=fetcher)


def primary_blocked(base=None, *, now=None, route=None, gateway_directory=None):
    """True only when the trusted switch is enabled and safely blocks primary."""
    route = route or load(base, gateway_directory=gateway_directory)
    if not route.enabled:
        return False
    status = route.check(now=now)
    return bool(status.get("blocked"))


def enforce_primary(base=None, *, now=None, route=None, gateway_directory=None):
    """Raise before any original-account or gateway generation may start."""
    route = route or load(base, gateway_directory=gateway_directory)
    if not route.enabled:
        return None
    status = route.check(now=now)
    if status.get("blocked"):
        if status.get("reason") == "vertex_budget":
            raise VertexBudgetExceeded("vertex_budget")
        raise GatewayUnavailable("gateway_unavailable")
    return status


def console_status(base=None, *, now=None):
    """Read-only console view.  Missing/stale/tampered cache is unknown."""
    now = time.time() if now is None else float(now)
    route = load(base)
    enabled = bool(route.enabled)
    path = _state_path(base, BUDGET_STATE)
    value = _read_json(path, None)
    if not isinstance(value, dict):
        return _unknown_console(base, now, "status_missing", route_enabled=enabled)
    if value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        return _unknown_console(base, now, "status_invalid", route_enabled=enabled)
    if value.get("route_enabled") is not True:
        return _unknown_console(base, now, "route_disabled", route_enabled=enabled)
    try:
        as_of = _iso_utc(value.get("as_of"), "as_of")
        as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except GatewayUnavailable:
        return _unknown_console(base, now, "status_invalid", route_enabled=enabled)
    if now - as_of_dt.timestamp() > STATUS_MAX_AGE_SECONDS or as_of_dt.timestamp() - now > 30:
        return _unknown_console(base, now, "status_stale", route_enabled=enabled)
    status = value.get("status")
    if status is None:
        # Older/simple caches may omit the internal status marker; usage
        # fields still have to pass the full validations below.
        status = "ok" if value.get("spent_usd") is not None else "unknown"
    status = str(status)
    reason = value.get("reason")
    if status not in {"ok", "unknown"}:
        return _unknown_console(base, now, "status_invalid", route_enabled=enabled)
    if status == "unknown":
        return _unknown_console(base, now, str(reason or "gateway_unavailable"),
                                route_enabled=True,
                                blocked=bool(value.get("blocked")),
                                as_of=as_of)
    try:
        spent = _money(_decimal(value.get("spent_usd"), "spent"))
        reserved = _money(_decimal(value.get("reserved_usd"), "reserved"))
        limit, remaining = _monthly_amounts(value, Decimal(spent), Decimal(reserved))
        if value.get("period") != _pacific_month(now):
            raise GatewayUnavailable("period_mismatch")
        reset = _iso_utc(value.get("next_reset_at"), "next_reset_at")
        cap = value.get("official_spend_cap")
        if not isinstance(cap, dict) or type(cap.get("configured")) is not bool:
            raise GatewayUnavailable("cap_invalid")
        name = cap.get("name")
        if name is not None and not isinstance(name, str):
            raise GatewayUnavailable("cap_invalid")
        credits = _credits(value.get("credits"))
    except VertexRouteError:
        return _unknown_console(base, now, "status_invalid", route_enabled=enabled)
    exhausted = remaining is not None and Decimal(remaining) <= 0
    blocked = bool(value.get("blocked"))
    vertex_blocked = bool(value.get("vertex_blocked", blocked)) or exhausted
    active_provider = 'vertex'
    from hakimi.models.video_hydrogen import DUTY_PROVIDERS
    if vertex_blocked and not blocked and value.get('active_provider') in DUTY_PROVIDERS | {'video_hydrogen'}:
        active_provider = value['active_provider']
    elif exhausted:
        blocked = True
    if (route.model == 'gemini-4-argon' and value.get('active_provider') == 'argon_backup'
            and value.get('block_reason') == MONTHLY_EXHAUSTED):
        from hakimi.models.argon_session import backup_config
        if backup_config(route.root):
            blocked = bool(value.get('blocked'))
            active_provider = 'argon_backup'
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "fresh": True,
        "route_enabled": True,
        "blocked": blocked,
        "reason": "vertex_budget" if blocked else None,
        "block_reason": value.get("block_reason") if vertex_blocked else None,
        "project": PROJECT,
        "model": route.model,
        "currency": "USD",
        "period": value.get("period"),
        "timezone": TIMEZONE,
        "limit_usd": limit,
        "spent_usd": spent,
        "reserved_usd": reserved,
        "remaining_usd": remaining,
        "vertex_blocked": vertex_blocked,
        "active_provider": active_provider,
        **({"active_model": value["active_model"]} if active_provider in DUTY_PROVIDERS and "active_model" in value else {}),
        "measurement": MEASUREMENT,
        "price_verified_at": value.get("price_verified_at")
        if value.get("price_verified_at") == PRICE_VERIFIED_AT else PRICE_VERIFIED_AT,
        "official_spend_cap": {"configured": cap["configured"], "name": name},
        "credits": credits,
        "next_reset_at": reset,
        "gateway_as_of": value.get("gateway_as_of"),
        "as_of": as_of,
    }


# Stable names for callers and isolated tests.
VertexRoute = Route
load_route = load
route_for = load
BudgetBlocked = VertexBudgetExceeded


def _unknown_console(base, now, reason, *, route_enabled=False, blocked=False,
                     as_of=None):
    route = load(base) if route_enabled else None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "unknown",
        "fresh": False,
        "route_enabled": bool(route_enabled),
        "blocked": bool(blocked) if route_enabled else False,
        "reason": str(reason)[:80],
        "block_reason": None,
        "project": PROJECT,
        "model": route.model if route else MODEL,
        "currency": "USD",
        "period": _pacific_month(now),
        "timezone": TIMEZONE,
        "limit_usd": _money(route.month_limit) if route and route.month_limit is not None else None,
        "spent_usd": None,
        "reserved_usd": None,
        "remaining_usd": None,
        "measurement": MEASUREMENT,
        "price_verified_at": PRICE_VERIFIED_AT,
        "official_spend_cap": {"configured": False, "name": None},
        "credits": {"official_remaining_usd": None, "source": "not_connected",
                    "note": CREDITS_NOTE},
        "next_reset_at": None,
        "gateway_as_of": None,
        "as_of": as_of or _now_iso(now),
    }
