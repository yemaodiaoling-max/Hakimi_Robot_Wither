"""Private settings for the native Gemini drawing provider."""

from collections.abc import Mapping
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

from hakimi.core.runtime_io import atomic_replace
from hakimi.tools.drawing_accounting import validate_limits


DEFAULT_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.1-flash-image"
DRAWING_TIMEOUT_SECONDS = 180
DEFAULT_FALLBACKS = ("gemini-nano-banana-2.1", "gemini-3.1-flash-image-preview",
                     "gemini-3-pro-image", "gemini-3-pro-image-preview")
_LIMIT_FIELDS = ("daily_usd_limit", "monthly_usd_limit", "reserve_usd")
_DEFAULTS = {"enabled": False, "endpoint": DEFAULT_ENDPOINT, "model": DEFAULT_MODEL,
             "fallback_models": DEFAULT_FALLBACKS,
             "api_key": "", **dict.fromkeys(_LIMIT_FIELDS)}
_SAVE_LOCK = threading.Lock()
_MODEL = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_FULL_PATH = re.compile(rf"(?P<root>.*)/models/(?P<model>{_MODEL}):generateContent")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class DrawingError(RuntimeError):
    """A user-readable failure whose text is safe to return to QQ or the console."""

    def __init__(self, code, message, *, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.models_tried = ()
        self.receipt = None


def _endpoint_error():
    return DrawingError("drawing_endpoint", "请填写公网 HTTPS 的 Gemini API 地址。")


def public_ip(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.scope_id or ip.is_site_local:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in ipaddress.ip_network("64:ff9b::/96"):
            # The well-known NAT64 prefix can otherwise hide a local IPv4 target.
            ip = ipaddress.IPv4Address(int(ip) & 0xffffffff)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _parse_endpoint(value):
    if not isinstance(value, str):
        raise _endpoint_error()
    value = value.strip()
    if not re.fullmatch(r"[\x21-\x7e]{1,2048}", value) or any(ch in value for ch in "\\?#@"):
        raise _endpoint_error()
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".")
        port = parsed.port
    except ValueError:
        raise _endpoint_error() from None
    if parsed.scheme != "https" or not host or port == 0:
        raise _endpoint_error()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (len(host) > 253 or len(labels) < 2 or not any(ch.isalpha() for ch in labels[-1])
                or any(not _HOST_LABEL.fullmatch(label) for label in labels)):
            raise _endpoint_error() from None
        authority = host
    else:
        if not public_ip(host):
            raise _endpoint_error()
        authority = f"[{literal.compressed}]" if literal.version == 6 else literal.compressed
    if port not in (None, 443):
        authority += f":{port}"
    path = parsed.path.rstrip("/")
    if (not re.fullmatch(r"[/A-Za-z0-9._~:-]*", path)
            or any(part in {".", ".."} for part in path.split("/")) or "//" in path):
        raise _endpoint_error()
    return urlunsplit(("https", authority, path, "", ""))


def request_path(path, model):
    if full := _FULL_PATH.fullmatch(path):
        if full.group("model") != model:
            raise DrawingError("drawing_model_path", "完整接口地址中的模型名应与绘图模型一致。")
        return path
    if ":" in path:
        raise _endpoint_error()
    if path.endswith("/models"):
        return f"{path}/{model}:generateContent"
    if not re.search(r"/v\d+(?:alpha|beta)?\d*$", path):
        path += "/v1beta"
    return f"{path}/models/{model}:generateContent"


def _model_name(value):
    if isinstance(value, str):
        value = value.strip().removeprefix("models/")
        if re.fullmatch(_MODEL, value):
            return value
    raise DrawingError("drawing_model", "请输入有效的 Gemini 绘图模型名。")


def _validate(values, previous):
    enabled = values.get("enabled", previous["enabled"])
    if type(enabled) is not bool:
        raise DrawingError("drawing_enabled", "请选择是否启用绘图。")
    endpoint = _parse_endpoint(values.get("endpoint", previous["endpoint"]))
    parsed = urlsplit(endpoint)
    model = _model_name(values.get("model", previous["model"]))
    request_path(parsed.path, model)
    fallbacks = values.get("fallback_models", previous["fallback_models"])
    if not isinstance(fallbacks, (list, tuple)) or len(fallbacks) > 8:
        raise DrawingError("drawing_fallbacks", "备用绘图模型最多可填写八个。")
    models = dict.fromkeys(_model_name(item) for item in fallbacks)
    models.pop(model, None)

    api_key = values.get("api_key", "")
    if isinstance(api_key, str):
        api_key = api_key.strip()
    if not isinstance(api_key, str) or (api_key and not re.fullmatch(r"[\x21-\x7e]{1,2048}", api_key)):
        raise DrawingError("drawing_key_format", "绘图 API 密钥的格式无效。")
    if previous["api_key"] and not api_key and parsed.netloc != urlsplit(previous["endpoint"]).netloc:
        raise DrawingError("drawing_key_origin", "更换绘图 API 主机或端口时，请重新输入密钥。")
    api_key = api_key or previous["api_key"]
    if enabled and not api_key:
        raise DrawingError("drawing_key_required", "请先保存绘图 API 密钥。")
    # Protect against accidentally pasting the stored credential into a visible field.
    if api_key and (api_key == model or api_key in models or api_key in (parsed.hostname or "").split(".")
                    or api_key in parsed.path.split("/")):
        raise DrawingError("drawing_key_location", "请将绘图密钥填写在专用密钥栏。")
    try:
        limits = validate_limits({field: values.get(field, previous[field]) for field in _LIMIT_FIELDS})
    except ValueError:
        raise DrawingError("drawing_budget_config", "请检查绘图费用上限和单次预留金额。") from None
    return {"enabled": enabled, "endpoint": endpoint, "model": model, "fallback_models": list(models),
            "api_key": api_key, **limits}


def _settings_path(base):
    return Path(base) / "state" / "drawing_provider.json"


def load_settings(base):
    try:
        with _settings_path(base).open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return _validate(data, _DEFAULTS)
    except FileNotFoundError:
        return {**_DEFAULTS, "fallback_models": list(DEFAULT_FALLBACKS)}
    except (OSError, ValueError, RecursionError, DrawingError):
        raise DrawingError("drawing_settings_unreadable", "绘图配置读取失败，请在控制台检查。") from None


def _public(settings):
    return {"enabled": settings["enabled"], "endpoint": settings["endpoint"], "model": settings["model"],
            "fallback_models": settings["fallback_models"],
            "has_api_key": bool(settings["api_key"]), **{field: settings[field] for field in _LIMIT_FIELDS}}


def public_settings(base):
    return _public(load_settings(base))


def save_settings(base, values):
    if not isinstance(values, Mapping):
        raise DrawingError("drawing_settings", "绘图设置格式无效。")
    path = _settings_path(base)
    with _SAVE_LOCK:
        settings = _validate(values, load_settings(base))
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".drawing-provider-", delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                json.dump(settings, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace(temporary, path)
        except OSError:
            raise DrawingError("drawing_settings_write", "绘图设置保存失败，请检查服务权限。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return _public(settings)
