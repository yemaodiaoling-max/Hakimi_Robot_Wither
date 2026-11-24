"""Bilibili login for the setup panel: pasted cookies or QR-code login.

Cookies are written to two encrypted stores, each with its own key:

* the video module vault  ``<video>/state/private/auth/{vault.key,cookies.fernet}``
  (format of ``bili_companion.platform.auth``), and
* the QQ bot's video-tool vault ``<root>/state/bilibili_cookies.dpapi``
  (``hakimi.tools.bilibili_login``; Fernet on Linux, DPAPI on Windows).

Only ``SESSDATA``, ``DedeUserID`` and (for danmaku/comments) ``bili_jct`` are kept.
The values never leave this machine except in requests to ``*.bilibili.com``.
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from hakimi import modules, paths

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"
_PENDING = {}
_LOCK = threading.Lock()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
      "Referer": "https://www.bilibili.com/"}


def _rows(sessdata, uid, bili_jct=""):
    # Browsers show SESSDATA URL-encoded (``%2C``); a decoded comma is not a valid cookie value.
    sessdata = urllib.parse.quote(sessdata, safe="%*-._~")
    expires = int(time.time()) + 180 * 86400
    rows = [dict(name="SESSDATA", value=sessdata, domain=".bilibili.com", path="/", secure=True, expires=expires),
            dict(name="DedeUserID", value=str(uid), domain=".bilibili.com", path="/", secure=True, expires=expires)]
    if bili_jct:
        rows.append(dict(name="bili_jct", value=bili_jct, domain=".bilibili.com", path="/", secure=True, expires=expires))
    return rows


def _private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def check_login(rows) -> dict:
    """Fixed read-only request: is this cookie set logged in, and as the expected uid?"""
    jar = http.cookiejar.CookieJar()
    for row in rows:
        jar.set_cookie(http.cookiejar.Cookie(0, row["name"], row["value"], None, False, row["domain"], True, True,
                                             row["path"], True, True, row["expires"], False, None, None, {}))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(urllib.request.Request(NAV, headers=UA), timeout=12) as response:
            data = json.loads(response.read(65536)).get("data") or {}
    except Exception:
        return {"checked": False, "logged_in": None}
    uid = next((r["value"] for r in rows if r["name"] == "DedeUserID"), "")
    return {"checked": True, "logged_in": bool(data.get("isLogin")),
            "same_account": str(data.get("mid", "")) == str(uid), "uname": str(data.get("uname", ""))[:40]}


def save_rows(rows, *, verify=True) -> str:
    names = {r["name"] for r in rows}
    if not {"SESSDATA", "DedeUserID"} <= names:
        raise ValueError("bilibili_cookie_incomplete")
    result = check_login(rows) if verify else {"checked": False}
    if result.get("checked") and not result.get("logged_in"):
        raise ValueError("B站返回未登录：Cookie 已失效或填写有误")
    if result.get("checked") and not result.get("same_account"):
        raise ValueError("DedeUserID 与登录账号不一致")
    uid = next(r["value"] for r in rows if r["name"] == "DedeUserID")
    saved = []
    # 1) video module vault (independent key)
    if modules.installed("video") or modules.video_dir().exists():
        from cryptography.fernet import Fernet
        auth = modules.video_dir() / "state" / "private" / "auth"
        key_path = auth / "vault.key"
        if not key_path.exists():
            _private_write(key_path, Fernet.generate_key())
        payload = dict(version=1, expected_uid=str(int(uid)), cookies=rows, imported_at=time.time())
        _private_write(auth / "cookies.fernet", Fernet(key_path.read_bytes()).encrypt(json.dumps(payload).encode()))
        saved.append("视频模块")
    # 2) QQ bot video tools
    from hakimi.core.host_secrets import crypt
    root = paths.root()
    _private_write(root / "state" / "bilibili_cookies.dpapi", crypt(json.dumps(rows).encode(), True))
    saved.append("QQ 视频工具")
    who = f"（{result['uname']}）" if result.get("uname") else ""
    return "已保存到" + "、".join(saved) + who + ("" if result.get("checked") else "；暂时无法联网核对登录状态")


def save_cookie_values(sessdata, uid, bili_jct="") -> str:
    return save_rows(_rows(sessdata.strip(), uid.strip(), bili_jct.strip()))


def _qr_svg(text) -> str | None:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def qr_start() -> dict:
    with urllib.request.urlopen(urllib.request.Request(QR_GENERATE, headers=UA), timeout=10) as response:
        data = json.loads(response.read(65536)).get("data") or {}
    url, key = data.get("url"), data.get("qrcode_key")
    if not url or not key:
        raise ValueError("B站二维码获取失败，请稍后重试")
    ticket = os.urandom(12).hex()
    with _LOCK:
        for old in [k for k, v in _PENDING.items() if time.time() - v["at"] > 300]:
            _PENDING.pop(old, None)
        _PENDING[ticket] = {"key": key, "at": time.time()}
    return {"ticket": ticket, "image": _qr_svg(url), "url": None if _qr_svg(url) else url, "expires_in": 180}


def qr_poll(ticket: str) -> dict:
    with _LOCK:
        pending = _PENDING.get(str(ticket))
    if not pending:
        return {"state": "expired", "message": "二维码已过期，请重新获取"}
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url = QR_POLL + "?" + urllib.parse.urlencode({"qrcode_key": pending["key"]})
    with opener.open(urllib.request.Request(url, headers=UA), timeout=10) as response:
        data = json.loads(response.read(65536)).get("data") or {}
    code = data.get("code")
    if code == 86101:
        return {"state": "waiting", "message": "等待扫码"}
    if code == 86090:
        return {"state": "scanned", "message": "已扫码，请在手机上确认"}
    if code == 86038:
        with _LOCK:
            _PENDING.pop(str(ticket), None)
        return {"state": "expired", "message": "二维码已过期，请重新获取"}
    if code != 0:
        return {"state": "error", "message": "B站返回状态 " + str(code)}
    values = {c.name: c.value for c in jar if c.domain.lstrip(".").endswith("bilibili.com")}
    if "SESSDATA" not in values and data.get("url"):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(data["url"]).query))
        for name in ("SESSDATA", "bili_jct", "DedeUserID"):
            values.setdefault(name, params.get(name, ""))
    if not values.get("SESSDATA") or not values.get("DedeUserID"):
        return {"state": "error", "message": "登录成功但没有拿到 Cookie，请改用手动填写"}
    with _LOCK:
        _PENDING.pop(str(ticket), None)
    message = save_rows(_rows(values["SESSDATA"], values["DedeUserID"], values.get("bili_jct", "")))
    return {"state": "done", "message": message}
