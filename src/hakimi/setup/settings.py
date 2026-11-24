"""Unified settings: one place for keys, accounts, module switches and auxiliary APIs.

Non-secret values live in ``<root>/hakimi_settings.json`` and ``<root>/aux_config.json``.
Secrets are never stored there:

* model API key       -> ``<gateway>/upstream.key``                         (0600)
* QQ AppSecret,
  fallback/aux keys   -> ``~/.config/hakimi/credentials.fernet``           (Fernet, key 0600)
* Bilibili cookies    -> ``<video>/state/private/auth/cookies.fernet``      (Fernet, own key)
                         and ``<root>/state/bilibili_cookies.dpapi``        (QQ video tools)

``apply()`` then writes the files each module already reads.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets as _secrets
import shutil
import time
from pathlib import Path

from hakimi import modules, paths
from hakimi.models import aux_config

SETTINGS_NAME = "hakimi_settings.json"
CREDENTIALS = Path.home() / ".config" / "hakimi" / "credentials.fernet"
CONSOLE_TOKEN = Path.home() / ".config" / "hakimi" / "memory_console.token"
DEFAULT_TEMPLATES = ("config.json", "group_profiles.json", "codex_help_config.json", "file_send_config.json",
                     "video_tools_config.json", "social_behavior_policy.json", "task_routing.json", "aux_config.json")

AUX_CREDENTIAL_MAP = {
    "review": "review",
    "qq_backup": "qq_backup",
    "context_judge": "context_judge",
    "search_routing": "search_judge",
    "ops": "ops",
}

DEFAULTS = {
    "version": 1,
    "modules": ["core"],
    "bot": {"name": "哈基米✨Gemini"},
    "owner": {"name": "", "aliases": [], "ids": [], "known_bot_ids": []},
    "model": {
        "provider": "gemini",                      # gemini | gemini_compatible | openai_compatible
        "base_url": "https://generativelanguage.googleapis.com",
        "auth": "x-goog-api-key",
        "proxy": "",
        "main_model": "gemini-4-argon",
        "strong_model": "",
        "light_model": "gemini-4-hydrogen",
        "video_model": "gemini-3.5-flash-lite",
        "routing": "single",                       # single | dual (light for chat, strong for hard tasks)
        "monthly_limit_usd": "",
        "prices": {},
    },
    "fallback": {
        "enabled": False,
        "backend": "http_api",                     # http_api | dsh
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
    "aux": {
        "review": copy.deepcopy(aux_config.DEFAULTS["review"]),
        "qq_backup": copy.deepcopy(aux_config.DEFAULTS["qq_backup"]),
        "context_judge": copy.deepcopy(aux_config.DEFAULTS["context_judge"]),
        "search_routing": copy.deepcopy(aux_config.DEFAULTS["search_routing"]),
        "diary": copy.deepcopy(aux_config.DEFAULTS["diary"]),
        "ops": copy.deepcopy(aux_config.DEFAULTS["ops"]),
    },
    "qq": {"app_id": "", "test_group": "", "operation_mode": "test", "allow_private_chat": True,
           "allow_proactive": False, "auto_adapt_groups": True},
    "video": {"priority_room": "", "send_danmaku": False, "comments": False,
              "monthly_usd": "10", "daily_usd": "1"},
    "pet": {"connection": "local", "ssh_host": "", "ssh_port": 22, "ssh_user": "", "ssh_identity": "",
            "ssh_known_hosts": "", "remote_python": "/opt/hakimi/venv/bin/python",
            "remote_app": "/opt/hakimi/app"},
}
SECRET_FIELDS = {
    "model.api_key": "模型 API Key",
    "fallback.api_key": "备用模型 API Key",
    "qq.app_secret": "QQ 机器人 AppSecret",
    "video.sessdata": "B站 SESSDATA",
    "video.bili_jct": "B站 bili_jct",
    "video.dedeuserid": "B站 DedeUserID",
    "aux.review.api_key": "复盘独立 API Key",
    "aux.qq_backup.api_key": "QQ 备用模型 API Key",
    "aux.context_judge.api_key": "上下文筛选 API Key",
    "aux.search_routing.api_key": "搜索分类 API Key",
    "aux.ops.api_key": "运维 Agent API Key",
}
_ID = re.compile(r"^[1-9][0-9]{4,19}$")


class SetupError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field, self.message = field, message


# ------------------------------------------------------------------ files
def root() -> Path:
    return paths.root()


def runtime() -> Path:
    return paths.runtime()


def _atomic_json(path: Path, value, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return copy.deepcopy(default)


def _merge(base, override):
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _strip_secret_path(obj: dict, dotted: str):
    parts = dotted.split(".")
    cur = obj
    for key in parts[:-1]:
        if not isinstance(cur, dict):
            return
        cur = cur.get(key)
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _get_dotted(obj: dict, dotted: str):
    cur = obj
    for key in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def load() -> dict:
    s = _merge(copy.deepcopy(DEFAULTS), _read_json(root() / SETTINGS_NAME, {}))
    if aux_config.has_config(root()):
        live_aux = aux_config.load(root())
        for sub in ("review", "qq_backup", "context_judge", "search_routing", "diary", "ops"):
            if isinstance(live_aux.get(sub), dict):
                _merge(s["aux"][sub], live_aux[sub])
        if isinstance(live_aux.get("fallback"), dict):
            _merge(s["fallback"], live_aux["fallback"])
    return s


def save(value: dict):
    clean = copy.deepcopy(value)
    for dotted in SECRET_FIELDS:
        _strip_secret_path(clean, dotted)
    _atomic_json(root() / SETTINGS_NAME, clean)


# ------------------------------------------------------------------ secrets
def _crypt(data: bytes, protect: bool) -> bytes:
    from hakimi.core.host_secrets import crypt
    return crypt(data, protect)


def credentials() -> dict:
    try:
        return json.loads(_crypt(CREDENTIALS.read_bytes(), False))
    except Exception:
        return {}


def save_credentials(value: dict):
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _crypt(json.dumps(value).encode(), True)
    tmp = CREDENTIALS.with_name(".credentials.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, CREDENTIALS)


def secret_state() -> dict:
    """Which secrets are present - booleans only."""
    from hakimi.gateway.server import upstream_key
    creds = credentials()
    aux_creds = creds.get("aux") or {}
    live_aux = aux_config.load(root()) if aux_config.has_config(root()) else {}
    bili = _bili_auth_dir()
    state = {
        "model.api_key": bool(upstream_key()),
        "fallback.api_key": bool((creds.get("provider") or {}).get("api_key") or ((aux_creds.get("fallback") or {}).get("api_key"))),
        "qq.app_secret": bool(creds.get("app_secret")),
        "video.sessdata": (bili / "cookies.fernet").is_file() or (root() / "state/bilibili_cookies.dpapi").is_file(),
        "video.bili_jct": (bili / "cookies.fernet").is_file(),
        "video.dedeuserid": (bili / "cookies.fernet").is_file(),
    }
    for sub, cred_key in AUX_CREDENTIAL_MAP.items():
        has_vault = bool((aux_creds.get(cred_key) or {}).get("api_key"))
        key_file = str((live_aux.get(sub) or {}).get("api_key_file") or "")
        has_file = bool(key_file and Path(key_file).is_file())
        state[f"aux.{sub}.api_key"] = has_vault or has_file
    return state


# ------------------------------------------------------------------ instance
def init_instance(target: Path | None = None) -> dict:
    """Create the instance folders and copy templates that are missing.  Never overwrites."""
    base = Path(target) if target else root()
    created = []
    for folder in (base, base / "state", base / "stickers", base / paths.RUNTIME_NAME / "state"):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created.append(str(folder))
    for name in DEFAULT_TEMPLATES:
        target_file = base / name
        if not target_file.exists() and paths.default_config(name).is_file():
            shutil.copyfile(paths.default_config(name), target_file)
            created.append(name)
    codex = _read_json(base / "codex_help_config.json", {})
    if codex.get("enabled") and not shutil.which("codex"):
        codex["enabled"] = False  # optional external tool; off unless installed
        _atomic_json(base / "codex_help_config.json", codex)
    if not (base / modules.FILE_NAME).exists():
        modules.save(["core"], base)
        created.append(modules.FILE_NAME)
    from hakimi.gateway.server import init as gateway_init
    created += gateway_init()["created"]
    from hakimi.console.console_auth import load_or_create_token
    load_or_create_token(CONSOLE_TOKEN)
    return {"root": str(base), "created": created}


# ------------------------------------------------------------------ validation
def _digits(value, field, label, required=False):
    value = str(value or "").strip()
    if not value:
        if required:
            raise SetupError(field, f"请填写{label}")
        return ""
    if not _ID.match(value):
        raise SetupError(field, f"{label}应为 5~20 位数字")
    return value


def _money(value, field, label, *, maximum=None):
    value = str(value or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"\d{1,6}(\.\d{1,4})?", value) or float(value) <= 0:
        raise SetupError(field, f"{label}应为正数（美元）")
    if maximum is not None and float(value) > maximum:
        raise SetupError(field, f"{label}不能超过 {maximum}")
    return value


def _model_name(value, field):
    value = str(value or "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", value):
        raise SetupError(field, "模型名称只能包含字母、数字、点、冒号、斜杠、下划线和短横线")
    return value


def _url(value, field, *, allow_local_http=True):
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    if value.startswith("https://") or (allow_local_http and re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", value)):
        if any(c in value for c in "@?#\\ ") or len(value) > 300:
            raise SetupError(field, "地址不能包含账号、参数或空格")
        return value
    raise SetupError(field, "必须是 https:// 地址（本机代理可用 http://127.0.0.1）")


def _secret(value, field, label):
    value = str(value or "").strip()
    if value and (len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise SetupError(field, f"{label}含有不可见字符或空格，请重新粘贴")
    return value


def _normalise_aux(s: dict, current: dict):
    aux = s.setdefault("aux", {})
    cur_aux = current.get("aux") or {}
    # review
    rv = aux.setdefault("review", {})
    rv["enabled"] = bool(rv.get("enabled", True))
    if rv.get("mode") not in aux_config.REVIEW_MODES:
        rv["mode"] = "main_gateway"
    rv["model"] = _model_name(rv.get("model") or "gemini-4-hydrogen", "aux.review.model")
    rv["protocol"] = "gemini" if rv.get("protocol") == "gemini" else "openai_compatible"
    rv["base_url"] = _url(rv.get("base_url"), "aux.review.base_url")
    rv["output_limit_parameter"] = "max_completion_tokens" if rv.get("output_limit_parameter") == "max_completion_tokens" else "max_tokens"
    rv["reasoning_effort"] = rv.get("reasoning_effort") if rv.get("reasoning_effort") in {"", "off", "low", "medium", "high"} else "low"
    rv["credential_key"] = "review"
    for keep in ("api_key_file", "config_path"):
        if not rv.get(keep) and isinstance(cur_aux.get("review"), dict) and cur_aux["review"].get(keep):
            rv[keep] = cur_aux["review"][keep]
    # qq_backup
    qb = aux.setdefault("qq_backup", {})
    qb["enabled"] = bool(qb.get("enabled", False))
    if qb.get("mode") not in aux_config.QQ_BACKUP_MODES:
        qb["mode"] = "disabled"
    qb["protocol"] = "gemini" if qb.get("protocol") == "gemini" else "openai_compatible"
    qb["base_url"] = _url(qb.get("base_url"), "aux.qq_backup.base_url")
    qb["model"] = _model_name(qb.get("model"), "aux.qq_backup.model")
    qb["fallback_model"] = _model_name(qb.get("fallback_model"), "aux.qq_backup.fallback_model")
    qb["output_limit_parameter"] = "max_completion_tokens" if qb.get("output_limit_parameter") == "max_completion_tokens" else "max_tokens"
    qb["reasoning_effort"] = qb.get("reasoning_effort") if qb.get("reasoning_effort") in {"", "off", "low", "medium", "high"} else "low"
    qb["credential_key"] = "qq_backup"
    for keep in ("api_key_file", "config_path"):
        if not qb.get(keep) and isinstance(cur_aux.get("qq_backup"), dict) and cur_aux["qq_backup"].get(keep):
            qb[keep] = cur_aux["qq_backup"][keep]
    # context_judge
    cj = aux.setdefault("context_judge", {})
    if cj.get("mode") not in aux_config.CONTEXT_JUDGE_MODES:
        cj["mode"] = "heuristic"
    cj["protocol"] = "gemini" if cj.get("protocol") == "gemini" else "openai_compatible"
    cj["base_url"] = _url(cj.get("base_url"), "aux.context_judge.base_url")
    cj["model"] = _model_name(cj.get("model") or "gemini-4-hydrogen", "aux.context_judge.model")
    cj["output_limit_parameter"] = "max_completion_tokens" if cj.get("output_limit_parameter") == "max_completion_tokens" else "max_tokens"
    cj["reasoning_effort"] = cj.get("reasoning_effort") if cj.get("reasoning_effort") in {"", "off", "low", "medium", "high"} else "low"
    cj["credential_key"] = "context_judge"
    # search_routing
    sr = aux.setdefault("search_routing", {})
    sr["enabled"] = bool(sr.get("enabled", False))
    if sr.get("judge_mode") not in aux_config.SEARCH_JUDGE_MODES:
        sr["judge_mode"] = "local_rules"
    sr["paid_search_mode"] = "deepseek" if sr.get("paid_search_mode") == "deepseek" else "disabled"
    sr["protocol"] = "gemini" if sr.get("protocol") == "gemini" else "openai_compatible"
    sr["base_url"] = _url(sr.get("base_url"), "aux.search_routing.base_url")
    sr["model"] = _model_name(sr.get("model") or "gemini-4-hydrogen", "aux.search_routing.model")
    sr["credential_key"] = "search_judge"
    # diary
    di = aux.setdefault("diary", {})
    di["fallback_enabled"] = bool(di.get("fallback_enabled", False))
    # ops
    op = aux.setdefault("ops", {})
    for flag in ("watchdog_enabled", "terra_supervision_enabled", "sol_repair_enabled", "codex_help_enabled"):
        op[flag] = bool(op.get(flag, False))
    if op.get("backend") not in aux_config.OPS_BACKENDS:
        op["backend"] = "codex_cli"
    op["protocol"] = "gemini" if op.get("protocol") == "gemini" else "openai_compatible"
    op["base_url"] = _url(op.get("base_url"), "aux.ops.base_url")
    op["model"] = _model_name(op.get("model"), "aux.ops.model")
    op["credential_key"] = "ops"


def normalise(submitted: dict) -> tuple[dict, dict]:
    """Return (settings, new_secrets) from a panel submission; raises SetupError."""
    current = load()
    s = _merge(copy.deepcopy(current), {k: v for k, v in submitted.items() if k in DEFAULTS})
    new_secrets = {}
    for dotted, label in SECRET_FIELDS.items():
        value = _secret(_get_dotted(submitted, dotted), dotted, label)
        if value:
            new_secrets[dotted] = value
        _strip_secret_path(s, dotted)
    s["modules"] = [m for m in dict.fromkeys(["core", *(s.get("modules") or [])]) if m in modules.BY_KEY]
    s["bot"]["name"] = str(s["bot"].get("name") or DEFAULTS["bot"]["name"]).strip()[:40]
    owner = s["owner"]
    owner["name"] = str(owner.get("name") or "").strip()[:40]
    owner["aliases"] = [str(a).strip()[:40] for a in _list(owner.get("aliases")) if str(a).strip()][:8]
    owner["ids"] = [_digits(i, "owner.ids", "主人 ID") for i in _list(owner.get("ids"))][:6]
    owner["known_bot_ids"] = [_digits(i, "owner.known_bot_ids", "机器人 ID") for i in _list(owner.get("known_bot_ids"))][:32]
    m = s["model"]
    if m.get("provider") not in ("gemini", "gemini_compatible", "openai_compatible"):
        m["provider"] = "gemini"
    m["base_url"] = _url(m.get("base_url"), "model.base_url") or DEFAULTS["model"]["base_url"]
    m["auth"] = "bearer" if (m.get("auth") == "bearer" or m["provider"] == "openai_compatible") else "x-goog-api-key"
    m["proxy"] = str(m.get("proxy") or "").strip()
    if m["proxy"] and not re.fullmatch(r"(https?|socks5h?)://[A-Za-z0-9.\-]+:\d{2,5}", m["proxy"]):
        raise SetupError("model.proxy", "代理格式如 http://127.0.0.1:7890")
    for key in ("main_model", "strong_model", "light_model", "video_model"):
        m[key] = _model_name(m.get(key), "model." + key)
    if not m["main_model"]:
        raise SetupError("model.main_model", "请填写主回复模型")
    m["routing"] = "dual" if m.get("routing") == "dual" and m["strong_model"] else "single"
    m["monthly_limit_usd"] = _money(m.get("monthly_limit_usd"), "model.monthly_limit_usd", "月度上限")
    prices = {}
    for name, row in (m.get("prices") or {}).items():
        if _model_name(name, "model.prices") and isinstance(row, dict):
            prices[name] = {"input_usd_per_million": _money(row.get("input_usd_per_million"), "model.prices", "输入单价") or "0",
                            "output_usd_per_million": _money(row.get("output_usd_per_million"), "model.prices", "输出单价") or "0"}
    m["prices"] = prices
    f = s["fallback"]
    f["enabled"] = bool(f.get("enabled"))
    f["backend"] = "dsh" if f.get("backend") == "dsh" else "http_api"
    f["protocol"] = f.get("protocol") if f.get("protocol") in aux_config.PROTOCOLS else "openai_compatible"
    f["base_url"] = _url(f.get("base_url"), "fallback.base_url") or DEFAULTS["fallback"]["base_url"]
    f["model"] = _model_name(f.get("model") or DEFAULTS["fallback"]["model"], "fallback.model")
    _normalise_aux(s, current)
    q = s["qq"]
    q["app_id"] = _digits(q.get("app_id"), "qq.app_id", "AppID", required="qq" in s["modules"])
    q["test_group"] = _digits(q.get("test_group"), "qq.test_group", "测试群号", required="qq" in s["modules"])
    q["operation_mode"] = "normal" if q.get("operation_mode") == "normal" else "test"
    for key in ("allow_private_chat", "allow_proactive", "auto_adapt_groups"):
        q[key] = bool(q.get(key))
    v = s["video"]
    room = str(v.get("priority_room") or "").strip()
    if room and not room.isdigit():
        raise SetupError("video.priority_room", "直播间号应为数字（不需要盯直播就留空）")
    v["priority_room"] = room
    v["send_danmaku"], v["comments"] = bool(v.get("send_danmaku")), bool(v.get("comments"))
    v["monthly_usd"] = _money(v.get("monthly_usd"), "video.monthly_usd", "视频月预算", maximum=10) or "10"
    v["daily_usd"] = _money(v.get("daily_usd"), "video.daily_usd", "视频日预算", maximum=1) or "1"
    if v["send_danmaku"] and not room:
        raise SetupError("video.send_danmaku", "发送弹幕需要先填写直播间号")
    p = s["pet"]
    p["connection"] = "ssh" if p.get("connection") == "ssh" else "local"
    try:
        p["ssh_port"] = int(p.get("ssh_port") or 22)
    except (TypeError, ValueError):
        raise SetupError("pet.ssh_port", "SSH 端口应为数字")
    for key in ("ssh_host", "ssh_user", "ssh_identity", "ssh_known_hosts", "remote_python", "remote_app"):
        p[key] = str(p.get(key) or "").strip()[:300]
    if p["connection"] == "ssh" and "pet" in s["modules"]:
        if not re.fullmatch(r"[A-Za-z0-9.\-:]{1,253}", p["ssh_host"]):
            raise SetupError("pet.ssh_host", "请填写服务器地址（域名或 IP）")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", p["ssh_user"]):
            raise SetupError("pet.ssh_user", "请填写 SSH 用户名")
    if "model.api_key" not in new_secrets and not secret_state()["model.api_key"]:
        raise SetupError("model.api_key", "请填写模型 API Key")
    if "qq" in s["modules"] and "qq.app_secret" not in new_secrets and not secret_state()["qq.app_secret"]:
        raise SetupError("qq.app_secret", "启用 QQ 机器人需要 AppSecret")
    bili = {k for k in ("video.sessdata", "video.bili_jct", "video.dedeuserid") if k in new_secrets}
    if bili and not {"video.sessdata", "video.dedeuserid"} <= bili:
        raise SetupError("video.sessdata", "B站登录需要同时填写 SESSDATA 与 DedeUserID（发弹幕/评论还需要 bili_jct）")
    if "video.dedeuserid" in new_secrets and not new_secrets["video.dedeuserid"].isdigit():
        raise SetupError("video.dedeuserid", "DedeUserID 应为数字")
    return s, new_secrets


def _list(value):
    if isinstance(value, str):
        return [x for x in re.split(r"[,，\s]+", value) if x]
    return list(value or [])


# ------------------------------------------------------------------ apply
def _bili_auth_dir() -> Path:
    return modules.video_dir() / "state" / "private" / "auth"


def apply(s: dict, new_secrets: dict, *, run_prepare=True) -> list:
    """Write every module's configuration from the unified settings.  Returns step reports."""
    report = []

    def step(name, fn):
        try:
            detail = fn()
            report.append({"step": name, "ok": True, "detail": detail or ""})
        except Exception as exc:  # each step is independent; never echo secrets
            message = getattr(exc, "message", None) or exc.__class__.__name__
            if isinstance(exc, (OSError, ValueError)) and not isinstance(exc, SetupError):
                message = f"{exc.__class__.__name__}: {str(exc)[:160]}"
            report.append({"step": name, "ok": False, "detail": message})

    base = root()
    init_instance(base)
    step("保存设置", lambda: save(s))
    step("模块开关", lambda: "、".join(modules.save(s["modules"], base)))
    step("主人与机器人身份", lambda: _apply_owner(base, s))
    step("模型网关", lambda: _apply_gateway(s, new_secrets))
    step("主回复路由", lambda: _apply_route(base, s))
    step("密钥保管", lambda: _apply_credentials(s, new_secrets))
    step("独立辅助配置", lambda: _apply_aux(base, s))
    step("机器人配置", lambda: _apply_bot_config(base, s))
    if "qq" in s["modules"]:
        step("QQ 官方通道", lambda: _apply_qq(base, s, run_prepare))
    if "video" in s["modules"] or any(k.startswith("video.") for k in new_secrets):
        step("B站登录", lambda: _apply_bilibili_login(base, new_secrets))
    if "video" in s["modules"]:
        step("视频机器人", lambda: _apply_video(base, s))
    return report


