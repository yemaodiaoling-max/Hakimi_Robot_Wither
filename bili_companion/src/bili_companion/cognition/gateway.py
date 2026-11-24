"""Independent, fixed-loopback GenAI REST transport for the existing TLS gateway.

Only the key and CA paths are configured. No QQ imports, tools, proxy, redirects,
prompt logging, retries, SDK, or credential-bearing subprocess arguments.
"""
import json
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.request

HYDROGEN = 'gemini-4-hydrogen'
NATIVE_VIDEO_MODEL = 'gemini-3.5-flash-lite'
MODELS = {NATIVE_VIDEO_MODEL, 'gemini-3.8-flash', HYDROGEN}
BASE_URL = 'https://127.0.0.1:18765'
NATIVE_VIDEO_SOURCES = {'video': 'native-video-frames', 'audio': 'native-video-audio'}


def hydrogen_mp4(model, media_mode):
    return model == HYDROGEN and media_mode == 'mp4_image_url'


class CognitionUnavailable(RuntimeError):
    def __init__(self, code='model_unavailable'):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CognitionUnavailable('model_redirect')


class GatewayClient:
    @classmethod
    def for_hydrogen(cls, config):
        key, ca = Path(config['gateway_key_file']), Path(config['gateway_ca_file'])
        if not key.is_file() or not ca.is_file():
            raise ValueError('Hydrogen gateway credentials unavailable')
        return cls(key, ca)

    def __init__(self, key_file, ca_file, *, timeout=90):
        self.key_file, self.ca_file = Path(key_file), Path(ca_file)
        if not 1 <= timeout <= 180:
            raise ValueError('timeout outside 1..180 seconds')
        self.timeout = timeout
        self.last_http_status = None
        self.rejected_fields = ()

    def _request(self, model, method, payload):
        if model not in MODELS or method not in {'countTokens', 'generateContent'}:
            raise ValueError('unsupported model or method')
        try:
            key = self.key_file.read_bytes().strip()
            if not re.fullmatch(rb'[A-Za-z0-9._~+/=-]{1,4096}', key):
                raise CognitionUnavailable('model_credentials_invalid')
            context = ssl.create_default_context(cafile=str(self.ca_file))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context), _NoRedirect())
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(body) > 24*1024*1024:
                raise CognitionUnavailable('model_request_too_large')
            url = f'{BASE_URL}/v1beta/models/{model}:{method}'
            request = urllib.request.Request(url, data=body, method='POST', headers={
                'Content-Type':'application/json', 'Authorization':'Bearer '+key.decode('ascii')})
            with opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != url:
                    raise CognitionUnavailable('model_redirect')
                raw = response.read(2*1024*1024+1)
            if len(raw) > 2*1024*1024:
                raise CognitionUnavailable('model_response_too_large')
            result = json.loads(raw)
            if type(result) is not dict:
                raise CognitionUnavailable('model_response_invalid')
            return result
        except CognitionUnavailable:
            raise
        except urllib.error.HTTPError as exc:
            self.last_http_status = exc.code
            try:
                error = json.loads(exc.read(65536)).get('error', {})
                message = str(error.get('message', ''))
                # Diagnostics carry schema field NAMES only, never request values,
                # upstream messages, model output, key or response body.
                self.rejected_fields = tuple(re.findall(r'Unknown name ["\']([A-Za-z_][A-Za-z0-9_]*)["\']', message))
            except Exception:
                self.rejected_fields = ()
            code = 'model_auth' if exc.code in {401,403} else 'model_quota' if exc.code == 429 else 'model_http_error'
            exc.close()
            raise CognitionUnavailable(code) from None
        except Exception:
            raise CognitionUnavailable() from None

    def count_tokens(self, model, payload):
        result = self._request(model, 'countTokens', {key:value for key,value in payload.items()
            if key in {'contents','systemInstruction','generationConfig'}})
        total = result.get('totalTokens')
        if type(total) is not int or not 0 <= total <= 200_000:
            raise CognitionUnavailable('model_token_count_invalid')
        return total

    def generate(self, model, payload):
        result = self._request(model, 'generateContent', payload)
        if model == NATIVE_VIDEO_MODEL and any(
            part.get('inlineData', {}).get('mimeType') == 'video/mp4'
            for content in payload.get('contents', []) for part in content.get('parts', [])
        ):
            result['_media_sources'] = dict(NATIVE_VIDEO_SOURCES)
        return result
