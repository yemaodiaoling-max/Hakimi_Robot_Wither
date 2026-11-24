"""Read-only model listing for an explicitly submitted provider origin.

No inference, pricing assumptions, model activation, files, redirects, proxies,
or logs. A listed model is not proof of audio/video support. HTTPS is required
unless the caller records explicit consent to HTTP credential transmission.
"""
import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler, HTTPSHandler

MAX_BYTES = 1024*1024
MAX_MODELS = 200
TIMEOUT = 8
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}')
_METHODS = {'generateContent','streamGenerateContent','countTokens','embedContent',
            'batchEmbedContents','bidiGenerateContent','predict','predictLongRunning'}


class _ProbeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise _ProbeError('redirect_blocked')


def _endpoint(base_url, allow_insecure_http):
    if (type(base_url) is not str or not 1<=len(base_url)<=2048
            or any(ord(c)<33 or ord(c)>126 or c=='\\' for c in base_url)):
        raise _ProbeError('invalid_submission')
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        raise _ProbeError('invalid_submission') from None
    if (url.scheme not in {'https','http'} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or port is not None and not 1<=port<=65535):
        raise _ProbeError('invalid_submission')
    if url.scheme=='http' and allow_insecure_http is not True:
        raise _ProbeError('unsafe_transport')
    decoded = unquote(url.path)
    if any(segment in {'.','..'} for segment in decoded.split('/')) or any(ord(c)<32 or c=='\\' for c in decoded):
        raise _ProbeError('invalid_submission')
    path = url.path.rstrip('/')
    # API base prefix is respected; only conventional terminal resource paths
    # are removed. No server response can select a host or arbitrary probe URL.
    for suffix in ('/chat/completions','/responses','/messages','/models'):
        if path.endswith(suffix):
            path=path[:-len(suffix)]
            break
    origin = urlunsplit((url.scheme,url.netloc,'','',''))
    return origin,path


def _list_url(origin, path, protocol):
    prefix = path or ('/v1beta' if protocol=='gemini' else '/v1')
    return origin+prefix+'/models'


def _read(url, api_key, protocol):
    opener=build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context()),_NoRedirect())
    headers={'Accept':'application/json','User-Agent':'bili-companion-provider-probe/1'}
    if protocol=='anthropic':
        headers.update({'x-api-key':api_key,'anthropic-version':'2023-06-01'})
    elif protocol=='gemini':headers['x-goog-api-key']=api_key
    else:headers['Authorization']='Bearer '+api_key
    request=Request(url,method='GET',headers=headers)
    try:
        with opener.open(request,timeout=TIMEOUT) as response:
            if response.geturl()!=url:
                raise _ProbeError('redirect_blocked')
            raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:
            raise _ProbeError('response_too_large')
        value=json.loads(raw)
        if type(value) is not dict:
            raise _ProbeError('invalid_response')
        return value
    except HTTPError as exc:
        status=exc.code
        exc.close()  # Never read upstream error bodies (which can echo a key).
        code='redirect_blocked' if 300<=status<400 else 'auth_rejected' if status in {401,403} else 'not_found' if status==404 else 'http_error'
        error=_ProbeError(code)
        error.http_status=status
        raise error from None
    except URLError as exc:
        raise _ProbeError('tls_error' if isinstance(exc.reason,ssl.SSLError) else 'network_error') from None
    except _ProbeError:
        raise
    except (ValueError,TypeError,UnicodeError):
        raise _ProbeError('invalid_response') from None
    except Exception:
        raise _ProbeError('network_error') from None


def _models(payload, api_key, attempted_protocol):
    if type(payload.get('models')) is list:
        rows, actual='models','gemini'
    elif type(payload.get('data')) is list:
        rows, actual='data','anthropic' if attempted_protocol=='anthropic' else 'openai'
    else:
        raise _ProbeError('invalid_response')
    result, seen = [], set()
    for row in payload[rows]:
        if type(row) is not dict:
            continue
        name=row.get('name') if actual=='gemini' else row.get('id')
        if type(name) is not str or not _ID.fullmatch(name) or api_key in name:
            continue
        if actual=='gemini' and name.startswith('models/'):
            name=name[7:]
        if not name or name in seen:
            continue
        seen.add(name)
        model={'id':name,'audio_video_capability':'unverified','pricing_status':'missing'}
        methods=row.get('supportedGenerationMethods')
        if type(methods) is list:
            model['declared_methods']=sorted({m for m in methods if type(m) is str and m in _METHODS})
        for upstream,target in (('inputTokenLimit','input_token_limit'),('outputTokenLimit','output_token_limit')):
            number=row.get(upstream)
            if type(number) is int and 0<number<=10_000_000:
                model[target]=number
        result.append(model)
        if len(result)>=MAX_MODELS:
            break
    if not result:
        raise _ProbeError('no_models')
    return result,actual,bool(payload.get('nextPageToken')) or len(payload[rows])>len(result)


def probe_models(base_url, api_key, protocol='auto', *, allow_insecure_http=False):
    """Return safe JSON metadata; never raises with credential-bearing details.

    Auto makes at most two GETs to standard list paths on the submitted origin.
    Custom API prefixes are kept. No pagination, inference, fallback hosts, or
    generation capability checks occur. HTTP is an explicit caller opt-in only.
    """
    result=dict(state='probe_failed',protocol=protocol if protocol in ('auto','gemini','openai','anthropic') else 'auto',
        models=[],model_ids=[],attempts=[],audio_video_capability='unverified',
        pricing_status='missing',needs_real_audio_video_test=True,
        activation_allowed=False,budget_change_allowed=False)
    try:
        if (type(api_key) is not str or not 1<=len(api_key)<=4096
                or any(ord(c)<33 or ord(c)>126 for c in api_key)
                or protocol not in ('auto','gemini','openai','anthropic')):
            raise _ProbeError('invalid_submission')
        # Key-bearing URL submissions are rejected instead of reflected publicly.
        if len(api_key)>=8 and api_key in str(base_url):
            raise _ProbeError('invalid_submission')
        origin,path=_endpoint(base_url,allow_insecure_http)
        result['origin']=origin
        protocols=[protocol] if protocol!='auto' else (['openai','gemini'] if path.endswith('/v1') else ['gemini','openai'])
        for selected in protocols:
            try:
                payload=_read(_list_url(origin,path,selected),api_key,selected)
                models,actual,truncated=_models(payload,api_key,selected)
                result.update(state='models_ready',protocol=actual,models=models,
                    model_ids=[m['id'] for m in models],truncated=truncated)
                result['attempts'].append(dict(protocol=selected,http_status=200))
                result['compatibility_note']=(
                    '模型列表不证明音频或视频能力；须完成真实小音视频和用量校验，价格缺失时不可启用计费。'
                    if actual=='gemini' else
                    'OpenAI兼容列表不证明原生视频支持；须验证帧与音频输入策略，缺少音频能力则不能称为视听观看。价格仍需核实。')
                return result
            except _ProbeError as exc:
                result['attempts'].append(dict(protocol=selected,error=exc.code,http_status=getattr(exc,'http_status',None)))
                result['error']=exc.code
        return result
    except _ProbeError as exc:
        result['error']=exc.code
        return result
    except Exception:
        result['error']='invalid_submission'
        return result