def _apply_owner(base, s):
    o, ids = s["owner"], s["owner"]["ids"]
    value = {"name": o["name"], "aliases": o["aliases"], "ids": ids,
             "pet_session": ("private:" + sorted(ids, key=len, reverse=True)[0]) if ids else "",
             "bot_self_id": "", "known_bot_ids": o["known_bot_ids"]}
    _atomic_json(base / "owner.json", value)
    policy = _read_json(base / "social_behavior_policy.json", {"version": 1, "protected_user_ids": []})
    policy["protected_user_ids"] = sorted(set(policy.get("protected_user_ids", [])) | set(ids))
    _atomic_json(base / "social_behavior_policy.json", policy)
    return "已写入 owner.json"


def _apply_gateway(s, new_secrets):
    from hakimi.gateway import server as gw
    gw.init()
    config = gw.load_config()
    m = s["model"]
    protocol = "openai_compatible" if m.get("provider") == "openai_compatible" else "gemini"
    config["upstream"].update(protocol=protocol, base_url=m["base_url"], auth=m["auth"], proxy=m["proxy"])
    light = m["light_model"] or m["main_model"]
    config["models"] = {
        "gemini-4-argon": m["main_model"],
        "gemini-4-carbon": m["strong_model"] or m["main_model"],
        "gemini-4-hydrogen": light,
        "gemini-3.8-flash": light,
        "gemini-3.5-flash-lite": m["video_model"] or light,
    }
    config["prices"] = m["prices"]
    config["monthly_limit_usd"] = m["monthly_limit_usd"] or None
    gw.save_config(config)
    if "model.api_key" in new_secrets:
        gw.save_upstream_key(new_secrets["model.api_key"])
    return f"上游 {m['base_url']}（{protocol}）"


