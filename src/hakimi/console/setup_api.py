"""JSON API behind ``/setup.html``.

All routes are served by the token-protected loopback console; POST requires same-origin.
Responses contain secret *presence* flags only - never secret values.
"""
from __future__ import annotations

import shutil
import subprocess

from hakimi import modules
from hakimi.console.activity import activity_payload  # historical panel API export
from hakimi.console.errors import ApiError
from hakimi.setup import settings as st


def setup_get() -> dict:
    s = st.load()
    from hakimi.gateway import server as gw
    from hakimi.gateway.config import TIER_LABELS
    gateway = gw.status()
    return {
        "settings": s,
        "secrets": st.secret_state(),
        "modules": modules.status_payload()["modules"],
        "gateway": {"initialised": gateway["initialised"], "spent_usd": gateway["spent_usd"],
                    "period": gateway["period"], "requests": gateway["requests"]},
        "tiers": TIER_LABELS,
        "tools": {"dsh": bool(shutil.which("dsh") or st.paths.dsh_bin().joinpath("dsh").is_file()),
                  "codex": bool(shutil.which("codex") or st.paths.codex_executable().is_file()),
                  "systemctl": bool(shutil.which("systemctl")),
                  "qrcode": _has("qrcode"), "ffmpeg": bool(shutil.which("ffmpeg"))},
        "pet_connection": st.pet_connection(s),
        "root": str(st.root()),
    }


def _has(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def setup_post(path: str, data: dict) -> dict:
    if path == "/api/setup":
        try:
            s, new_secrets = st.normalise(data.get("settings") or {})
        except st.SetupError as exc:
            raise ApiError(400, "invalid_setting", f"{exc.message}（{exc.field}）")
        report = st.apply(s, new_secrets)
        restart = modules.units()
        result = {"report": report, "restart_units": restart, "restarted": False,
                  "secrets": st.secret_state(), "pet_connection": st.pet_connection(s)}
        if data.get("restart") and shutil.which("systemctl"):
            result["restarted"] = _restart(restart)
        return result
    if path == "/api/setup/check":
        return _check(data)
    if path == "/api/setup/persona":
        return _persona(data)
    if path == "/api/setup/bilibili-login":
        from hakimi.setup import bilibili
        try:
            if data.get("action") == "start":
                return bilibili.qr_start()
            return bilibili.qr_poll(str(data.get("ticket", "")))
        except ValueError as exc:
            raise ApiError(400, "bilibili_login_failed", str(exc))
        except OSError:
            raise ApiError(502, "bilibili_unreachable", "无法连接 B 站，请检查服务器网络")
    raise ApiError(404, "not_found", "接口不存在")


def _restart(units) -> bool:
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], timeout=15, check=False, capture_output=True)
        for unit in units:
            if unit == "hakimi-memory-console.service":
                continue  # restarting ourselves would cut this response; done last below
            subprocess.run(["systemctl", "--user", "enable", "--now", unit], timeout=30, check=False, capture_output=True)
            subprocess.run(["systemctl", "--user", "restart", unit], timeout=60, check=False, capture_output=True)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _persona(data):
    """list | read | save | reset one editable persona file."""
    from hakimi.persona import catalog
    action, name = data.get("action", "list"), str(data.get("file", ""))
    if action == "list":
        return {"entries": catalog.listing()}
    try:
        if action == "read":
            return catalog.read(name)
        if action == "save":
            result = catalog.save(name, data.get("text"))
        elif action == "reset":
            result = catalog.reset(name)
        else:
            raise ApiError(400, "unknown_action", "未知操作")
    except KeyError:
        raise ApiError(404, "unknown_persona", "没有这个人设文件")
    except ValueError as exc:
        raise ApiError(400, "invalid_persona", str(exc))
    if name.startswith("video/") and modules.is_enabled("video"):
        try:
            st._apply_video(st.root(), st.load())   # point watcher.json at the custom folder
        except Exception:
            pass
    return result


def _check(data):
    """Live checks the user can run from the panel; nothing is saved."""
    target = data.get("target")
    if target == "model":
        from hakimi.gateway import server as gw
        from hakimi.gateway.config import load_config, upstream_key
        config = load_config()
        m = (data.get("model") or {})
        if m.get("provider"):
            config["upstream"]["protocol"] = "openai_compatible" if m["provider"] == "openai_compatible" else "gemini"
        if m.get("base_url"):
            config["upstream"]["base_url"] = st._url(m["base_url"], "model.base_url")
        if m.get("auth"):
            config["upstream"]["auth"] = "bearer" if m["auth"] == "bearer" else "x-goog-api-key"
        if "proxy" in m:
            config["upstream"]["proxy"] = str(m.get("proxy") or "")
        key = str(m.get("api_key") or "").strip() or upstream_key()
        if not key:
            raise ApiError(400, "missing_key", "请先填写 API Key")
        try:
            names = gw.list_models(config, key)
        except Exception as exc:
            code = getattr(exc, "code", None)
            raise ApiError(400, "model_check_failed",
                           "连接失败：" + (f"HTTP {code}（Key 无效或无权限）" if code else exc.__class__.__name__ + "（检查网络/代理）"))
        return {"ok": True, "models": names[:200]}
    if target == "gateway":
        from hakimi.models import vertex_route
        route = vertex_route.load(st.runtime())
        status = route.check(force=True, write_status=False)
        return {"ok": not status.get("blocked"), "status": {k: status.get(k) for k in
                ("status", "blocked", "reason", "block_reason", "spent_usd", "period")}}
    raise ApiError(400, "unknown_check", "未知检测项")
