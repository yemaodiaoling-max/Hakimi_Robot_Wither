"""Explicit same-account import and fixed, read-only Bilibili login check.

Run with ``python -m bili_companion.platform.auth``. No QQ runtime imports.
Cookie values, account IDs, keys and upstream bodies never enter CLI output.
"""
import argparse
from contextlib import closing
import http.cookiejar
import json
import math
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
COOKIE_NAMES = {"SESSDATA", "DedeUserID"}


class AuthError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fernet(key):
    try:
        from cryptography.fernet import Fernet
        return Fernet(key)
    except ImportError:
        raise AuthError("dependency_missing") from None
    except Exception:
        raise AuthError("invalid_vault") from None


def _decrypt(key, ciphertext):
    try:
        return json.loads(_fernet(key).decrypt(ciphertext))
    except AuthError:
        raise
    except Exception:
        raise AuthError("invalid_vault") from None


def _allowed_domain(domain):
    return (isinstance(domain, str) and
            (domain.lower().lstrip(".") in {"bilibili.com", "b23.tv"}
             or domain.lower().endswith(".bilibili.com")))


def filter_cookies(rows, *, now=None, include_csrf=False):
    """Only login identity cookies; no CSRF, unrelated sites or expired rows."""
    now = time.time() if now is None else now
    if not isinstance(rows, list) or len(rows) > 10000:
        raise AuthError("invalid_vault")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not _allowed_domain(row.get("domain")):
            continue
        if row.get("name") not in (COOKIE_NAMES | ({"bili_jct"} if include_csrf else set())):
            continue
        value, domain = row.get("value"), row["domain"].lower()
        path, expires = row.get("path", "/"), row.get("expires")
        if (not isinstance(value, str) or not value or len(value) > 8192
                or any(ord(c) < 33 or ord(c) > 126 or c in ';,' for c in value)
                or not isinstance(path, str) or not path.startswith("/")
                or any(ord(c) < 32 for c in path)):
            raise AuthError("invalid_vault")
        if expires is not None and (type(expires) not in (int, float)
                                    or not math.isfinite(expires) or expires <= now):
            continue
        result.append(dict(name=row["name"], value=value, domain=domain,
                           path=path, secure=True, expires=expires))
    if not any(row["name"] == "SESSDATA" and "bilibili.com" in row["domain"] for row in result):
        raise AuthError("expired")
    return result


def _expected_uid(rows):
    values = {r["value"] for r in rows if r["name"] == "DedeUserID"
              and (r["domain"].lstrip(".") == "bilibili.com" or r["domain"].endswith(".bilibili.com"))}
    if len(values) != 1 or not next(iter(values)).isdigit() or int(next(iter(values))) <= 0:
        raise AuthError("mismatch")
    return str(int(next(iter(values))))


def cookie_jar(rows, *, include_csrf=False):
    """Reusable in-memory jar, with unrelated and CSRF cookies removed."""
    jar = http.cookiejar.CookieJar()
    for row in filter_cookies(rows, include_csrf=include_csrf):
        jar.set_cookie(http.cookiejar.Cookie(
            0, row["name"], row["value"], None, False, row["domain"],
            row["domain"].startswith("."), row["domain"].startswith("."),
            row["path"], True, True, row["expires"], row["expires"] is None,
            None, None, {}))
    return jar


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AuthError("redirect")


def check_nav(jar, expected_uid):
    """The only network operation: HTTPS GET to a fixed endpoint, no redirects."""
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(NAV_URL, method="GET", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with closing(opener.open(request, timeout=12)) as response:
            if response.geturl() != NAV_URL:
                raise AuthError("redirect")
            raw = response.read(65537)
        if len(raw) > 65536:
            raise AuthError("network")
        body = json.loads(raw)
        if body.get("code") == -101 or body.get("data", {}).get("isLogin") is False:
            raise AuthError("expired")
        if body.get("code") != 0 or body.get("data", {}).get("isLogin") is not True:
            raise AuthError("network")
        mid = body["data"].get("mid")
        if type(mid) is not int or mid <= 0 or str(mid) != expected_uid:
            raise AuthError("mismatch")
        return {"logged_in": True, "same_account": True}
    except AuthError:
        raise
    except Exception:
        raise AuthError("network") from None


def _paths(state_dir):
    directory = Path(state_dir).absolute()
    if directory.is_symlink():
        raise AuthError("unsafe_path")
    for name in ("vault.key", "cookies.fernet"):
        if (directory / name).is_symlink():
            raise AuthError("unsafe_path")
    return directory, directory / "vault.key", directory / "cookies.fernet"


def _atomic_private(path, content):
    fd, name = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_cookie_jar(state_dir, *, include_csrf=False):
    """Load the independent vault only; no implicit QQ or guest fallback."""
    _, key, vault = _paths(state_dir)
    try:
        payload = _decrypt(key.read_bytes(), vault.read_bytes())
    except OSError:
        raise AuthError("not_configured") from None
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or not isinstance(payload.get("expected_uid"), str)):
        raise AuthError("invalid_vault")
    rows = filter_cookies(payload.get("cookies"), include_csrf=include_csrf)
    if _expected_uid(rows) != payload["expected_uid"]:
        raise AuthError("mismatch")
    return cookie_jar(rows, include_csrf=include_csrf), payload["expected_uid"]


def status(state_dir):
    jar, expected = load_cookie_jar(state_dir)
    return check_nav(jar, expected)


def import_qq(source_vault, source_key, state_dir, *, include_csrf=False):
    """Validate before persisting, re-encrypt with a separate key, revalidate."""
    directory, key_path, vault_path = _paths(state_dir)
    source_vault, source_key = Path(source_vault).resolve(), Path(source_key).resolve()
    if (directory.resolve() in {source_vault.parent, source_key.parent}
            or key_path.resolve() in {source_vault, source_key}
            or vault_path.resolve() in {source_vault, source_key}):
        raise AuthError("unsafe_path")
    try:
        rows = filter_cookies(_decrypt(source_key.read_bytes(), source_vault.read_bytes()), include_csrf=include_csrf)
    except OSError:
        raise AuthError("source_unavailable") from None
    expected = _expected_uid(rows)
    check_nav(cookie_jar(rows), expected)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if not key_path.exists():
        from cryptography.fernet import Fernet
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
    os.chmod(key_path, 0o600)
    key = key_path.read_bytes()
    if key == source_key.read_bytes():
        raise AuthError("key_not_independent")
    payload = dict(version=1, expected_uid=expected, cookies=rows, imported_at=time.time())
    _atomic_private(vault_path, _fernet(key).encrypt(json.dumps(payload, allow_nan=False).encode()))
    return status(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import-qq")
    imp.add_argument("--source-vault", required=True)
    imp.add_argument("--source-key", required=True)
    imp.add_argument("--state-dir", required=True)
    imp.add_argument("--include-csrf", action="store_true", help="Import Bilibili CSRF cookie for explicitly enabled sending")
    stat = commands.add_parser("status")
    stat.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if os.name != "posix":
            raise AuthError("unsupported_platform")
        result = (import_qq(args.source_vault, args.source_key, args.state_dir, include_csrf=args.include_csrf)
                  if args.command == "import-qq" else status(args.state_dir))
        print(json.dumps({"state": "logged_in", **result}))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, AuthError) else "local_error"
        print(json.dumps(dict(state=code, logged_in=False, same_account=False)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