def _apply_route(base, s):
    from hakimi.gateway.server import directory
    _atomic_json(base / "state" / "vertex_route.json", {
        "enabled": True, "gateway_directory": str(directory()), "model": "gemini-4-argon",
        "month_limit_usd": None, "fallback": "dsh"})
    routing = ({"mode": "dual", "single_model": "gemini-4-argon", "dual_model": "gemini-4-carbon"}
               if s["model"]["routing"] == "dual" else {"mode": "single", "single_model": "gemini-4-argon"})
    _atomic_json(base / "task_routing.json", routing)
    return "双模型分流" if routing["mode"] == "dual" else "单模型"


def _apply_credentials(s, new_secrets):
    creds = credentials()
    provider = dict(creds.get("provider") or {})
    provider.setdefault("api_key", "")
    provider["base_url"] = s["fallback"]["base_url"]
    if "fallback.api_key" in new_secrets:
        provider["api_key"] = new_secrets["fallback.api_key"]
    creds["provider"] = provider
    creds.setdefault("app_secret", "")
    if "qq.app_secret" in new_secrets:
        creds["app_secret"] = new_secrets["qq.app_secret"]
    aux_creds = dict(creds.get("aux") or {})
    for sub, cred_key in AUX_CREDENTIAL_MAP.items():
        field = f"aux.{sub}.api_key"
        if field in new_secrets:
            entry = dict(aux_creds.get(cred_key) or {})
            entry["api_key"] = new_secrets[field]
            aux_creds[cred_key] = entry
    if "fallback.api_key" in new_secrets:
        entry = dict(aux_creds.get("fallback") or {})
        entry["api_key"] = new_secrets["fallback.api_key"]
        aux_creds["fallback"] = entry
    creds["aux"] = aux_creds
    save_credentials(creds)
    return "已加密保存"


