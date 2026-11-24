"""Open-source local gateway compatible with Hakimi's model clients.

    hakimi gateway init      # create local key + TLS certificate (idempotent)
    hakimi gateway serve     # listen on https://127.0.0.1:18765
    hakimi gateway status    # configuration summary and this month's spend (no secrets)
    hakimi gateway models    # list models the upstream key can use

Files in the gateway directory (``HAKIMI_GATEWAY_DIR``, default ``~/.local/share/hakimi-gateway``):

=========================  ==========================================================
``gateway.key``            local bearer key the Hakimi clients send (0600)
``gateway-cert.pem``       local CA certificate the clients trust
``gateway-tls-*.pem``      server certificate chain and key (0600)
``gateway.json``           upstream URL, model mapping, prices, monthly cap
``upstream.key``           your upstream API key, written by the setup panel (0600)
``gateway_ledger.sqlite3`` token usage and estimated cost per request
=========================  ==========================================================

Only loopback connections are accepted.  Request/response bodies are never logged.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from hakimi import paths

HOST = "127.0.0.1"
PORT = 18765
KEY_NAME = "gateway.key"
CA_NAME = "gateway-cert.pem"
TLS_CERT_NAME = "gateway-tls-cert.pem"
TLS_KEY_NAME = "gateway-tls-key.pem"
CONFIG_NAME = "gateway.json"
UPSTREAM_KEY_NAME = "upstream.key"
LEDGER_NAME = "gateway_ledger.sqlite3"
PROVISION_NAME = "gateway_provision.json"
MAX_BODY = 64 * 1024 * 1024
TIMEZONE = "America/Los_Angeles"
MONTHLY_EXHAUSTED = "vertex_monthly_budget_exhausted"
# Internal tier names used by the Hakimi clients; each maps to a real upstream model.
INTERNAL_MODELS = ("gemini-4-argon", "gemini-4-carbon", "gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite")
TIER_LABELS = {
    "gemini-4-argon": "主回复模型",
    "gemini-4-carbon": "强推理模型（详细推导、考据、解题）",
    "gemini-4-hydrogen": "轻量模型（复盘、梳理、桌宠、视频选片）",
    "gemini-3.8-flash": "轻量模型（旧名称，同上）",
    "gemini-3.5-flash-lite": "视频观察模型（看视频、直播抽帧）",
}
DEFAULT_CONFIG = {
    "version": 1,
    "upstream": {
        "base_url": "https://generativelanguage.googleapis.com",
        "api_version": "v1beta",
        "auth": "x-goog-api-key",   # or "bearer" for Gemini-compatible proxies
        "proxy": "",                # e.g. http://127.0.0.1:7890 ; empty = system proxy settings
        "timeout_seconds": 240,
    },
    "models": {name: name for name in INTERNAL_MODELS},
    "prices": {},                   # upstream model -> {"input_usd_per_million": "0.3", "output_usd_per_million": "2.5"}
    "monthly_limit_usd": None,      # None = no cap (spend is still recorded)
    "max_concurrency": 4,
}
_PATH = re.compile(r"^/v1beta/models/([A-Za-z0-9._-]{1,80}):(generateContent|streamGenerateContent|countTokens)$")
_LEDGER_LOCK = threading.Lock()


def directory() -> Path:
    return paths.gateway_dir()


def _private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config(base: Path | None = None) -> dict:
    base = base or directory()
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        value = json.loads((base / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(config.get(key), dict):
            config[key].update(item)
        else:
            config[key] = item
    return config


def save_config(config: dict, base: Path | None = None):
    base = base or directory()
    _private_write(base / CONFIG_NAME, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())


def save_upstream_key(value: str, base: Path | None = None):
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("upstream_key_invalid")
    _private_write((base or directory()) / UPSTREAM_KEY_NAME, value.encode())


def upstream_key(base: Path | None = None) -> str:
    try:
        return ((base or directory()) / UPSTREAM_KEY_NAME).read_text(encoding="ascii").strip()
    except OSError:
        return ""


# ------------------------------------------------------------------ init / certificates
def init(base: Path | None = None) -> dict:
    """Create the local key, CA and server certificate when missing.  Never overwrites."""
    base = base or directory()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []
    if not (base / KEY_NAME).exists():
        _private_write(base / KEY_NAME, secrets.token_urlsafe(36).encode())
        created.append(KEY_NAME)
    if not (base / CA_NAME).exists() or not (base / TLS_CERT_NAME).exists() or not (base / TLS_KEY_NAME).exists():
        ca_pem, chain_pem, key_pem = _make_certificates()
        _private_write(base / CA_NAME, ca_pem)
        _private_write(base / TLS_CERT_NAME, chain_pem)
        _private_write(base / TLS_KEY_NAME, key_pem)
        created += [CA_NAME, TLS_CERT_NAME, TLS_KEY_NAME]
    if not (base / CONFIG_NAME).exists():
        save_config(load_config(base), base)
        created.append(CONFIG_NAME)
    from hakimi.models.vertex_route import PROJECT
    _private_write(base / PROVISION_NAME, json.dumps({"project": PROJECT, "kind": "hakimi-open-gateway"}).encode())
    return {"directory": str(base), "created": created}


def _make_certificates():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hakimi local gateway CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_pem = ca.public_bytes(pem)
    chain = leaf.public_bytes(pem) + ca_pem
    key_pem = leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return ca_pem, chain, key_pem


# ------------------------------------------------------------------ ledger
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
            db.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (request_id, time.time(), period(), scope, model, upstream_model, prompt, output, cached,
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


def budget_payload(base: Path, config: dict, now=None) -> dict:
    """Document accepted by ``hakimi.models.vertex_route.validate_budget_payload``."""
    from hakimi.models.vertex_route import PROJECT
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


# ------------------------------------------------------------------ upstream
def _opener(config):
    proxy = (config.get("upstream") or {}).get("proxy") or ""
    return build_opener(ProxyHandler({"http": proxy, "https": proxy}) if proxy else ProxyHandler())


def _upstream_request(config, key, upstream_model, method, body, query=""):
    upstream = config["upstream"]
    base_url = str(upstream.get("base_url") or DEFAULT_CONFIG["upstream"]["base_url"]).rstrip("/")
    if not base_url.startswith("https://") and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base_url):
        raise ValueError("upstream_must_be_https")
    url = f"{base_url}/{upstream.get('api_version', 'v1beta')}/models/{upstream_model}:{method}{query}"
    headers = {"Content-Type": "application/json"}
    if upstream.get("auth") == "bearer":
        headers["Authorization"] = "Bearer " + key
    else:
        headers["x-goog-api-key"] = key
    return Request(url, data=body, headers=headers, method="POST")


def list_models(config=None, key=None, timeout=15):
    """Model names the upstream key can call (for the setup panel)."""
    config = config or load_config()
    key = key or upstream_key()
    upstream = config["upstream"]
    base_url = str(upstream.get("base_url")).rstrip("/")
    request = Request(f"{base_url}/{upstream.get('api_version', 'v1beta')}/models?pageSize=200", method="GET",
                      headers={"Authorization": "Bearer " + key} if upstream.get("auth") == "bearer" else {"x-goog-api-key": key})
    with _opener(config).open(request, timeout=timeout) as response:
        data = json.loads(response.read(4 * 1024 * 1024))
    names = []
    for row in data.get("models", []):
        name = str(row.get("name", "")).removeprefix("models/")
        methods = row.get("supportedGenerationMethods") or []
        if name and (not methods or "generateContent" in methods):
            names.append(name)
    return sorted(set(names))


# ------------------------------------------------------------------ HTTP server
class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, base: Path):
        super().__init__(address, GatewayHandler)
        self.base = base
        self.key = (base / KEY_NAME).read_text(encoding="ascii").strip()
        self.slots = threading.BoundedSemaphore(max(1, int(load_config(base).get("max_concurrency") or 4)))


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "HakimiGateway/1"
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _send(self, status, payload, extra=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, message, extra=None):
        self._send(status, {"error": {"code": status, "message": message}}, extra)

    def _authorised(self):
        value = self.headers.get("Authorization", "")
        return value.startswith("Bearer ") and hmac.compare_digest(value[7:].strip(), self.server.key)

    def do_GET(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._error(403, "loopback_only")
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if not self._authorised():
            return self._error(401, "unauthorised")
        if self.path == "/budget":
            return self._send(200, budget_payload(self.server.base, load_config(self.server.base)))
        return self._error(404, "not_found")

    def do_POST(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._error(403, "loopback_only")
        if not self._authorised():
            return self._error(401, "unauthorised")
        path, _, query = self.path.partition("?")
        match = _PATH.match(path)
        if not match:
            return self._error(404, "not_found")
        model, method = match.groups()
        config = load_config(self.server.base)
        models = config.get("models") or {}
        upstream_model = models.get(model) or (model if config.get("allow_any_model") else None)
        if not upstream_model:
            return self._error(400, "model_not_configured")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._error(413, "body_too_large")
        body = self.rfile.read(length)
        request_id = uuid.uuid4().hex
        headers = {"X-Gateway-Request-Id": request_id, "X-Gateway-Model": model,
                   "X-Gateway-Requested-Model": model}
        if method != "countTokens":
            limit = _limit(config)
            if limit is not None and month_spend(self.server.base) >= limit:
                return self._send(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": MONTHLY_EXHAUSTED}}, headers)
        key = upstream_key(self.server.base)
        if not key:
            return self._error(503, "upstream_key_missing", headers)
        stream = method == "streamGenerateContent"
        try:
            request = _upstream_request(config, key, upstream_model, method, body, "?alt=sse" if stream else "")
        except ValueError as exc:
            return self._error(400, str(exc), headers)
        if not self.server.slots.acquire(timeout=30):
            return self._error(429, "gateway_busy", headers)
        try:
            timeout = float((config.get("upstream") or {}).get("timeout_seconds") or 240)
            try:
                upstream = _opener(config).open(request, timeout=timeout)
            except HTTPError as exc:
                detail = exc.read(64 * 1024)
                try:
                    payload = json.loads(detail)
                except ValueError:
                    payload = {"error": {"code": exc.code, "message": "upstream_http_" + str(exc.code)}}
                record(self.server.base, config, request_id=request_id, model=model, upstream_model=upstream_model,
                       usage=None, status=exc.code)
                return self._send(exc.code, payload, headers)
            except (URLError, TimeoutError, OSError):
                return self._error(502, "upstream_unavailable", headers)
            with upstream:
                self.send_response(200)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                self.send_header("Connection", "close")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                usage = None
                if stream:
                    while True:
                        line = upstream.readline()
                        if not line:
                            break
                        if line.startswith(b"data:"):
                            try:
                                usage = json.loads(line[5:]).get("usageMetadata") or usage
                            except ValueError:
                                pass
                        try:
                            self.wfile.write(line)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                else:
                    data = upstream.read(MAX_BODY)
                    try:
                        usage = json.loads(data).get("usageMetadata")
                    except ValueError:
                        pass
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                if method != "countTokens":
                    record(self.server.base, config, request_id=request_id, model=model,
                           upstream_model=upstream_model, usage=usage, status=200)
        finally:
            self.server.slots.release()


def serve(base: Path | None = None, port: int = PORT):
    base = base or directory()
    init(base)
    server = GatewayServer((HOST, port), base)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(base / TLS_CERT_NAME), str(base / TLS_KEY_NAME))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"Hakimi gateway: https://{HOST}:{port}  (dir {base})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def status(base: Path | None = None) -> dict:
    base = base or directory()
    config = load_config(base)
    return {
        "directory": str(base), "initialised": (base / KEY_NAME).exists() and (base / CA_NAME).exists(),
        "upstream": config["upstream"].get("base_url"), "upstream_key": bool(upstream_key(base)),
        "models": config.get("models"), "monthly_limit_usd": config.get("monthly_limit_usd"),
        "period": period(), "spent_usd": str(month_spend(base)), **month_tokens(base),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hakimi gateway", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["init", "serve", "status", "models"])
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    base = args.dir.resolve() if args.dir else directory()
    if args.action == "init":
        print(json.dumps(init(base), ensure_ascii=False))
    elif args.action == "serve":
        serve(base, args.port)
    elif args.action == "status":
        print(json.dumps(status(base), ensure_ascii=False, indent=2))
    else:
        print("\n".join(list_models(load_config(base), upstream_key(base))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
