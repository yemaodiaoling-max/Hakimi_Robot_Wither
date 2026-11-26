"""QQ official HTTP API client and bounded image download."""
import asyncio
import io
import json
import re
import time
import urllib.request
from urllib.parse import urlsplit

from PIL import Image

from hakimi.chat.official_base import API_ROOT, MAX_IMAGE_BYTES, NoRedirect, OfficialError


def business_code(result):
    if not isinstance(result, dict):
        return None
    for key in ('err_code', 'code'):
        value = result.get(key)
        if value is None or value == '':
            continue
        if ((type(value) is int or isinstance(value, str))
                and re.fullmatch(r'-?[0-9]{1,20}', str(value))):
            return int(value)
    return None


def api_error(status, path, code, *, http_error=False):
    if http_error:
        reason = f'HTTP_{code or status}'
    else:
        reason = f'API_{code}' if code else 'API_UNKNOWN'
    error = OfficialError(reason, ambiguous=status >= 500)
    error.endpoint = re.sub(r'/(groups|users)/[^/]+', r'/\1/<target>', urlsplit(path).path)
    error.detail = f'QQ HTTP {status}' + (f' / 业务码 {code}' if code else '')
    return error


class OfficialAPI:
    def __init__(self, app_id, secret, clock=time.time):
        self.app_id, self._secret, self.clock = app_id, secret, clock
        self._token, self._expires = "", 0
        self.lock = asyncio.Lock()
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, method, path, data=None, token=None, raw=False):
        if not path.startswith("/") or ".." in path:
            raise OfficialError("INVALID_API_PATH")
        headers = {"Content-Type": "application/json", "User-Agent": "Hakimi-Official-Test/1"}
        if token:
            headers["Authorization"] = "QQBot " + token
        request = urllib.request.Request(API_ROOT + path, method=method, headers=headers,
            data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.opener.open(request, timeout=9) as response:
                body = response.read(1024 * 1024 + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read(65536))
            except (ValueError, OSError):
                result = {}
            raise api_error(exc.code, path, business_code(result), http_error=True) from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise OfficialError("NETWORK_UNKNOWN", ambiguous=True) from None
        try:
            result = json.loads(body) if body else {}
            if len(body) > 1024 * 1024 or not isinstance(result, dict):
                raise ValueError()
        except ValueError:
            raise OfficialError("INVALID_API_RESPONSE", ambiguous=True) from None
        code = business_code(result)
        if code or code is None and any(result.get(key) not in (None, '') for key in ('err_code', 'code')):
            raise api_error(status, path, code)
        return result

    async def token(self):
        async with self.lock:
            if self.clock() >= self._expires - 45:
                value = await asyncio.to_thread(self._request, "POST", "/app/getAppAccessToken",
                    {"appId": self.app_id, "clientSecret": self._secret})
                token = value.get("access_token")
                try:
                    ttl = float(value["expires_in"])
                    if not isinstance(token, str) or not token or not 60 <= ttl <= 7200:
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise OfficialError("INVALID_TOKEN_RESPONSE") from None
                self._token, self._expires = token, self.clock() + ttl
            return self._token

    async def call(self, method, path, data=None):
        return await asyncio.to_thread(self._request, method, path, data, await self.token())

    async def upload_image(self, group, path, *, private=False):
        from hakimi.transport.official_files import upload
        return await upload(self, group, path, 1, private=private)


    async def upload_file(self, group, path, kind, *, private=False):
        from hakimi.transport.official_files import upload
        return await upload(self, group, path, kind, private=private)


def download_image(url, destination):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host == d or host.endswith("." + d) for d in ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.cn"))):
        raise OfficialError("UNAPPROVED_IMAGE_HOST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(url, timeout=6) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise OfficialError("IMAGE_TOO_LARGE")
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 20_000_000:
                raise OfficialError("IMAGE_DIMENSIONS")
            if image.format == "GIF":
                destination.with_suffix(".gif").write_bytes(data)
            image.seek(0)
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            rgb.thumbnail((1280, 1280))
            rgb.save(destination, "JPEG", quality=88)
    except OfficialError:
        raise
    except Exception:
        raise OfficialError("IMAGE_DOWNLOAD_FAILED") from None
    return str(destination)