def _apply_aux(base, s):
    existing = aux_config.load(base)
    payload = copy.deepcopy(existing)
    aux_in = s.get("aux") or {}
    for sub in ("review", "qq_backup", "context_judge", "search_routing", "diary", "ops"):
        if isinstance(aux_in.get(sub), dict):
            _merge(payload[sub], aux_in[sub])
    _merge(payload["fallback"], s.get("fallback") or {})
    aux_config.save(payload, base)

    codex_path = base / "codex_help_config.json"
    codex_cfg = _read_json(codex_path, None) or _read_json(paths.default_config("codex_help_config.json"), {"enabled": False})
    codex_cfg["enabled"] = bool(payload["ops"].get("codex_help_enabled", False))
    _atomic_json(codex_path, codex_cfg)

    search_path = base / "search_routing.json"
    search_cfg = _read_json(search_path, {}) or {}
    search_cfg["enabled"] = bool(payload["search_routing"].get("enabled", False))
    search_cfg.setdefault("daily_limit_usd", "0.10")
    search_cfg.setdefault("monthly_limit_usd", "1.00")
    search_cfg.setdefault("max_web_calls", 6)
    search_cfg.setdefault("total_web_seconds", 40)
    search_cfg.setdefault("priority_reserve_usd", "0.0015")
    _atomic_json(search_path, search_cfg)
    return "aux_config.json 已更新"


