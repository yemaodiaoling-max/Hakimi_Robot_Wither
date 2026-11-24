"""Generate one QQ-ready image via the configured native Gemini endpoint."""

import base64
import binascii
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import http.client
from io import BytesIO
import json
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlsplit

from PIL import Image

from hakimi.tools.drawing_accounting import BudgetError, finish, reserve
from hakimi.tools.drawing_settings import DrawingError, load_settings, public_ip, public_settings, request_path, save_settings


__all__ = ["DrawingError", "GeneratedImage", "generate", "public_settings", "save_settings"]

CREATIVE_PREFIX = """请把下面这份由哈基米构思的画面说明画成一张完成的作品。优先遵循指定的主题和风格，保留她选定的主体、动作、关系与情绪，让她特意选的视觉细节承载故事的转折或余韵。
用构图和光线引导视线，把最重要的瞬间放在一眼能读懂的位置。安排清楚的前景、主体和环境层次，选取能呼应情绪的色彩、材质和光影。通过姿态、表情、物件和留白讲故事，让每处细节服务于这一幕。画面适合时，可以用一个让人好奇的小发现、一点俏皮的视觉反差，或一个嘴上嫌弃却悄悄照顾对方的举动来传达哈基米的性格。只有画面说明明确要求时才加入文字。

哈基米的画面说明
"""

_MAX_BRIEF = 3000
_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_BASE64 = ((_MAX_IMAGE_BYTES + 2) // 3) * 4
_MAX_RESPONSE_BYTES = _MAX_BASE64 + 1024 * 1024
_MAX_IMAGE_EDGE = 8192
_MAX_IMAGE_PIXELS = 24_000_000
_ADDRESS_CONNECT_TIMEOUT = 8
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_BLOCKED = {"SAFETY", "IMAGE_SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "IMAGE_PROHIBITED_CONTENT", "SPII"}
_DEFINITE_REJECTIONS = {400, 401, 403, 404, 413, 415, 422, 429}


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def _public_addresses(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not public_ip(row[4][0]) for row in addresses):
        raise DrawingError("drawing_endpoint", "绘图 API 主机必须解析到公网地址。")
    return addresses


def _connect(addresses, context, host, deadline):
    for family, socktype, proto, _, address in addresses:
        with socket.socket(family, socktype, proto) as raw:
            raw.settimeout(min(_remaining(deadline), _ADDRESS_CONNECT_TIMEOUT))
            try:
                raw.connect(address)
                raw.settimeout(min(_remaining(deadline), _ADDRESS_CONNECT_TIMEOUT))
                secure_socket = context.wrap_socket(raw, server_hostname=host)
            except OSError:
                _remaining(deadline)
                continue
            return secure_socket
    raise OSError("drawing_connection_failed")


def _abort(connection_socket):
    with suppress(OSError):
        connection_socket.shutdown(socket.SHUT_RDWR)


@contextmanager
def _https_connection(endpoint, deadline):
    host, port = endpoint.hostname, endpoint.port or 443
    addresses = _public_addresses(host, port)
    tls = ssl.create_default_context()
    with _connect(addresses, tls, host, deadline) as connection_socket:
        connection_socket.settimeout(_remaining(deadline))
        # Use the resolved address directly with TLS verified for the original hostname.
        # The HTTP client will neither re-resolve nor proxy or follow redirects.
        connection = http.client.HTTPSConnection(host, port, context=tls)
        connection.sock = connection_socket
        timer = threading.Timer(_remaining(deadline), _abort, (connection_socket,))
        timer.daemon = True
        timer.start()
        try:
            yield connection
        finally:
            timer.cancel()
            connection.close()


def _http_error(status):
    if status in {401, 403}:
        message = "绘图服务拒绝了鉴权，请检查密钥和模型权限。"
        code = "drawing_auth"
    elif status == 429:
        message = "绘图服务的调用额度暂时不足。"
        code = "drawing_provider_limit"
    elif status in {400, 404, 413, 415, 422}:
        message = "绘图服务拒绝了请求，请检查 API 地址和模型配置。"
        code = "drawing_provider_request"
    else:
        message = "绘图服务暂时未能完成请求。"
        code = "drawing_provider"
    return DrawingError(code, message, http_status=status)


def _response_json(connection, path, key, payload):
    connection.request("POST", path, body=payload, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Accept-Encoding": "identity", "x-goog-api-key": key,
    })
    with connection.getresponse() as response:
        if not 200 <= response.status < 300:
            raise _http_error(response.status)
        if ((response.length is not None and response.length > _MAX_RESPONSE_BYTES)
                or response.getheader("Content-Encoding", "identity").lower() != "identity"):
            raise DrawingError("drawing_response", "绘图服务返回的数据格式或大小超出支持范围。")
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise DrawingError("drawing_response", "绘图服务返回的数据过大。")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise DrawingError("drawing_response", "绘图服务返回了无法读取的数据。") from None
    if not isinstance(data, dict):
        raise DrawingError("drawing_response", "绘图服务返回了无法读取的数据。")
    return data


def _valid_envelope(data, image_format):
    if image_format == "PNG":
        return data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"\0\0\0\0IEND\xaeB`\x82")
    if image_format == "JPEG":
        return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    return (len(data) > 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") == len(data) - 8)


def _decode_image(blob):
    mime = blob.get("mimeType", blob.get("mime_type"))
    if mime == "image/jpg":
        mime = "image/jpeg"
    encoded = blob.get("data")
    if not isinstance(mime, str) or not isinstance(encoded, str) or not 0 < len(encoded) <= _MAX_BASE64:
        return None
    image_format = _FORMATS.get(mime)
    if not image_format:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
        if len(data) > _MAX_IMAGE_BYTES or not _valid_envelope(data, image_format):
            return None
        with Image.open(BytesIO(data), formats=[image_format]) as image:
            width, height = image.size
            if (not 0 < min(width, height) <= max(width, height) <= _MAX_IMAGE_EDGE
                    or width * height > _MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1):
                return None
            image.verify()
        with Image.open(BytesIO(data), formats=[image_format]) as image:
            image.load()
    except (binascii.Error, ValueError, OSError, SyntaxError, EOFError, IndexError, struct.error, Image.DecompressionBombError):
        return None
    return GeneratedImage(data, mime)


def _pick_image(data):
    feedback = data.get("promptFeedback", data.get("prompt_feedback"))
    if isinstance(feedback, dict) and feedback.get("blockReason", feedback.get("block_reason")) not in (None, "", "BLOCK_REASON_UNSPECIFIED"):
        raise DrawingError("drawing_blocked", "绘图服务未能通过这份画面说明。")
    candidates = data.get("candidates")
    blocked = invalid = False
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        reason = candidate.get("finishReason", candidate.get("finish_reason"))
        if isinstance(reason, str) and reason in _BLOCKED:
            blocked = True
        if reason not in (None, "", "STOP", "FINISH_REASON_UNSPECIFIED"):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict) or part.get("thought"):
                continue
            blob = part.get("inlineData", part.get("inline_data"))
            if isinstance(blob, dict):
                if result := _decode_image(blob):
                    return result
                invalid = True
    if blocked:
        raise DrawingError("drawing_blocked", "绘图服务未能通过这份画面说明。")
    if invalid:
        raise DrawingError("drawing_invalid_image", "绘图服务返回的图片无法安全读取。")
    raise DrawingError("drawing_no_image", "绘图服务没有返回可发送的图片。")


