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
import re
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from hakimi.gateway.config import (
    CA_NAME,
    CONFIG_NAME,
    DEFAULT_CONFIG,
    INTERNAL_MODELS,
    KEY_NAME,
    PROVISION_NAME,
    TIER_LABELS,
    TLS_CERT_NAME,
    TLS_KEY_NAME,
    UPSTREAM_KEY_NAME,
    directory,
    init,
    load_config,
    save_config,
    save_upstream_key,
    upstream_key,
)
from hakimi.gateway.ledger import (
    LEDGER_NAME,
    MONTHLY_EXHAUSTED,
    TIMEZONE,
    budget_payload,
    month_spend,
    month_tokens,
    monthly_budget_exhausted,
    next_reset,
    period,
    record,
)

# Keep the original server imports available to existing callers.
__all__ = [
    "CA_NAME", "CONFIG_NAME", "DEFAULT_CONFIG", "GatewayHandler", "GatewayServer", "HOST",
    "INTERNAL_MODELS", "KEY_NAME", "LEDGER_NAME", "MAX_BODY", "MONTHLY_EXHAUSTED", "PORT",
    "PROVISION_NAME", "TIER_LABELS", "TIMEZONE", "TLS_CERT_NAME", "TLS_KEY_NAME",
    "UPSTREAM_KEY_NAME", "budget_payload", "directory", "init", "list_models", "load_config",
    "main", "month_spend", "month_tokens", "next_reset", "period", "record", "save_config",
    "save_upstream_key", "serve", "status", "upstream_key",
]

HOST = "127.0.0.1"
PORT = 18765
MAX_BODY = 64 * 1024 * 1024
_PATH = re.compile(r"^/v1beta/models/([A-Za-z0-9._-]{1,80}):(generateContent|streamGenerateContent|countTokens)$")


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
    if upstream.get("protocol") == "openai_compatible":
        from hakimi.gateway import openai_compat
        return openai_compat.list_openai_models(base_url, key, _opener(config), timeout=timeout)
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
        if method != "countTokens" and monthly_budget_exhausted(self.server.base, config):
            return self._send(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": MONTHLY_EXHAUSTED}}, headers)
        key = upstream_key(self.server.base)
        if not key:
            return self._error(503, "upstream_key_missing", headers)
        stream = method == "streamGenerateContent"
        upstream_cfg = config.get("upstream") or {}
        is_openai = upstream_cfg.get("protocol") == "openai_compatible"
        name_map = {}
        if is_openai:
            if method == "countTokens":
                return self._send(200, {"totalTokens": max(1, len(body) // 4)}, headers)
            base_url = str(upstream_cfg.get("base_url") or "").strip().rstrip("/")
            if not base_url.startswith("https://") and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base_url):
                return self._error(400, "upstream_must_be_https", headers)
            try:
                gemini_payload = json.loads(body or b"{}")
            except ValueError:
                return self._error(400, "invalid_json", headers)
            from hakimi.gateway import openai_compat
            openai_body, name_map = openai_compat.gemini_to_openai_request(gemini_payload, upstream_model, stream=stream)
            req_headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
            if stream:
                req_headers["Accept"] = "text/event-stream"
            request = Request(
                openai_compat.openai_chat_url(base_url),
                data=json.dumps(openai_body, ensure_ascii=False).encode("utf-8"),
                headers=req_headers,
                method="POST",
            )
        else:
            try:
                request = _upstream_request(config, key, upstream_model, method, body, "?alt=sse" if stream else "")
            except ValueError as exc:
                return self._error(400, str(exc), headers)
        if not self.server.slots.acquire(timeout=30):
            return self._error(429, "gateway_busy", headers)
        try:
            timeout = float(upstream_cfg.get("timeout_seconds") or 240)
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
                usage = None
                if is_openai:
                    from hakimi.gateway import openai_compat
                    if stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Connection", "close")
                        for k, v in headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        if upstream.headers.get_content_type() == "text/event-stream":
                            usage_holder = {}
                            for chunk in openai_compat.iter_openai_sse_as_gemini_sse(
                                upstream, upstream_model, name_map=name_map, usage_out=usage_holder
                            ):
                                try:
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError):
                                    break
                            usage = usage_holder.get("usageMetadata")
                        else:
                            raw_data = upstream.read(MAX_BODY)
                            try:
                                gemini_resp = openai_compat.openai_response_to_gemini(
                                    json.loads(raw_data), upstream_model, name_map=name_map
                                )
                                usage = gemini_resp.get("usageMetadata")
                                self.wfile.write(("data: " + json.dumps(gemini_resp, ensure_ascii=False) + "\n\n").encode("utf-8"))
                                self.wfile.flush()
                            except (ValueError, BrokenPipeError, ConnectionResetError):
                                pass
                    else:
                        raw_data = upstream.read(MAX_BODY)
                        try:
                            gemini_resp = openai_compat.openai_response_to_gemini(
                                json.loads(raw_data), upstream_model, name_map=name_map
                            )
                        except ValueError:
                            return self._error(502, "upstream_invalid_response", headers)
                        usage = gemini_resp.get("usageMetadata")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Connection", "close")
                        for k, v in headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        try:
                            self.wfile.write(json.dumps(gemini_resp, ensure_ascii=False).encode("utf-8"))
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
                    self.send_header("Connection", "close")
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
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
    now = time.time()
    return {
        "directory": str(base), "initialised": (base / KEY_NAME).exists() and (base / CA_NAME).exists(),
        "upstream": config["upstream"].get("base_url"), "upstream_key": bool(upstream_key(base)),
        "models": config.get("models"), "monthly_limit_usd": config.get("monthly_limit_usd"),
        "period": period(now), "spent_usd": str(month_spend(base, now)), **month_tokens(base, now),
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
