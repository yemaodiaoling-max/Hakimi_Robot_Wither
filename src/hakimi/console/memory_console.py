"""Loopback-only, token-protected real-time memory and social relations console.

The HTTP service reads the live SQLite database with short timeouts and bounded
queries. Settings and identity changes require authenticated same-origin requests.
Static files contain no data. Start it explicitly:

    python memory_console.py --base . --port 18768 --token-file state/console.token
"""
from __future__ import annotations
from hakimi import paths

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hmac
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit

from hakimi.social.memory_social import (
    HAKIMI_SUBJECT,
    SocialError,
    assessment_evidence,
    ensure_schema,
    familiar_score,
    interaction_stats,
    latest_assessments,
    latest_by_direction,
    pair_familiarity,
    pair_stats,
)
from hakimi.social import identity_links
from hakimi.social import identity_member_context
from hakimi.console.errors import ApiError
from hakimi.console.console_db import DEFAULT_SELF_ID, KINDS, METRICS, SESSIONS, _like, _name_map, _page, _paginate, _path, _self_ids, _session_clause, db_conn, initialise_social, open_db  # noqa: F401 (re-export)
from hakimi.console.console_auth import _chmod_private, _read_token_file, load_or_create_token  # noqa: F401 (re-export)
from hakimi.console.payloads_memory import groups_payload, memories_payload, memory_payload, status_payload, vertex_budget_payload  # noqa: F401 (re-export)
from hakimi.console.payloads_social import _assessments_for, _evidence_details, _graph_metric, _selected_score, _staleness, graph_payload, people_payload, person_payload, relation_payload  # noqa: F401 (re-export)
from hakimi.console.payloads_review_history import review_history_payload
from hakimi.console.payloads_identity import identity_apply_payload, identity_catalog_payload, identity_links_payload, identity_members_payload, identity_memory_payload, identity_preview_payload  # noqa: F401 (re-export)
from hakimi.tools import drawing_accounting, drawing_settings