def generate(base, creative_brief, *, timeout=90):
    """Submit just the creative brief and built-in direction, without chat history."""
    brief = creative_brief.strip() if isinstance(creative_brief, str) else ""
    if not 0 < len(brief) <= _MAX_BRIEF:
        raise DrawingError("drawing_brief", "请提供一段不超过 3000 字的画面说明。")
    settings = load_settings(base)
    if not settings["enabled"] or not settings["api_key"]:
        raise DrawingError("drawing_disabled", "绘图还未启用，请在控制台设置专用 API。")
    endpoint = urlsplit(settings["endpoint"])
    path = request_path(endpoint.path, settings["model"])
    payload = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": CREATIVE_PREFIX + brief}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "candidateCount": 1},
    }).encode("utf-8")

    receipt = call_id = None
    rejected = attempted = False
    deadline = time.monotonic() + timeout
    try:
        try:
            call_id = reserve(base, settings)
            with _https_connection(endpoint, deadline) as connection:
                _remaining(deadline)
                attempted = True
                receipt = _response_json(connection, path, settings["api_key"], payload)
        except DrawingError as exc:
            rejected = exc.http_status in _DEFINITE_REJECTIONS
            raise
        except (OSError, http.client.HTTPException):
            if time.monotonic() >= deadline:
                raise DrawingError("drawing_timeout", "绘图服务响应超时。") from None
            raise DrawingError("drawing_connection", "绘图服务连接失败。") from None
        finally:
            if call_id is not None:
                if attempted:
                    finish(base, call_id, receipt, rejected=rejected)
                else:
                    finish(base, call_id, None, not_submitted=True)
    except BudgetError as exc:
        raise DrawingError(exc.code, str(exc)) from None

    return _pick_image(receipt)