def _apply_bot_config(base, s):
    config = _read_json(base / "config.json", None) or _read_json(paths.default_config("config.json"), {})
    profile = config.setdefault("bot_profile", {})
    profile["name"] = s["bot"]["name"]
    profile["allow_private_chat"] = s["qq"]["allow_private_chat"]
    fb = s["fallback"]
    backend = fb.get("backend", "http_api")
    has_dsh = bool(shutil.which("dsh") or (paths.dsh_bin() / "dsh").exists())
    creds = credentials()
    has_key = bool((creds.get("provider") or {}).get("api_key") or ((creds.get("aux") or {}).get("fallback") or {}).get("api_key"))
    if backend == "http_api":
        ready = fb.get("protocol") == "main_gateway" or has_key
    else:
        ready = has_dsh and has_key
    config.setdefault("fallback", {})["enabled"] = bool(fb["enabled"] and ready)
    _atomic_json(base / "config.json", config)
    note = f"备用模型已启用（{backend}）" if config["fallback"]["enabled"] else "备用模型未启用"
    if fb["enabled"] and backend == "dsh" and not has_dsh:
        note += "（未找到 dsh 命令，见 docs/modules.md）"
    elif fb["enabled"] and not ready:
        note += "（未填写备用 API Key）"
    return note


def _apply_qq(base, s, run_prepare):
    q = s["qq"]
    _atomic_json(base / "official_test_config.json", {
        "version": 1, "enabled": True, "app_id": q["app_id"], "test_group_number": q["test_group"],
        "allow_proactive": q["allow_proactive"], "operation_mode": q["operation_mode"],
        "auto_adapt_groups": q["auto_adapt_groups"], "allow_private_chat": q["allow_private_chat"]})
    if not run_prepare:
        return "已写入 official_test_config.json"
    from hakimi.transport.official_test import prepare
    prepare(base)
    return "运行目录已生成"