VERSION = "6.0"
DEFAULT_PORT = 18768
STATIC_DIR = Path(__file__).resolve().parent / "memory_console_static"
STATIC_FILES = {
    "/panel-shell.css": ("panel-shell.css", "text/css; charset=utf-8"),
    "/panel-shell.js": ("panel-shell.js", "text/javascript; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/activity.html": ("activity.html", "text/html; charset=utf-8"),
    "/activity.css": ("activity.css", "text/css; charset=utf-8"),
    "/activity.js": ("activity.js", "application/javascript; charset=utf-8"),
    "/drawing.html": ("drawing.html", "text/html; charset=utf-8"),
    "/drawing.js": ("drawing.js", "text/javascript; charset=utf-8"),
    "/drawing.css": ("drawing.css", "text/css; charset=utf-8"),
    "/messages.html": ("messages.html", "text/html; charset=utf-8"),
    "/messages.js": ("messages.js", "application/javascript"),
    "/messages.css": ("messages.css", "text/css"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/layout.js": ("layout.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/review-history.html": ("review-history.html", "text/html; charset=utf-8"),
    "/review-history.js": ("review-history.js", "text/javascript; charset=utf-8"),
    "/review-history.css": ("review-history.css", "text/css; charset=utf-8"),
    "/identity.html": ("identity.html", "text/html; charset=utf-8"),
    "/identity-member-context.js": ("identity-member-context.js", "text/javascript; charset=utf-8"),
    "/identity.js": ("identity.js", "text/javascript; charset=utf-8"),
    "/identity.css": ("identity.css", "text/css; charset=utf-8"),
    "/vertex-budget.js": ("vertex-budget.js", "text/javascript; charset=utf-8"),
    "/vertex-budget.css": ("vertex-budget.css", "text/css; charset=utf-8"),
    "/setup.html": ("setup.html", "text/html; charset=utf-8"),
    "/setup.css": ("setup.css", "text/css; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
    "/theme.css": ("theme.css", "text/css; charset=utf-8"),
}
MAX_JSON_BODY = 32 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                                "frame-ancestors 'none'; form-action 'none'; object-src 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _with_drawing_quota(base, settings):
    try:
        quota = drawing_accounting.quota_status(base)
    except Exception:
        # An unavailable quota snapshot must not block provider settings.
        quota = None
    return {**settings, "quota": quota}


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, base, token):
        super().__init__(address, ConsoleHandler)
        self.base = str(Path(base).resolve())
        self.token = token
        self.started_at = time.time()


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "HakimiMemoryConsole/" + VERSION
    sys_version = ""

    def log_message(self, format, *args):
        return

    def _headers(self, content_type, length, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()

    def _bytes(self, data, content_type, status=200):
        self._headers(content_type, len(data), status)
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._bytes(data, "application/json; charset=utf-8", status)

    def _error(self, status, code, message):
        self._json({"ok": False, "error": {"code": code, "message": message}}, status)

    def _unauthorised(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer realm="hakimi-memory-console"')
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        body = json.dumps({"ok": False, "error": {
            "code": "unauthorised", "message": "需要本地令牌"}}, ensure_ascii=False).encode()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _same_origin(self, require_origin=False):
        host = self.headers.get("Host", "")
        parsed_host = urlsplit("//" + host)
        host_name = parsed_host.hostname
        if host_name not in {"127.0.0.1", "localhost", "::1"}:
            raise ApiError(403, "host_rejected", "只接受本机Host")
        try:
            host_port = parsed_host.port
        except ValueError:
            raise ApiError(403, "host_rejected", "Host端口无效")
        # An SSH forward uses a different local port. Origin must still match
        # the browser's loopback Host exactly; the listener port is not its URL.
        if host_port is not None and not 1 <= host_port <= 65535:
            raise ApiError(403, "host_rejected", "Host端口无效")
        origin = self.headers.get("Origin")
        if not origin:
            if require_origin:
                raise ApiError(403, "origin_required", "写请求必须携带同源Origin")
            return True
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.netloc.lower() != host.lower():
            raise ApiError(403, "origin_rejected", "只接受同源请求")
        return True

    def _authorised(self):
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            return False
        return hmac.compare_digest(value[7:].strip(), self.server.token)

    def _read_json_body(self):
        content_type = self.headers.get("Content-Type", "")
        if not re.fullmatch(r"application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|UTF-8))?",
                            content_type.strip()):
            raise ApiError(415, "content_type_rejected", "写请求必须使用application/json")
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ApiError(411, "content_length_required", "写请求必须提供Content-Length")
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_content_length", "Content-Length格式无效")
        if length < 0 or length > MAX_JSON_BODY:
            raise ApiError(413, "body_too_large", "写请求正文不能超过32KB")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ApiError(400, "truncated_body", "请求正文不完整")
        try:
            data = json.loads(body.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "malformed_json", "请求正文不是合法UTF-8 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "请求正文必须是JSON对象")
        return data

    def do_GET(self):
        try:
            self._same_origin()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/health":
                self._json({"ok": True, "service": "memory-console", "version": VERSION})
                return
            if path == "/favicon.ico":
                self._bytes(b"", "image/x-icon", 204)
                return
            if not path.startswith("/api/"):
                if path not in STATIC_FILES:
                    self._error(404, "not_found", "页面不存在")
                    return
                name, content_type = STATIC_FILES[path]
                target = (STATIC_DIR / name).resolve()
                if target.parent != STATIC_DIR.resolve() or not target.is_file():
                    self._error(404, "not_found", "页面不存在")
                    return
                self._bytes(target.read_bytes(), content_type)
                return
            if not self._authorised():
                self._unauthorised()
                return
            values = {key: item[-1] for key, item in parse_qs(parsed.query, keep_blank_values=True).items()}
            if path == "/api/status":
                payload = status_payload(self.server.base, self.server.started_at)
            elif path == "/api/activity":
                from hakimi.console.activity import activity_payload
                payload = activity_payload()
            elif path == "/api/setup":
                from hakimi.console.setup_api import setup_get
                payload = setup_get()
            elif path == "/api/modules":
                from hakimi.modules import status_payload as modules_payload
                payload = modules_payload()
            elif path == "/api/messages":
                from hakimi.console.message_log import recent_messages
                session=values.get('session','')
                if session and not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}',session):
                    raise ApiError(400,'invalid_session','会话编号格式无效')
                scope=values.get('scope') or (session.split(':',1)[0] if session else 'group')
                if scope not in ('group','private') or (session and not session.startswith(scope+':')):
                    raise ApiError(400,'invalid_scope','会话类型与会话编号不匹配')
                payload = recent_messages(self.server.base,session=session,scope=scope)
            elif path == "/api/messages/sessions":
                from hakimi.console.message_log import message_sessions
                scope=values.get('scope','group')
                if scope not in ('group','private'):
                    raise ApiError(400,'invalid_scope','会话类型无效')
                payload=message_sessions(self.server.base,scope)
            elif path == "/api/groups":
                payload = groups_payload(self.server.base)
            elif path == "/api/people":
                payload = people_payload(self.server.base, values)
            elif path == "/api/graph":
                payload = graph_payload(self.server.base, values)
            elif path == "/api/person":
                payload = person_payload(self.server.base, values)
            elif path == "/api/relation":
                payload = relation_payload(self.server.base, values)
            elif path == "/api/memories":
                payload = memories_payload(self.server.base, values)
            elif path == "/api/memory":
                payload = memory_payload(self.server.base, values)
            elif path == "/api/identity/catalog":
                payload = identity_catalog_payload(self.server.base)
            elif path == "/api/identity/member-context":
                try:
                    payload = identity_member_context.payload(self.server.base, values)
                except identity_member_context.ContextError as exc:
                    raise ApiError(exc.status, exc.code, exc.message) from exc
            elif path == "/api/identity/members":
                payload = identity_members_payload(self.server.base, values)
            elif path == "/api/identity/links":
                payload = identity_links_payload(self.server.base)
            elif path == "/api/identity/memory":
                payload = identity_memory_payload(self.server.base, values)
            elif path == "/api/review-history":
                payload = review_history_payload(self.server.base, values)
            elif path == "/api/vertex-budget":
                payload = vertex_budget_payload(self.server.base)
            elif path == "/api/drawing":
                payload = _with_drawing_quota(self.server.base, drawing_settings.public_settings(self.server.base))
            else:
                self._error(404, "not_found", "接口不存在")
                return
            self._json({"ok": True, "data": payload})
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except drawing_settings.DrawingError as exc:
            self._error(503, exc.code, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.Error:
            self._error(503, "database_error", "记忆库暂时不可读")
        except (ValueError, KeyError, TypeError):
            self._error(400, "bad_request", "请求参数无效")
        except Exception:
            self._error(500, "internal_error", "服务内部错误")

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path not in {"/api/identity/preview", "/api/identity/apply", "/api/drawing", "/api/setup",
                            "/api/setup/check", "/api/setup/bilibili-login", "/api/setup/persona"}:
                self._method_not_allowed()
                return
            self._same_origin(require_origin=True)
            if not self._authorised():
                self._unauthorised()
                return
            data = self._read_json_body()
            if path.startswith("/api/setup"):
                from hakimi.console.setup_api import setup_post
                payload = setup_post(path, data)
            elif path == "/api/drawing":
                payload = _with_drawing_quota(self.server.base, drawing_settings.save_settings(self.server.base, data))
            elif path == "/api/identity/preview":
                payload = identity_preview_payload(self.server.base, data)
            else:
                payload = identity_apply_payload(self.server.base, data)
            self._json({"ok": True, "data": payload})
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.message)
        except identity_links.IdentityError as exc:
            self._error(exc.status, exc.code, exc.message)
        except drawing_settings.DrawingError as exc:
            status = 503 if exc.code in {"drawing_settings_unreadable", "drawing_settings_write"} else 400
            self._error(status, exc.code, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.Error:
            self._error(503, "database_error", "记忆库暂时不可读")
        except (ValueError, KeyError, TypeError):
            self._error(400, "bad_request", "请求参数无效")
        except Exception:
            self._error(500, "internal_error", "服务内部错误")

    def do_PUT(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def do_OPTIONS(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        body = json.dumps({"ok": False, "error": {"code": "method_not_allowed",
                                                  "message": "该接口不支持此请求方式"}},
                          ensure_ascii=False).encode()
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def create_server(base, *, port=DEFAULT_PORT, token_file=None):
    """Initialise schemas and return a configured loopback server."""
    base = Path(base).resolve()
    initialise_social(base)
    identity_links.ensure_schema(base)
    token_path = Path(token_file) if token_file else base / "state/memory_console.token"
    token = load_or_create_token(token_path)
    return ConsoleServer(("127.0.0.1", int(port)), base, token)


def main(argv=None):
    parser = argparse.ArgumentParser(description="哈基米实时记忆与关系面板（仅限本机）")
    parser.add_argument("--base", default=str(paths.base()))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token-file", default="")
    parser.add_argument("--version", action="version", version="memory-console " + VERSION)
    args = parser.parse_args(argv)
    try:
        server = create_server(args.base, port=args.port, token_file=args.token_file or None)
    except (OSError, ValueError) as exc:
        print("服务未启动：" + str(exc), file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    print(f"记忆面板：http://{host}:{port}/")
    print(f"健康检查：http://{host}:{port}/health")
    print("令牌文件已就绪（内容不打印）；运行 `hakimi setup url` 获取带令牌的面板地址。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