def _apply_bilibili_login(base, new_secrets):
    if "video.sessdata" not in new_secrets:
        return "未修改"
    from hakimi.setup.bilibili import save_cookie_values
    return save_cookie_values(new_secrets["video.sessdata"], new_secrets["video.dedeuserid"],
                              new_secrets.get("video.bili_jct", ""))


def _apply_video(base, s):
    v = s["video"]
    from hakimi.gateway.server import CA_NAME, KEY_NAME, directory
    video = modules.video_dir()
    state = video / "state"
    room = v["priority_room"] or "0"
    watcher = {
        "account_id": "hakimi-bilibili", "priority_room": room,
        "state_dir": str(state / "watch"), "native_database": str(state / "native_watch.sqlite3"),
        "bridge_database": str(state / "bridge" / "bilibili_bridge.sqlite3"),
        "auth_state_dir": str(state / "private" / "auth"),
        "gateway_key_file": str(directory() / KEY_NAME), "gateway_ca_file": str(directory() / CA_NAME),
        "persona_dir": str(__import__("hakimi.persona.catalog", fromlist=["x"]).video_persona_dir()),
        "monitor_seconds": 120, "chunk_seconds": 30, "live_observation_seconds": 90,
        "video_observation_seconds": 180, "video_native_vertex_model": "gemini-3.5-flash-lite",
        "budget_scope": "autonomous_video_only", "monthly_usd": v["monthly_usd"], "daily_usd": v["daily_usd"],
        "send_enabled": bool(v["send_danmaku"] and v["priority_room"]), "comments_enabled": v["comments"],
        "max_video_chunks": 4, "cooldown_seconds": 90, "hourly_danmaku_limit": 12,
        "evidence_max_age_seconds": 60, "live_guaranteed_hours": 2 if v["priority_room"] else 0,
        "live_daily_reserve_usd": "0.16" if v["priority_room"] else "0",
    }
    previous = _read_json(video / "deploy" / "watcher.json", {})
    for key, value in previous.items():   # keep operator-tuned extra keys
        if key != "persona_dir":
            watcher.setdefault(key, value)
    _atomic_json(video / "deploy" / "watcher.json", watcher, private=True)
    owner_ids = s["owner"]["ids"]
    bridge = {
        "state_dir": str(state / "bridge"), "qq_identity": "hakimi-qq", "bilibili_identity": "hakimi-bilibili",
        "qq_database": str(runtime() / "state" / "memory.sqlite3"),
        "bilibili_database": str(state / "native_watch.sqlite3"),
        "watcher_database": str(state / "watch" / "watcher.sqlite3"),
        "bilibili_account_id": "hakimi-bilibili",
        "owner_private_session": ("private:" + owner_ids[0]) if owner_ids else "private:0",
        "autobiography_snapshot": str(runtime() / "state" / "bili_autobiography.json"),
        "presence_snapshot": str(runtime() / "state" / "hakimi_presence.json"),
    }
    _atomic_json(video / "deploy" / "bridge.json", bridge, private=True)
    for folder in ("watch", "bridge", "private/auth"):
        (state / folder).mkdir(parents=True, exist_ok=True)
    return "watcher.json / bridge.json 已写入"


def pet_connection(s: dict) -> dict:
    """``desktop_pet/pet_connection.json`` for the Windows client."""
    p = s["pet"]
    if p["connection"] == "local":
        return {"mode": "local"}
    return {"mode": "ssh", "host": p["ssh_host"], "port": p["ssh_port"], "user": p["ssh_user"],
            "identity": p["ssh_identity"], "known_hosts": p["ssh_known_hosts"],
            "remote_command": f"HAKIMI_ROOT={root()} {p['remote_python']} -u {p['remote_app']}/desktop_pet_agent.py"}


def summary() -> dict:
    s = load()
    return {"root": str(root()), "modules": modules.status_payload(), "secrets": secret_state(),
            "configured": {"model": secret_state()["model.api_key"], "qq": bool(s["qq"]["app_id"]),
                           "owner": bool(s["owner"]["ids"])}, "updated_at": time.time()}


def new_token() -> str:
    return _secrets.token_urlsafe(32)
